"""Reconcile clarification answers for a target project and lot.

A bid specialist asks several written clarification questions and must know,
before finalising a bid, which ones have actually been answered.  This module
crawls a paginated Q&A directory on a single site and, for every requested
question number, decides one of:

* ``answered``       – a substantive answer was found (possibly by following
  one or more "参见另一问题 Qxxx" references across pages);
* ``unanswered``     – the question was found but has no answer body, or was
  not found at all while every relevant page was read successfully;
* ``needs_review``   – the evidence conflicts (a circular reference, a
  dangling reference, or the same question number carrying different direct
  answer texts);
* ``indeterminate``  – a list/detail page could not be fetched or parsed, so
  the absence of an answer cannot be trusted this round.

A fetch failure is never turned into an "unanswered" verdict: any requested
question whose status cannot be proven under incomplete data is reported as
``indeterminate`` together with the failing address.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx
from bs4 import BeautifulSoup, Tag

from .crawler import (
    MAX_DETAIL_PAGES,
    MAX_LIST_PAGES,
    USER_AGENT,
    FETCH_TIMEOUT,
    FetchIssue,
    _fetch,
    _hints_for_anchor,
    extract_links,
    normalize_space,
    normalize_url,
    project_matches,
    section_matches,
)

MAX_REFERENCE_HOPS = int(os.getenv("MAX_QA_REFERENCE_HOPS", "10"))

# A clarification page whose whole answer points at another question, e.g.
# "本问题答复参见另一问题 Q003 的答复。" / "请详见第 Q008 问的答复。"
REFERENCE_RE = re.compile(
    r"(?:参见另一问题|参见问题|详见问题|请参见问题|参照问题|参见第|详见第|请参见|请详见|参见|详见)"
    r"\s*(?:问题|第)?\s*"
    r"(?P<ref>[A-Za-z0-9]+)\s*(?:问|题|号)?"
)

# Everything that is allowed around a reference phrase in a pure pointer
# answer: connectors, the words "问题/答复/事项" and punctuation.  Chinese
# characters outside this list make the text a substantive answer.
REFERENCE_SURROUND_RE = re.compile(r"^[\s，,。.；;：:、（()）的了是为与和按该此本就请关于有关相关事项问题答复回复内容具体说明A-Za-z0-9]*$")

# Visible placeholders publishers use when an answer has not been issued yet.
EMPTY_ANSWER_PLACEHOLDER_RE = re.compile(
    r"^(?:本问题|该问题|此问题)?\s*(?:暂未答复|暂无答复|待答复|未答复|尚未答复)[。.]?$"
)

# Labels used when a page gives the question number in plain body text rather
# than in a data-* attribute.
QUESTION_LABEL_RE = re.compile(
    r"(?:问题编号|澄清问题编号|提问编号|问题序号|质疑编号)\s*(?:为|是)?\s*[：:]\s*"
    r"([A-Za-z0-9\-_]{1,40})"
)
ANSWER_LABEL_RE = re.compile(r"(?:采购人答复|招标人答复|答复内容|回复内容|答复意见|答复|回复)\s*[：:]")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def canonical_question(value: str | None) -> str:
    """Upper-case, de-spaced question number used for equality checks."""

    if not value:
        return ""
    text = re.sub(r"\s+", "", value).upper()
    text = text.strip("，。；;、：:()（）")
    return text


def split_question_numbers(raw: str | None) -> list[str]:
    """Split a free-form question-number field into trimmed values."""

    if not raw:
        return []
    parts = re.split(r"[，,;；、\s]+", raw.strip())
    return [part.strip() for part in parts if part.strip()]


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def _article(soup: BeautifulSoup) -> Tag:
    for selector in ("article.qa-detail", "article.qa", "article", "main", "body"):
        found = soup.select_one(selector)
        if found is not None:
            return found
    return soup  # type: ignore[return-value]


def _first_data_attr(container: Tag, names: Sequence[str]) -> str | None:
    for name in names:
        attr = container.get(name)
        if attr:
            return str(attr).strip()
    for name in names:
        for tag in container.find_all(attrs={name: True}):
            value = tag.get(name)
            if value:
                return str(value).strip()
    return None


def _project_candidates(container: Tag, text: str) -> list[str]:
    explicit: list[str] = []
    attr = container.get("data-project-no")
    if attr:
        explicit.append(str(attr))
    for tag in container.find_all(attrs={"data-project-no": True}):
        value = tag.get("data-project-no")
        if value:
            explicit.append(str(value))
    if explicit:
        return list(dict.fromkeys(explicit))
    return [match.group(1).strip() for match in re.finditer(r"项目编号\s*[：:]\s*([^\s，。；;、|（(\n]{2,80})", text)]


def _section_candidates(container: Tag, text: str) -> list[str]:
    explicit: list[str] = []
    for name in ("data-section-no", "data-section", "data-lot", "data-package-no"):
        value = container.get(name)
        if value:
            explicit.append(str(value))
    for name in ("data-section-no", "data-section", "data-lot", "data-package-no"):
        for tag in container.find_all(attrs={name: True}):
            value = tag.get(name)
            if value:
                explicit.append(str(value))
    if explicit:
        return list(dict.fromkeys(explicit))
    candidates: list[str] = []
    for match in re.finditer(r"(?:标段/包(?:编号|号)?|标段(?:编号|号)?|包号|分包号)\s*[：:]\s*([^\s，。；;、|（(\n]{1,40})", text):
        candidates.append(match.group(1).strip())
    return list(dict.fromkeys(candidates))


def _extract_question_no(container: Tag, text: str) -> str | None:
    attr = _first_data_attr(container, ("data-question-no", "data-question", "data-qa-no"))
    if attr:
        return attr
    code = container.select_one("code.qa-question-no")
    if code is not None:
        value = normalize_space(code.get_text(" ", strip=True))
        if value:
            return value
    match = QUESTION_LABEL_RE.search(text)
    if match:
        return match.group(1).strip()
    heading = container.find(["h1", "h2"])
    if heading is not None:
        match = re.search(r"问题\s*([A-Za-z0-9\-_]{1,40})", heading.get_text(" ", strip=True))
        if match:
            return match.group(1)
    return None


def _extract_answer(container: Tag, text: str) -> str:
    marked = container.select_one("[data-qa-answer]")
    if marked is not None:
        paragraphs = []
        for raw_line in marked.get_text("\n").splitlines():
            line = normalize_space(raw_line)
            if not line:
                continue
            # Drop an answer-section heading ("采购人答复") rendered inside the
            # marked container, with or without a trailing colon.
            if line in {"采购人答复", "招标人答复", "答复内容", "回复内容", "答复意见", "答复", "回复"}:
                continue
            if ANSWER_LABEL_RE.fullmatch(line):
                continue
            paragraphs.append(line)
        answer = "\n".join(paragraphs)
    else:
        lines = text.splitlines()
        answer = ""
        for index, line in enumerate(lines):
            if ANSWER_LABEL_RE.search(line):
                tail = ANSWER_LABEL_RE.sub("", line).strip()
                body = [tail] if tail else []
                body.extend(lines[index + 1 :])
                answer = normalize_space("\n".join(body))
                break

    for line in answer.splitlines():
        if EMPTY_ANSWER_PLACEHOLDER_RE.match(line):
            return ""
    return answer


def extract_reference(answer: str) -> str | None:
    """Return the referenced question number when an answer is a pure pointer."""

    text = normalize_space(answer)
    if not text:
        return None
    match = REFERENCE_RE.search(text)
    if not match:
        return None
    # A pointer answer is built entirely around the reference phrase.  A topic
    # preamble ending in a comma ("关于联合体资质认定，答复内容请参见……") names
    # the subject but adds no substantive answer; anything else with
    # unrecognised Chinese characters is treated as a real answer.
    prefix = text[: match.start()]
    if "，" in prefix or "," in prefix:
        prefix = re.split(r"[，,]", prefix, maxsplit=1)[-1]
    suffix = text[match.end() :]
    if not REFERENCE_SURROUND_RE.match(prefix) or not REFERENCE_SURROUND_RE.match(suffix):
        return None
    return match.group("ref").upper()


@dataclass
class QAParsed:
    url: str
    project_candidates: list[str]
    section_candidates: list[str]
    question_no: str
    question_no_raw: str | None
    answer: str
    reference: str | None
    title: str
    published_at: str | None
    text: str


def parse_qa_html(html: str, url: str) -> QAParsed:
    soup = BeautifulSoup(html, "html.parser")
    container = _article(soup)
    text = "\n".join(
        normalize_space(line)
        for line in container.get_text("\n").splitlines()
        if normalize_space(line)
    )
    title = ""
    h1 = soup.find("h1")
    if h1 is not None:
        title = normalize_space(h1.get_text(" ", strip=True))
    elif soup.title is not None:
        title = normalize_space(soup.title.get_text(" ", strip=True))

    question_raw = _extract_question_no(container, text)
    answer = _extract_answer(container, text)
    published = None
    time_tag = container.find("time")
    if time_tag is not None and time_tag.get_text(strip=True):
        published = normalize_space(time_tag.get_text(" ", strip=True))
    if published is None:
        match = re.search(r"发布时间\s*[：:]\s*([0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2}日?[\s0-9:：]*)", text)
        if match:
            published = normalize_space(match.group(1))

    return QAParsed(
        url=url,
        project_candidates=_project_candidates(container, text),
        section_candidates=_section_candidates(container, text),
        question_no=canonical_question(question_raw),
        question_no_raw=question_raw,
        answer=answer,
        reference=extract_reference(answer),
        title=title,
        published_at=published,
        text=text,
    )


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------


@dataclass
class QARecord:
    parsed: QAParsed
    source_url: str
    list_page: str


@dataclass
class QAHop:
    kind: str  # direct | reference
    question_no: str
    url: str | None
    title: str | None
    note: str = ""

    def as_dict(self, public_url_fn=None) -> dict[str, Any]:
        url = self.url
        if url and public_url_fn:
            url = public_url_fn(url)
        return {
            "kind": self.kind,
            "question_no": self.question_no,
            "url": url,
            "title": self.title,
            "note": self.note,
        }


@dataclass
class QAQuestionResult:
    question_no: str
    status: str  # answered | unanswered | needs_review | indeterminate
    status_label: str
    answer: str | None
    answer_source_url: str | None
    answer_source_title: str | None
    record_count: int
    chain: list[QAHop]
    reasons: list[str]

    def as_dict(self, public_url_fn=None) -> dict[str, Any]:
        source = self.answer_source_url
        if source and public_url_fn:
            source = public_url_fn(source)
        return {
            "question_no": self.question_no,
            "status": self.status,
            "status_label": self.status_label,
            "answer": self.answer,
            "answer_source_url": source,
            "answer_source_title": self.answer_source_title,
            "record_count": self.record_count,
            "chain": [hop.as_dict(public_url_fn) for hop in self.chain],
            "reasons": self.reasons,
        }


STATUS_LABELS = {
    "answered": "已答复",
    "unanswered": "未答复",
    "needs_review": "待核实",
    "indeterminate": "无法核定",
}


@dataclass
class QACheckOutcome:
    list_url: str
    project_no: str
    section_no: str
    question_numbers: list[str]
    results: list[QAQuestionResult]
    issues: list[FetchIssue]
    warnings: list[str]
    searched_pages: list[str]
    checked_detail_urls: list[str]
    detail_page_count: int
    round_incomplete: bool

    def as_dict(self, public_url_fn=None) -> dict[str, Any]:
        def pub(url: str | None) -> str | None:
            if not url:
                return None
            return public_url_fn(url) if public_url_fn else url

        return {
            "input_url": self.list_url,
            "project_no": self.project_no,
            "section_no": self.section_no,
            "question_numbers": self.question_numbers,
            "round_incomplete": self.round_incomplete,
            "results": [result.as_dict(public_url_fn) for result in self.results],
            "failures": [
                issue.as_dict() | {"url": pub(issue.url) or issue.url} for issue in self.issues
            ],
            "warnings": self.warnings,
            "searched_pages": [pub(url) or url for url in self.searched_pages],
            "checked_detail_urls": [pub(url) or url for url in self.checked_detail_urls],
            "detail_page_count": self.detail_page_count,
            "summary": {
                "total": len(self.results),
                "answered": sum(1 for r in self.results if r.status == "answered"),
                "unanswered": sum(1 for r in self.results if r.status == "unanswered"),
                "needs_review": sum(1 for r in self.results if r.status == "needs_review"),
                "indeterminate": sum(1 for r in self.results if r.status == "indeterminate"),
            },
        }


async def check_qa(
    list_url: str,
    project_no: str,
    section_no: str,
    question_numbers: Iterable[str],
    *,
    client_factory=None,
) -> QACheckOutcome:
    """Crawl the Q&A directory and reconcile every requested question."""

    targets: list[str] = []
    seen_targets: set[str] = set()
    for raw in question_numbers:
        value = canonical_question(raw)
        if value and value not in seen_targets:
            seen_targets.add(value)
            targets.append(value)

    try:
        start_url = normalize_url(list_url)
    except ValueError as exc:
        issue = FetchIssue(url=list_url, reason=str(exc), stage="validation")
        results = [
            QAQuestionResult(
                question_no=value,
                status="indeterminate",
                status_label=STATUS_LABELS["indeterminate"],
                answer=None,
                answer_source_url=None,
                answer_source_title=None,
                record_count=0,
                chain=[],
                reasons=[f"答疑目录地址无效：{exc}"],
            )
            for value in targets
        ]
        return QACheckOutcome(
            list_url=list_url,
            project_no=project_no,
            section_no=section_no,
            question_numbers=targets,
            results=results,
            issues=[issue],
            warnings=[],
            searched_pages=[],
            checked_detail_urls=[],
            detail_page_count=0,
            round_incomplete=True,
        )

    project_no = normalize_space(project_no)
    section_no = normalize_space(section_no)
    issues: list[FetchIssue] = []
    warnings: list[str] = []
    searched_pages: list[str] = []
    checked_detail_urls: list[str] = []
    records: list[QARecord] = []
    seen_pages: set[str] = set()
    seen_details: set[str] = set()
    queue: list[str] = [start_url]
    detail_count = 0
    incomplete = False

    timeout = httpx.Timeout(FETCH_TIMEOUT, connect=min(8.0, FETCH_TIMEOUT))
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)

    if client_factory is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
            limits=limits,
        )
        owns_client = True
    else:
        client = client_factory()
        owns_client = False

    try:
        while queue:
            if len(searched_pages) >= MAX_LIST_PAGES:
                incomplete = True
                warnings.append(f"答疑列表超过 {MAX_LIST_PAGES} 页，已停止继续翻页。")
                break
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            searched_pages.append(page_url)

            html, error, final_page_url = await _fetch(client, page_url, "list")
            if html is None:
                issues.append(
                    FetchIssue(url=page_url, reason=f"答疑列表页抓取失败：{error}", stage="list")
                )
                incomplete = True
                continue
            if final_page_url != page_url:
                if final_page_url not in searched_pages:
                    searched_pages.append(final_page_url)
                seen_pages.add(final_page_url)

            try:
                links = extract_links(html, final_page_url)
            except Exception as exc:
                issues.append(
                    FetchIssue(
                        url=page_url,
                        reason=f"答疑列表页解析失败：{exc.__class__.__name__}",
                        stage="list",
                    )
                )
                incomplete = True
                continue

            for next_url in links.pagination:
                if next_url not in seen_pages and next_url not in queue:
                    queue.append(next_url)

            candidates: list[tuple[str, Any]] = []
            pagination_set = set(links.pagination)
            for index, detail_url in enumerate(links.detail_urls):
                if detail_url in seen_details or detail_url in pagination_set:
                    continue
                anchor = links.details[index] if index < len(links.details) else None
                project_hint, section_hint, _ = (
                    _hints_for_anchor(anchor, detail_url) if anchor is not None else (None, None, None)
                )
                if project_hint and not project_matches(project_no, [project_hint]):
                    continue
                if project_hint and section_hint and not section_matches(section_no, [section_hint]):
                    continue
                candidates.append((detail_url, anchor))

            if detail_count + len(candidates) > MAX_DETAIL_PAGES:
                incomplete = True
                warnings.append(f"答疑详情超过 {MAX_DETAIL_PAGES} 页，已停止继续核对。")
                candidates = candidates[: max(0, MAX_DETAIL_PAGES - detail_count)]

            async def fetch_detail(detail_url: str, anchor: Any):
                detail_html, detail_error, final_url = await _fetch(client, detail_url, "detail")
                return detail_url, anchor, detail_html, detail_error, final_url

            fetched = await asyncio.gather(
                *(fetch_detail(url, anchor) for url, anchor in candidates),
                return_exceptions=False,
            )

            for detail_url, anchor, detail_html, detail_error, final_detail_url in fetched:
                if detail_url in seen_details:
                    continue
                seen_details.add(detail_url)
                checked_detail_urls.append(detail_url)
                detail_count += 1
                if detail_html is None:
                    issues.append(
                        FetchIssue(
                            url=detail_url,
                            reason=f"答疑详情页抓取失败：{detail_error}",
                            stage="detail",
                        )
                    )
                    incomplete = True
                    continue
                source_url = final_detail_url or detail_url
                try:
                    parsed = parse_qa_html(detail_html, source_url)
                except Exception as exc:
                    issues.append(
                        FetchIssue(
                            url=detail_url,
                            reason=f"答疑详情页解析失败：{exc.__class__.__name__}",
                            stage="detail",
                        )
                    )
                    incomplete = True
                    continue

                if not parsed.project_candidates or not parsed.section_candidates:
                    issues.append(
                        FetchIssue(
                            url=detail_url,
                            reason="答疑详情页未解析出项目编号或标段号，无法确认归属",
                            stage="parse",
                        )
                    )
                    incomplete = True
                    continue
                if not project_matches(project_no, parsed.project_candidates):
                    # Another project: explicitly excluded.
                    continue
                if not section_matches(section_no, parsed.section_candidates):
                    # Another lot of the same project: explicitly excluded.
                    continue
                if not parsed.question_no:
                    issues.append(
                        FetchIssue(
                            url=detail_url,
                            reason="答疑详情页未解析出问题编号，无法对应到待核对问题",
                            stage="parse",
                        )
                    )
                    incomplete = True
                    continue

                records.append(QARecord(parsed=parsed, source_url=source_url, list_page=page_url))
    finally:
        if owns_client:
            await client.aclose()

    # Preserve publication order when the pages provide timestamps.
    records.sort(key=lambda record: (record.parsed.published_at or "", record.source_url))

    by_question: dict[str, list[QARecord]] = {}
    for record in records:
        by_question.setdefault(record.parsed.question_no, []).append(record)

    results = [
        _resolve_question(
            target,
            by_question.get(target, []),
            by_question,
            incomplete,
        )
        for target in targets
    ]

    return QACheckOutcome(
        list_url=start_url,
        project_no=project_no,
        section_no=section_no,
        question_numbers=targets,
        results=results,
        issues=issues,
        warnings=warnings,
        searched_pages=searched_pages,
        checked_detail_urls=checked_detail_urls,
        detail_page_count=detail_count,
        round_incomplete=incomplete,
    )


def _resolve_question(
    target: str,
    own_records: list[QARecord],
    by_question: dict[str, list[QARecord]],
    incomplete: bool,
) -> QAQuestionResult:
    """Apply the answering / conflict / reference rules for one question."""

    chain: list[QAHop] = []
    review_reasons: list[str] = []

    if not own_records:
        if incomplete:
            return QAQuestionResult(
                question_no=target,
                status="indeterminate",
                status_label=STATUS_LABELS["indeterminate"],
                answer=None,
                answer_source_url=None,
                answer_source_title=None,
                record_count=0,
                chain=[],
                reasons=["本轮存在页面抓取或解析失败，无法确认该问题是否已发布答复，不能按未答复处理。"],
            )
        return QAQuestionResult(
            question_no=target,
            status="unanswered",
            status_label=STATUS_LABELS["unanswered"],
            answer=None,
            answer_source_url=None,
            answer_source_title=None,
            record_count=0,
            chain=[],
            reasons=["已完整读取答疑目录，未找到该问题编号的答疑记录。"],
        )

    for record in own_records:
        chain.append(
            QAHop(
                kind="direct",
                question_no=record.parsed.question_no,
                url=record.source_url,
                title=record.parsed.title,
            )
        )

    direct_texts = {
        record.parsed.answer
        for record in own_records
        if record.parsed.answer and not record.parsed.reference
    }
    if len(direct_texts) > 1:
        review_reasons.append(
            f"问题 {target} 出现 {len(direct_texts)} 种不同的直接答复正文，无法自动确定以哪条为准。"
        )

    substantive = [
        record for record in own_records if record.parsed.answer and not record.parsed.reference
    ]
    empty_records = [record for record in own_records if not record.parsed.answer]
    reference_records = [record for record in own_records if record.parsed.reference]

    # Follow the first published reference chain.  Cycles and dangling
    # references make the question unverifiable rather than silently answered.
    followed: QARecord | None = None
    terminal_empty: QARecord | None = None
    if reference_records and not substantive:
        first_ref = reference_records[0]
        visited = [target]
        current_ref = first_ref.parsed.reference
        hops = 0
        while current_ref:
            hops += 1
            if hops > MAX_REFERENCE_HOPS:
                review_reasons.append(f"问题 {target} 的引用链超过 {MAX_REFERENCE_HOPS} 跳，已停止追踪。")
                break
            if current_ref in visited:
                cycle = " → ".join(visited + [current_ref])
                review_reasons.append(f"发现循环引用：{cycle}，无法定位实际答复。")
                break
            visited.append(current_ref)
            targets_records = by_question.get(current_ref, [])
            if not targets_records:
                chain.append(
                    QAHop(
                        kind="reference",
                        question_no=current_ref,
                        url=None,
                        title=None,
                        note="被引用的问题编号在本项目/本标段答疑目录中不存在",
                    )
                )
                review_reasons.append(
                    f"问题 {target} 的答复引用了问题 {current_ref}，但目录中找不到该问题。"
                )
                break

            next_record = targets_records[0]
            chain.append(
                QAHop(
                    kind="reference",
                    question_no=current_ref,
                    url=next_record.source_url,
                    title=next_record.parsed.title,
                )
            )
            if not next_record.parsed.answer:
                # Reached a question whose answer body is empty.
                terminal_empty = next_record
                break
            if next_record.parsed.reference:
                current_ref = next_record.parsed.reference
                continue
            followed = next_record
            break

    # ----- Decide the status ------------------------------------------------
    if review_reasons:
        return QAQuestionResult(
            question_no=target,
            status="needs_review",
            status_label=STATUS_LABELS["needs_review"],
            answer=None,
            answer_source_url=None,
            answer_source_title=None,
            record_count=len(own_records),
            chain=chain,
            reasons=review_reasons,
        )

    if substantive:
        chosen = substantive[-1]
        reasons = ["已在答疑详情中找到直接答复。"]
        return QAQuestionResult(
            question_no=target,
            status="answered",
            status_label=STATUS_LABELS["answered"],
            answer=chosen.parsed.answer,
            answer_source_url=chosen.source_url,
            answer_source_title=chosen.parsed.title,
            record_count=len(own_records),
            chain=chain,
            reasons=reasons,
        )

    if followed is not None:
        # Proven answers stay valid even when other pages failed this round.
        return QAQuestionResult(
            question_no=target,
            status="answered",
            status_label=STATUS_LABELS["answered"],
            answer=followed.parsed.answer,
            answer_source_url=followed.source_url,
            answer_source_title=followed.parsed.title,
            record_count=len(own_records),
            chain=chain,
            reasons=[
                f"本问题答复为引用说明，已跨页追踪 {len(chain) - 1} 跳至问题 "
                f"{followed.parsed.question_no} 的实际答复。"
            ],
        )

    # No substantive answer and no review conflict: the record (or the end of
    # its reference chain) is an empty answer body.
    empty_source = terminal_empty or (empty_records[0] if empty_records else None)
    if empty_source is not None:
        if incomplete:
            return QAQuestionResult(
                question_no=target,
                status="indeterminate",
                status_label=STATUS_LABELS["indeterminate"],
                answer=None,
                answer_source_url=None,
                answer_source_title=None,
                record_count=len(own_records),
                chain=chain,
                reasons=[
                    f"问题 {empty_source.parsed.question_no} 答复为空，且本轮存在页面抓取或解析失败，"
                    "暂无法排除其他页面存在答复，不能按未答复处理。"
                ],
            )
        reason = (
            f"引用链终点问题 {empty_source.parsed.question_no} 答复为空，未取得实际答复。"
            if terminal_empty is not None
            else "已找到该问题，但答复正文为空。"
        )
        return QAQuestionResult(
            question_no=target,
            status="unanswered",
            status_label=STATUS_LABELS["unanswered"],
            answer=None,
            answer_source_url=None,
            answer_source_title=None,
            record_count=len(own_records),
            chain=chain,
            reasons=[reason],
        )

    # Defensive fallback: should be unreachable given the branches above.
    return QAQuestionResult(
        question_no=target,
        status="needs_review",
        status_label=STATUS_LABELS["needs_review"],
        answer=None,
        answer_source_url=None,
        answer_source_title=None,
        record_count=len(own_records),
        chain=chain,
        reasons=[f"问题 {target} 的引用链未定位到实际答复。"],
    )
