"""FastAPI web front end for the bid-deadline checker."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .crawler import check_lot, normalize_url

BASE_DIR = Path(__file__).resolve().parent
DEMO_INTERNAL_URL = os.getenv("DEMO_INTERNAL_URL", "http://localhost:8001").rstrip("/")
DEMO_PUBLIC_URL = os.getenv("DEMO_PUBLIC_URL", "http://localhost:8001").rstrip("/")
DEMO_LIST_URL = os.getenv(
    "DEMO_LIST_URL", f"{DEMO_PUBLIC_URL}/notices?page=1"
)

app = FastAPI(
    title="采购标段截标时间核验",
    description="跨页核对原公告、延期公告和终止公告中的最新投标截止时间",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def demo_crawl_url(url: str) -> str:
    """Accept either the public demo address or its browser /demo proxy path.

    The app lives in a different container from the demo site, so a user may
    naturally paste the demo URL they can see in their browser
    (``localhost:8001``).  Translate that known public origin to the
    container-internal address before the crawler starts.
    """

    normalized = normalize_url(url)
    source = urlsplit(normalized)
    internal = urlsplit(DEMO_INTERNAL_URL)

    if source.netloc.lower() == internal.netloc.lower():
        return normalized

    # The header exposes the demo through this app at /demo/..., which is a
    # valid, browser-visible listing address too.
    if source.path == "/demo" or source.path.startswith("/demo/"):
        target_path = source.path[5:] or "/"
        target = DEMO_INTERNAL_URL + (target_path if target_path.startswith("/") else "/" + target_path)
        return target + ("?" + source.query if source.query else "")

    # Docker Compose publishes the demo on host port 8001.  Match either
    # localhost or 127.0.0.1 (and any configured public demo origin).
    try:
        source_port = source.port
    except ValueError:
        source_port = None
    public_netloc = urlsplit(DEMO_PUBLIC_URL).netloc.lower()
    if source.netloc.lower() == public_netloc or source_port == 8001:
        target = DEMO_INTERNAL_URL + (source.path or "/")
        return target + ("?" + source.query if source.query else "")

    return normalized


def public_url_for(url: str | None) -> str | None:
    """Turn the container-only demo URL into a browser-friendly proxy URL."""

    if not url:
        return None
    try:
        source = urlsplit(url)
        demo = urlsplit(DEMO_INTERNAL_URL)
        if (
            source.scheme in {"http", "https"}
            and source.netloc.lower() == demo.netloc.lower()
        ):
            path = source.path or "/"
            proxied = "/demo" + (path if path.startswith("/") else "/" + path)
            if source.query:
                proxied += "?" + source.query
            return proxied
    except Exception:
        pass
    return url


templates.env.filters["public_url"] = public_url_for


@app.middleware("http")
async def disable_stale_results(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/check", "/api/check"} or request.url.path.startswith("/demo"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


class CheckPayload(BaseModel):
    list_url: str = Field(..., description="公告列表页地址")
    project_no: str = Field(..., description="项目编号")
    section_no: str = Field(..., description="标段号")


def _demo_presets() -> list[dict[str, str]]:
    presets = [
        {
            "name": "投标进行中（已延期）",
            "list_url": DEMO_LIST_URL,
            "project_no": "DEMO-2026-001",
            "section_no": "02",
            "note": "原公告第 1 页，延期公告第 2 页",
        },
        {
            "name": "已终止",
            "list_url": DEMO_LIST_URL,
            "project_no": "DEMO-2026-001",
            "section_no": "01",
            "note": "原公告、延期、终止分处 1/2/3 页；同项目 02 标段不得混入",
        },
        {
            "name": "不同编号格式",
            "list_url": DEMO_LIST_URL,
            "project_no": "DEMO-2026-002",
            "section_no": "A",
            "note": "A 包；结果公告不应改变截止时间",
        },
    ]
    for preset in presets:
        preset["href"] = "/check?" + urlencode(
            {
                "list_url": preset["list_url"],
                "project_no": preset["project_no"],
                "section_no": preset["section_no"],
            }
        )
    return presets


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "demo_list_url": DEMO_LIST_URL,
        "demo_public_url": "/demo/notices?page=1",
        "presets": _demo_presets(),
        "demo_available": bool(DEMO_INTERNAL_URL),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        _base_context(request) | {"result": None, "form": {}, "form_error": None},
    )


@app.get("/check", response_class=HTMLResponse)
async def check_page(
    request: Request,
    list_url: str = "",
    project_no: str = "",
    section_no: str = "",
):
    form = {
        "list_url": list_url,
        "project_no": project_no,
        "section_no": section_no,
    }
    if not list_url.strip() or not project_no.strip() or not section_no.strip():
        return templates.TemplateResponse(
            request,
            "index.html",
            _base_context(request)
            | {
                "result": None,
                "form": form,
                "form_error": "请填写公告列表页地址、项目编号和标段号。",
            },
            status_code=400,
        )

    try:
        normalized = demo_crawl_url(list_url)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            _base_context(request)
            | {"result": None, "form": form, "form_error": str(exc)},
            status_code=400,
        )

    outcome = await check_lot(normalized, project_no, section_no)
    result = outcome.as_dict(public_url_for)
    return templates.TemplateResponse(
        request,
        "index.html",
        _base_context(request) | {"result": result, "form": form, "form_error": None},
    )


async def _json_result(list_url: str, project_no: str, section_no: str) -> JSONResponse:
    try:
        crawl_url = demo_crawl_url(list_url)
    except ValueError:
        # Let check_lot produce its normal conservative "unknown" shape and
        # failed-address details for a malformed address.
        result = (await check_lot(list_url, project_no, section_no)).as_dict(public_url_for)
        return JSONResponse(result, status_code=400)
    result = (await check_lot(crawl_url, project_no, section_no)).as_dict(public_url_for)
    return JSONResponse(result)


@app.get("/api/check")
async def api_check(
    list_url: Annotated[str, Query(description="公告列表页地址")],
    project_no: Annotated[str, Query(description="项目编号")],
    section_no: Annotated[str, Query(description="标段号")],
):
    return await _json_result(list_url, project_no, section_no)


@app.post("/api/check")
async def api_check_post(payload: CheckPayload):
    return await _json_result(payload.list_url, payload.project_no, payload.section_no)


_ABSOLUTE_LINK_RE = re.compile(r'((?:href|src|action)=["\'])(/(?!/))', re.I)


def _rewrite_demo_html(html: str) -> str:
    """Make the demo site's root-absolute links work behind /demo."""

    return _ABSOLUTE_LINK_RE.sub(r"\1/demo\2", html)


@app.get("/demo", include_in_schema=False)
async def demo_root():
    return RedirectResponse(url="/demo/notices?page=1", status_code=307)


@app.get("/demo/{path:path}", include_in_schema=False)
async def demo_proxy(request: Request, path: str):
    target = DEMO_INTERNAL_URL + "/" + path.lstrip("/")
    if request.url.query:
        target += "?" + request.url.query

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            upstream = await client.get(target)
    except httpx.HTTPError:
        return HTMLResponse(
            "<h1>示例站暂时不可用</h1><p>请稍后再试，或直接访问示例站端口 8001。</p>",
            status_code=502,
        )

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    content: bytes | str = upstream.content
    if "text/html" in content_type.lower():
        content = _rewrite_demo_html(upstream.text)

    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in {"cache-control", "etag", "last-modified"}
    }
    return Response(
        content=content,
        status_code=upstream.status_code,
        media_type=content_type.split(";")[0],
        headers=headers,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "bid-deadline-checker"}
