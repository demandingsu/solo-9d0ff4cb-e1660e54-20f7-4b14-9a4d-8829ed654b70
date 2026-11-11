"""Crawl a notice index and reconcile the target lot's deadline.

The checker is deliberately conservative.  It never guesses from a cached
answer: a fetch failure, an unparseable relevant page, a missing original
notice, or an ambiguous postponement makes the lot *indeterminate*.  A
termination is represented as terminated and never exposes an old deadline as
the currently valid one.
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

MAX_LIST_PAGES = int(os.getenv("MAX_LIST_PAGES", "30"))
MAX_DETAIL_PAGES = int(os.getenv("MAX_DETAILS", "250"))
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "15"))
MAX_RESPONSE_BYTES = int(os.getenv("MAX_RESPONSE_BYTES", str(10 * 1024 * 1024)))

USER_AGENT = (
    "BidDeadlineChecker/1.0 (local procurement notice verifier; "
    "respectful single-purpose crawler)"
)

PUNCTUATION_TRIM = " \t\r\n。，,；;、：:|"


# ---------------------------------------------------------------------------
# Text/date helpers
# ---------------------------------------------------------------------------


def normalize_width(value: str) -> str:
    """Normalise full-width punctuation/digits without changing semantics."""

    return unicodedata.normalize("NFKC", value)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


_DATE_CORE = (
    r"\d{4}\s*(?:年|-|/|\.)\s*\d{1,2}\s*(?:月|-|/|\.)\s*\d{1,2}\s*日?"
    r"(?:\s*(?:T\s*)?\d{1,2}\s*[:：时]\s*\d{1,2}"
    r"(?:\s*[:：分]\s*\d{1,2}\s*秒?)?)?"
)
DATE_RE = re.compile(r"(?P<date>" + _DATE_CORE + r")")
_DATE_PARTS_RE = re.compile(
    r"(?P<year>\d{4})\s*(?:年|-|/|\.)\s*"
    r"(?P<month>\d{1,2})\s*(?:月|-|/|\.)\s*"
    r"(?P<day>\d{1,2})\s*日?"
    r"(?:\s*(?:T\s*)?(?P<hour>\d{1,2})\s*[:：时]\s*"
    r"(?P<minute>\d{1,2})(?:\s*[:：分]\s*(?P<second>\d{1,2})\s*秒?)?)?"
)

PUBLISH_LABEL_RE = re.compile(
    r"(?:公告发布时间|信息发布时间|公告发布日期|发布时间|发布日期|"
    r"上网时间|发布日|发布于|公告日期)\s*(?:为|是)?\s*[：:]?\s*"
)

PROJECT_LABEL_RE = re.compile(
    r"(?:采购项目编号|招标项目编号|项目编号|招标编号|项目编码|采购编号|项目代码)"
    r"\s*(?:为|是)?\s*[：:]\s*"
    r"([^\s，。；;、|（(\n]{2,80})"
)

SECTION_LABEL_RE = re.compile(
    r"(?:标段[（(]包[）)](?:编号|号)?|标段/包(?:编号|号)?|"
    r"标段(?:编号|号|名称)?|标包(?:编号|号)?|"
    r"分包(?:编号|号)?|合同包(?:编号|号)?|包(?:编号|号)|分包号)"
    r"\s*(?:为|是)?\s*[：:]\s*"
    r"([^\s，。；;、|（(\n]{1,40})"
)

SECTION_INLINE_RE = re.compile(
    r"第?\s*([0-9A-Za-z一二三四五六七八九十百]+)\s*(?:标段|标包|分包|合同包)"
)


def parse_datetime(value: str | None) -> tuple[datetime, bool] | None:
    """Return (datetime, had_clock_time) or None for an invalid value."""

    if not value:
        return None
    match = _DATE_PARTS_RE.search(normalize_width(value))
    if not match:
        return None
    groups = match.groupdict()
    try:
        result = datetime(
            int(groups["year"]),
            int(groups["month"]),
            int(groups["day"]),
            int(groups["hour"] or 0),
            int(groups["minute"] or 0),
            int(groups["second"] or 0),
        )
    except (TypeError, ValueError):
        return None
    return result, groups["hour"] is not None


def format_dt(value: datetime | None, include_time: bool = True) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M" if include_time else "%Y-%m-%d")


def parse_date_after_label(text: str, label_re: re.Pattern[str]) -> tuple[datetime, str, bool] | None:
    for match in label_re.finditer(text):
        segment = text[match.end() : match.end() + 120]
        date_match = DATE_RE.search(segment)
        if not date_match:
            continue
        parsed = parse_datetime(date_match.group("date"))
        if parsed:
            dt, has_time = parsed
            return dt, date_match.group("date"), has_time
    return None


def clean_label_value(value: str) -> str:
    return value.strip(PUNCTUATION_TRIM)


def chinese_number(value: str) -> int | None:
    """Small Chinese integer converter, sufficient for lot numbers."""

    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    value = value.strip()
    if not value or any(ch not in digits and ch not in "十百" for ch in value):
        return None
    if value == "十":
        return 10
    total = 0
    current = 0
    for ch in value:
        if ch in digits:
            current = digits[ch]
        elif ch == "十":
            total += (current or 1) * 10
            current = 0
        elif ch == "百":
            total += (current or 1) * 100
            current = 0
    return total + current


def canonical_project(value: str | None) -> str:
    if not value:
        return ""
    text = normalize_width(value).upper()
    text = re.sub(r"\s+", "", text)
    return text.strip("，。；;、：")


def canonical_section(value: str | None) -> str:
    if not value:
        return ""
    text = normalize_width(value).upper()
    text = re.sub(r"\s+", "", text)
    text = text.strip("，。；;、：:()（）")
    text = re.sub(r"^第", "", text)
    text = re.sub(r"^(?:标段|标包|分包|合同包|包)", "", text)
    text = re.sub(r"(?:标段|标包|分包|合同包|包|号|分标)$", "", text)
    text = text.strip("，。；;、：:()（）")
    if not text:
        return ""
    number = chinese_number(text)
    if number is not None:
        return str(number)
    if text.isdigit():
        return str(int(text))
    return text


def section_matches(target: str, candidates: Iterable[str]) -> bool:
    wanted = canonical_section(target)
    if not wanted:
        return False
    for candidate in candidates:
        if not candidate:
            continue
        for part in re.split(r"[、,，;；|]+", normalize_width(candidate)):
            normalized = canonical_section(part)
            if normalized == wanted:
                return True
            # Some sites print a fully-qualified lot id such as
            # DEMO-2026-001-01 in the lot field.
            pieces = re.split(r"[-/]", normalized)
            if len(pieces) > 1 and wanted in pieces:
                return True
    return False


def project_matches(target: str, candidates: Iterable[str]) -> bool:
    wanted = canonical_project(target)
    for candidate in candidates:
        if canonical_project(candidate) == wanted:
            return True
    return False


# ---------------------------------------------------------------------------
# HTML field extraction
# ---------------------------------------------------------------------------


def _article(soup: BeautifulSoup) -> Tag:
    for selector in ("article.notice-detail", "article", "main", "body"):
        found = soup.select_one(selector)
        if found is not None:
            return found
    return soup  # type: ignore[return-value]


def _data_attr(soup: BeautifulSoup, names: Sequence[str]) -> str | None:
    for name in names:
        for tag in soup.find_all(attrs={name: True}):
            value = tag.get(name)
            if value:
                return str(value).strip()
    return None


def visible_text(soup: BeautifulSoup) -> str:
    container = _article(soup)
    lines = []
    for raw_line in container.get_text("\n").splitlines():
        line = normalize_space(raw_line)
        if line:
            lines.append(line)
    return "\n".join(lines)


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1 is not None:
        title = normalize_space(h1.get_text(" ", strip=True))
        if title:
            return title
    if soup.title is not None:
        return normalize_space(soup.title.get_text(" ", strip=True))
    return ""


def _project_candidates(container: Tag, text: str, title: str) -> list[str]:
    explicit: list[str] = []
    attr = container.get("data-project-no") if hasattr(container, "get") else None
    if attr:
        explicit.append(str(attr))
    for tag in container.find_all(attrs={"data-project-no": True}):
        value = tag.get("data-project-no")
        if value:
            explicit.append(str(value))
    if explicit:
        return list(dict.fromkeys(explicit))

    candidates: list[str] = []
    for meta in container.find_all("meta"):
        name = str(meta.get("name") or meta.get("property") or "").lower()
        if "project" in name and ("number" in name or "no" in name) and meta.get("content"):
            candidates.append(str(meta.get("content")))
    for match in PROJECT_LABEL_RE.finditer(text):
        candidates.append(clean_label_value(match.group(1)))
    # A title often has "项目编号：..." but detail meta usually covers it.
    for match in PROJECT_LABEL_RE.finditer(title):
        candidates.append(clean_label_value(match.group(1)))
    return [value for value in dict.fromkeys(candidates) if value]


def _section_candidates(container: Tag, text: str, title: str) -> list[str]:
    explicit: list[str] = []
    for attr_name in ("data-section-no", "data-section", "data-lot", "data-package-no"):
        value = container.get(attr_name)
        if value:
            explicit.append(str(value))
    for tag in container.find_all(attrs={"data-section-no": True}):
        value = tag.get("data-section-no")
        if value:
            explicit.append(str(value))
    for attr_name in ("data-section", "data-lot", "data-package-no"):
        for tag in container.find_all(attrs={attr_name: True}):
            value = tag.get(attr_name)
            if value:
                explicit.append(str(value))
    # A structured lot field is authoritative.  Body text often mentions
    # sibling lots, and those references must not widen the match.
    if explicit:
        return list(dict.fromkeys(explicit))

    candidates: list[str] = []
    for source in (text, title):
        for match in SECTION_LABEL_RE.finditer(source):
            candidates.append(clean_label_value(match.group(1)))
        for match in SECTION_INLINE_RE.finditer(source):
            candidates.append(clean_label_value(match.group(1)))
    return [value for value in dict.fromkeys(candidates) if value]


DEADLINE_LABEL_RE = re.compile(
    r"(?:"
    r"电子投标文件(?:递交|提交|上传|接收)?(?:的)?截止时间|"
    r"投标文件(?:递交|提交|上传|接收)?(?:的)?截止时间|"
    r"(?:递交|提交|上传|接收)投标文件(?:的)?截止时间|"
    r"投标截止时间|投标截止|"
    r"响应文件(?:递交|提交|上传|接收)?(?:的)?截止时间|"
    r"(?:递交|提交|上传|接收)响应文件(?:的)?截止时间|"
    r"报价截止时间|"
    r"磋商文件(?:递交|提交)?(?:的)?截止时间|"
    r"谈判文件(?:递交|提交)?(?:的)?截止时间|"
    r"询价文件(?:递交|提交)?(?:的)?截止时间|"
    r"申请文件(?:递交|提交)?(?:的)?截止时间|"
    r"截标时间|"
    r"投标文件接收截止时间"
    r")"
)
OPEN_TIME_LABEL_RE = re.compile(r"(?:开标时间|开标日期|开标)")
GENERIC_DEADLINE_RE = re.compile(r"截止时间")
POSITIVE_MARKER_RE = re.compile(
    r"(?:变更后|延期后|调整后|更正后|最新|新的|现变更为|变更为|修改为|"
    r"更正为|调整为|延期至|延长至|顺延至|推迟至|更新为|改为|延至|推后至|"
    r"现为|现定于|确定为)"
)
OLD_MARKER_RE = re.compile(r"(?:原投标|原响应|原定|原计划|原先|此前|之前|曾经|曾|由)")
POSTPONE_RE = re.compile(r"(?:延期|推迟|顺延|延长|暂缓|另行通知|另行公告|另行确定|待定)")


def _deadline_label_candidates(text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for label_match in DEADLINE_LABEL_RE.finditer(text):
        segment = text[label_match.end() : label_match.end() + 140]
        date_match = DATE_RE.search(segment)
        if not date_match:
            continue
        between = segment[: date_match.start()]
        after = segment[date_match.end() : date_match.end() + 50]
        label = label_match.group(0)

        # Do not let a deadline label consume a later, unrelated date (for
        # example "投标截止时间不变，获取文件截止时间为……").  A field value
        # must be adjacent to its own label.
        if len(between) > 60:
            continue
        if re.search(
            r"(?:截止时间|获取|领取|报名|答疑|质疑|澄清|开标|评审|公示|"
            r"不变|未变更|不调整|不延期|保持不变|另行|详见|待定)",
            between,
        ):
            continue

        prefix = text[max(0, label_match.start() - 12) : label_match.start()]
        old = (
            bool(re.search(r"(?:原定|原计划|原先|此前|之前|曾)\s*(?:的|于)?\s*$", prefix))
            or prefix.rstrip().endswith("原")
            or bool(re.search(r"(?:原投标|原响应|原定|原计划|原先|此前|之前|曾|由)", between))
        )
        positive = bool(POSITIVE_MARKER_RE.search(label + between))
        old = old and not positive
        ambiguous = bool(re.search(r"(?:另行通知|另行公告|另行确定|待定)", after))
        parsed = parse_datetime(date_match.group("date"))
        if not parsed:
            continue
        dt, has_time = parsed
        results.append(
            {
                "dt": dt,
                "raw": date_match.group("date"),
                "has_time": has_time,
                "old": old,
                "positive": positive,
                "ambiguous": ambiguous,
                "label": label,
                "position": label_match.start(),
            }
        )
    return results


_MOD_DATE_RE = re.compile(
    r"(?P<marker>(?:变更|修改|调整|更正|延期|延长|顺延|推迟|更新|延至|推后)"
    r"[^。\n]{0,12}?(?:为|至|到|成))"
    r"\s*[：:]?\s*(?P<date>" + _DATE_CORE + r")"
)


def _marker_deadline_candidates(text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for match in _MOD_DATE_RE.finditer(text):
        # Stay on the sentence/line that contains the modification.  Public
        # sites frequently change the opening time on the same page; that
        # must not be mistaken for a change to the submission deadline.
        line_start = text.rfind("\n", 0, match.start()) + 1
        window = text[line_start : match.start()]
        deadline_labels = list(DEADLINE_LABEL_RE.finditer(window))
        if not deadline_labels:
            continue
        open_labels = list(OPEN_TIME_LABEL_RE.finditer(window))
        if open_labels and open_labels[-1].start() > deadline_labels[-1].start():
            continue
        parsed = parse_datetime(match.group("date"))
        if not parsed:
            continue
        dt, has_time = parsed
        results.append(
            {
                "dt": dt,
                "raw": match.group("date"),
                "has_time": has_time,
                "marker": match.group("marker"),
                "position": match.start(),
            }
        )
    return results


@dataclass
class DeadlineInfo:
    value: datetime | None = None
    raw: str | None = None
    has_time: bool = True
    explicit: bool = False
    unclear: bool = False
    quoted_old: datetime | None = None
    quoted_old_raw: str | None = None


def extract_deadline(
    text: str,
    notice_type: str,
    is_postponement: bool,
    data_value: str | None = None,
) -> DeadlineInfo:
    if data_value:
        parsed = parse_datetime(data_value)
        if parsed:
            return DeadlineInfo(value=parsed[0], raw=data_value, has_time=parsed[1], explicit=True)

    label_candidates = _deadline_label_candidates(text)
    marker_candidates = _marker_deadline_candidates(text)

    if notice_type == "extension":
        if marker_candidates:
            chosen = marker_candidates[-1]
            return DeadlineInfo(
                value=chosen["dt"],
                raw=chosen["raw"],
                has_time=chosen["has_time"],
                explicit=True,
            )
        current = [
            item for item in label_candidates if not item["old"] and not item["ambiguous"]
        ]
        if current:
            chosen = current[-1]
            return DeadlineInfo(
                value=chosen["dt"],
                raw=chosen["raw"],
                has_time=chosen["has_time"],
                explicit=True,
            )
        old = next((item for item in label_candidates if item["old"]), None)
        if old is None and label_candidates:
            old = label_candidates[-1]
        unclear = bool(
            is_postponement
            or POSTPONE_RE.search(text[-300:])
            or any(item["ambiguous"] for item in label_candidates)
        )
        return DeadlineInfo(
            value=None,
            quoted_old=old["dt"] if old else None,
            quoted_old_raw=old["raw"] if old else None,
            explicit=False,
            unclear=unclear,
        )

    current = [item for item in label_candidates if not item["old"]]
    if not current:
        current = label_candidates
    if current:
        chosen = current[0]
        return DeadlineInfo(
            value=chosen["dt"],
            raw=chosen["raw"],
            has_time=chosen["has_time"],
            explicit=True,
        )
    return DeadlineInfo()


NOTICE_TYPE_TERMINATION = re.compile(r"(?:终止公告|终止招标|终止采购|采购终止|项目终止|废标|流标)")
NOTICE_TYPE_EXTENSION = re.compile(r"(?:延期公告|延期通知|延长公告|推迟公告|顺延公告|变更公告|更正公告|澄清修改|修改公告|补充公告)")
NOTICE_TYPE_RESULT = re.compile(r"(?:中标(?:结果|候选人)?公告|成交(?:结果)?公告|结果公告|采购结果|评标结果)")
NOTICE_TYPE_ORIGINAL = re.compile(
    r"(?:原公告|招标公告|采购公告|竞争性磋商公告|竞争性谈判公告|询价公告|"
    r"单一来源采购公告|比选公告|竞价公告|谈判公告|磋商公告|采购邀请|谈判邀请)"
)


def classify_notice(title: str, text: str, explicit_type: str | None = None) -> tuple[str, bool]:
    explicit = normalize_space(explicit_type or "")
    head = "\n".join(text.splitlines()[:12])
    type_haystack = f"{explicit}\n{title}\n{head}"
    if NOTICE_TYPE_TERMINATION.search(type_haystack) or re.search(
        r"决定终止|终止本次|终止本项目|本项目终止|采购活动终止", text
    ):
        return "terminated", False
    if NOTICE_TYPE_EXTENSION.search(type_haystack) or re.search(
        r"延期公告|推迟公告|顺延公告|更正公告|变更公告", text[:800]
    ):
        is_postponement = bool(re.search(r"(?:延期|推迟|顺延|延长|暂缓)", type_haystack))
        return "extension", is_postponement
    if NOTICE_TYPE_RESULT.search(type_haystack):
        return "result", False
    if NOTICE_TYPE_ORIGINAL.search(type_haystack):
        return "original", False
    if re.search(r"投标截止时间|响应文件提交截止时间|报价截止时间", text):
        return "original", False
    return "other", False


@dataclass
class ParsedNotice:
    url: str
    title: str
    project_candidates: list[str]
    section_candidates: list[str]
    published_at: datetime | None
    published_raw: str | None
    published_has_time: bool
    notice_type: str
    is_postponement: bool
    deadline: DeadlineInfo
    text: str


def parse_notice_html(html: str, url: str, date_hint: str | None = None) -> ParsedNotice:
    soup = BeautifulSoup(html, "html.parser")
    container = _article(soup)
    text = visible_text(soup)
    title = page_title(soup)

    project_candidates = _project_candidates(container, text, title)
    section_candidates = _section_candidates(container, text, title)

    published = parse_date_after_label(text, PUBLISH_LABEL_RE)
    if published is None and date_hint:
        match = DATE_RE.search(date_hint)
        if match:
            parsed = parse_datetime(match.group("date"))
            if parsed:
                published = (parsed[0], match.group("date"), parsed[1])
    if published is None:
        attr = _data_attr(soup, ("data-publish-time", "data-published-at", "data-publish-date"))
        if attr:
            parsed = parse_datetime(attr)
            if parsed:
                published = (parsed[0], attr, parsed[1])
    # Prefer explicit publication metadata when available.
    attr = _data_attr(soup, ("data-publish-time", "data-published-at", "data-publish-date"))
    if attr:
        parsed = parse_datetime(attr)
        if parsed:
            published = (parsed[0], attr, parsed[1])

    explicit_type = _data_attr(soup, ("data-notice-type", "data-announcement-type", "data-type"))
    if not explicit_type:
        type_label = re.search(r"(?:公告类型|公告性质|信息类型)\s*(?:为|是)?\s*[：:]\s*([^\n]{1,30})", text)
        if type_label:
            explicit_type = type_label.group(1)

    notice_type, is_postponement = classify_notice(title, text, explicit_type)
    if explicit_type:
        normalized_type = normalize_space(explicit_type).lower()
        aliases = {
            "original": "original",
            "extension": "extension",
            "terminated": "terminated",
            "termination": "terminated",
            "result": "result",
        }
        if normalized_type in aliases:
            notice_type = aliases[normalized_type]
            if notice_type == "extension":
                is_postponement = bool(re.search(r"延期|推迟|顺延|延长", text))

    deadline_data = _data_attr(
        soup, ("data-deadline", "data-bid-deadline", "data-deadline-time")
    )
    deadline = extract_deadline(text, notice_type, is_postponement, deadline_data)

    return ParsedNotice(
        url=url,
        title=title,
        project_candidates=project_candidates,
        section_candidates=section_candidates,
        published_at=published[0] if published else None,
        published_raw=published[1] if published else None,
        published_has_time=published[2] if published else False,
        notice_type=notice_type,
        is_postponement=is_postponement,
        deadline=deadline,
        text=text,
    )


# ---------------------------------------------------------------------------
# Link discovery
# ---------------------------------------------------------------------------

NAV_WORDS = {
    "首页", "返回", "返回首页", "返回列表", "登录", "注册", "帮助", "关于",
    "联系我们", "网站地图", "打印", "关闭", "更多", "详情", "网站首页",
}
PAGER_WORDS = {"下一页", "下页", "下一頁", "后一页", "后页", "›", "»", ">", "更多"}
PAGER_CONTAINER_RE = re.compile(r"(?:pagination|pager|page-nav|pagebar|pages|分页)", re.I)


def normalize_url(raw: str) -> str:
    value = normalize_width(raw).strip()
    if not value:
        raise ValueError("URL 不能为空")
    if value.startswith("//"):
        value = "http:" + value
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("只支持 http 或 https 地址")
    if not parts.netloc:
        raise ValueError("URL 缺少主机名")
    if parts.username or parts.password:
        raise ValueError("URL 不能包含用户名或密码")
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _same_origin(a: str, b: str) -> bool:
    return _origin(a) == _origin(b)


def _link_text(anchor: Tag) -> str:
    text = normalize_space(anchor.get_text(" ", strip=True))
    if text:
        return text
    return normalize_space(str(anchor.get("aria-label") or anchor.get("title") or ""))


def _in_pager(anchor: Tag) -> bool:
    node: Tag | None = anchor
    for _ in range(4):
        if node is None:
            break
        marker = " ".join(
            str(value)
            for value in (node.get("class") or [], node.get("id") or "", node.get("role") or "")
        )
        if PAGER_CONTAINER_RE.search(marker):
            return True
        node = node.parent
    return False


@dataclass
class LinkSet:
    pagination: list[str] = field(default_factory=list)
    details: list[Tag] = field(default_factory=list)
    detail_urls: list[str] = field(default_factory=list)


def extract_links(html: str, page_url: str) -> LinkSet:
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    base_url = urljoin(page_url, str(base_tag.get("href"))) if base_tag else page_url

    links = LinkSet()
    all_anchors = soup.find_all("a", href=True)
    pagination_set: set[str] = set()

    for anchor in all_anchors:
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        try:
            absolute = normalize_url(absolute)
        except ValueError:
            continue
        if not _same_origin(absolute, page_url):
            continue
        text = _link_text(anchor)
        rel = " ".join(anchor.get("rel") or []).lower()
        is_next = "next" in rel or text in PAGER_WORDS or _in_pager(anchor)
        if not is_next:
            # Numeric links in a pager container are page links.
            if _in_pager(anchor) and re.fullmatch(r"\d+", text):
                is_next = True
        if is_next:
            pagination_set.add(absolute)
            if absolute not in links.pagination:
                links.pagination.append(absolute)

    # Prefer explicitly marked notice links when the site provides them, so
    # header/footer navigation is never mistaken for an announcement.
    marked = soup.select("a.notice-link, a[data-notice-url], a[data-project-no]")
    if marked:
        anchors_for_details = marked
    else:
        main = soup.find("main") or soup.find(id="content") or soup.body or soup
        anchors_for_details = main.find_all("a", href=True) if hasattr(main, "find_all") else []

    for anchor in anchors_for_details:
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = _link_text(anchor)
        absolute = urljoin(base_url, href)
        try:
            absolute = normalize_url(absolute)
        except ValueError:
            continue
        if not _same_origin(absolute, page_url):
            continue
        if absolute in pagination_set or _in_pager(anchor):
            continue
        if "next" in " ".join(anchor.get("rel") or []).lower():
            continue
        if text in NAV_WORDS or text in PAGER_WORDS:
            continue
        lower_path = urlsplit(absolute).path.lower()
        if lower_path.endswith((".css", ".js", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".pdf", ".zip")):
            continue
        if absolute == normalize_url(page_url):
            continue
        if absolute not in links.detail_urls:
            links.detail_urls.append(absolute)
            links.details.append(anchor)
    return links


def _context_text(anchor: Tag) -> str:
    node = anchor
    for _ in range(3):
        if node.parent is None:
            break
        node = node.parent
        if node.name in {"tr", "li", "article", "div"}:
            text = normalize_space(node.get_text(" ", strip=True))
            if text:
                return text
    return normalize_space(anchor.parent.get_text(" ", strip=True)) if anchor.parent else ""


def _hints_for_anchor(anchor: Tag, url: str) -> tuple[str | None, str | None, str | None]:
    project_hint = str(anchor.get("data-project-no") or "") or None
    section_hint = str(anchor.get("data-section-no") or "") or None
    context = _context_text(anchor)
    if not project_hint:
        match = PROJECT_LABEL_RE.search(context)
        if match:
            project_hint = clean_label_value(match.group(1))
            if len(project_hint) > 60:
                project_hint = None
    if not section_hint:
        match = SECTION_LABEL_RE.search(context)
        if match:
            section_hint = clean_label_value(match.group(1))
    date_hint = None
    date_match = DATE_RE.search(context)
    if date_match:
        date_hint = date_match.group("date")
    return project_hint, section_hint, date_hint


# ---------------------------------------------------------------------------
# Fetching and reconciliation
# ---------------------------------------------------------------------------


@dataclass
class FetchIssue:
    url: str
    reason: str
    stage: str

    def as_dict(self) -> dict[str, str]:
        return {"url": self.url, "reason": self.reason, "stage": self.stage}


@dataclass
class NoticeRecord:
    parsed: ParsedNotice
    order: int
    list_page: str
    source_url: str

    @property
    def published_at(self) -> datetime | None:
        return self.parsed.published_at


@dataclass
class CheckOutcome:
    list_url: str
    project_no: str
    section_no: str
    status_code: str
    status_label: str
    determinable: bool
    effective_deadline: datetime | None
    effective_deadline_raw: str | None
    effective_deadline_has_time: bool
    deadline_source_url: str | None
    deadline_source_title: str | None
    reason: str
    notices: list[NoticeRecord]
    changes: list[dict[str, Any]]
    issues: list[FetchIssue]
    searched_pages: list[str]
    checked_detail_urls: list[str]
    detail_page_count: int
    warnings: list[str]

    def as_dict(self, public_url_fn=None) -> dict[str, Any]:
        def pub(url: str | None) -> str | None:
            if not url:
                return None
            return public_url_fn(url) if public_url_fn else url

        notices = []
        for record in self.notices:
            parsed = record.parsed
            notices.append(
                {
                    "url": pub(record.source_url),
                    "raw_url": record.source_url,
                    "title": parsed.title,
                    "project_no": parsed.project_candidates[0] if parsed.project_candidates else None,
                    "section_no": parsed.section_candidates[0] if parsed.section_candidates else None,
                    "published_at": format_dt(parsed.published_at, parsed.published_has_time),
                    "published_raw": parsed.published_raw,
                    "notice_type": parsed.notice_type,
                    "notice_type_label": TYPE_LABELS.get(parsed.notice_type, "公告"),
                    "deadline": format_dt(parsed.deadline.value, parsed.deadline.has_time),
                    "deadline_raw": parsed.deadline.raw,
                    "deadline_explicit": parsed.deadline.explicit,
                }
            )
        changes = []
        for change in self.changes:
            copied = dict(change)
            copied["url"] = pub(change.get("url"))
            copied["raw_url"] = change.get("url")
            changes.append(copied)
        return {
            "input_url": self.list_url,
            "project_no": self.project_no,
            "section_no": self.section_no,
            "status": self.status_code,
            "status_label": self.status_label,
            "determinable": self.determinable,
            "effective_deadline": format_dt(
                self.effective_deadline, include_time=self.effective_deadline_has_time
            ),
            "effective_deadline_has_time": self.effective_deadline_has_time,
            "effective_deadline_raw": self.effective_deadline_raw,
            "deadline_source_url": pub(self.deadline_source_url),
            "deadline_source_raw_url": self.deadline_source_url,
            "deadline_source_title": self.deadline_source_title,
            "reason": self.reason,
            "notices": notices,
            "changes": changes,
            "failures": [issue.as_dict() | {"url": pub(issue.url) or issue.url} for issue in self.issues],
            "searched_pages": [pub(url) or url for url in self.searched_pages],
            "detail_page_count": self.detail_page_count,
            "warnings": self.warnings,
        }


TYPE_LABELS = {
    "original": "原公告",
    "extension": "延期公告",
    "terminated": "终止公告",
    "result": "结果公告",
    "other": "其他公告",
}


async def _fetch(
    client: httpx.AsyncClient, url: str, stage: str
) -> tuple[str | None, str | None, str]:
    try:
        response = await client.get(url)
        final_url = str(response.url)
        if response.status_code < 200 or response.status_code >= 300:
            return None, f"HTTP {response.status_code}".strip(), final_url
        content_type = response.headers.get("content-type", "").lower()
        if content_type and not any(
            allowed in content_type for allowed in ("text/html", "application/xhtml+xml", "text/plain")
        ):
            return None, f"不是 HTML 页面（{content_type.split(';')[0]}）", final_url
        if len(response.content) > MAX_RESPONSE_BYTES:
            return None, "页面超过大小限制", final_url
        return response.text, None, final_url
    except httpx.TimeoutException:
        return None, "请求超时", url
    except httpx.HTTPError as exc:
        return None, f"网络请求失败：{exc.__class__.__name__}", url
    except Exception as exc:  # defensive: never let one bad page abort a check
        return None, f"读取页面失败：{exc.__class__.__name__}", url


def _build_changes(records: Sequence[NoticeRecord], original: NoticeRecord | None) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    effective: datetime | None = original.parsed.deadline.value if original else None
    effective_has_time = original.parsed.deadline.has_time if original else True
    for record in records:
        parsed = record.parsed
        if parsed.notice_type not in {"original", "extension", "terminated"}:
            continue
        type_label = TYPE_LABELS.get(parsed.notice_type, "公告")
        summary = ""
        deadline_after: datetime | None = effective if parsed.notice_type != "terminated" else None
        deadline_after_has_time = effective_has_time
        if parsed.notice_type == "original":
            initial = parsed.deadline.value
            effective = initial
            effective_has_time = parsed.deadline.has_time
            summary = (
                f"发布原公告，投标截止时间为 "
                f"{format_dt(initial, include_time=parsed.deadline.has_time) or '未明确'}。"
            )
        elif parsed.notice_type == "extension":
            if parsed.deadline.explicit and parsed.deadline.value:
                before = effective
                before_has_time = effective_has_time
                effective = parsed.deadline.value
                effective_has_time = parsed.deadline.has_time
                if before:
                    summary = (
                        f"截止时间由 {format_dt(before, include_time=before_has_time)} 调整为 "
                        f"{format_dt(parsed.deadline.value, include_time=parsed.deadline.has_time)}。"
                    )
                else:
                    summary = (
                        "延期公告将截止时间明确为 "
                        f"{format_dt(parsed.deadline.value, include_time=parsed.deadline.has_time)}。"
                    )
                deadline_after = effective
                deadline_after_has_time = effective_has_time
            elif parsed.deadline.unclear:
                summary = "延期公告未明确新的截止时间；原截止时间不能直接视为有效。"
                deadline_after = None
            else:
                summary = "该公告未明确修改投标截止时间。"
                deadline_after = effective
        else:
            summary = "项目终止，投标截止时间不再有效，不应继续按原时间准备投标。"
            deadline_after = None
        changes.append(
            {
                "published_at": format_dt(parsed.published_at, parsed.published_has_time),
                "published_raw": parsed.published_raw,
                "type": parsed.notice_type,
                "type_label": type_label,
                "title": parsed.title,
                "summary": summary,
                "url": record.source_url,
                "deadline_after": format_dt(
                    deadline_after, include_time=deadline_after_has_time
                ),
                "deadline_raw_after": parsed.deadline.raw if parsed.deadline.explicit else None,
            }
        )
    return changes


def _problem(
    issues: list[FetchIssue],
    url: str,
    reason: str,
    stage: str = "validation",
) -> None:
    issues.append(FetchIssue(url=url, reason=reason, stage=stage))


async def check_lot(
    list_url: str,
    project_no: str,
    section_no: str,
    *,
    client_factory=None,
) -> CheckOutcome:
    """Crawl paginated listings and return a conservative reconciliation."""

    try:
        start_url = normalize_url(list_url)
    except ValueError as exc:
        return CheckOutcome(
            list_url=list_url,
            project_no=project_no,
            section_no=section_no,
            status_code="unknown",
            status_label="无法判定",
            determinable=False,
            effective_deadline=None,
            effective_deadline_raw=None,
            effective_deadline_has_time=False,
            deadline_source_url=None,
            deadline_source_title=None,
            reason=str(exc),
            notices=[],
            changes=[],
            issues=[FetchIssue(url=list_url, reason=str(exc), stage="validation")],
            searched_pages=[],
            checked_detail_urls=[],
            detail_page_count=0,
            warnings=[],
        )

    project_no = normalize_space(project_no)
    section_no = normalize_space(section_no)
    issues: list[FetchIssue] = []
    warnings: list[str] = []
    searched_pages: list[str] = []
    checked_detail_urls: list[str] = []
    relevant: list[NoticeRecord] = []
    seen_pages: set[str] = set()
    seen_details: set[str] = set()
    queue: list[str] = [start_url]
    detail_count = 0
    incomplete = False
    order = 0

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
                warnings.append(f"公告列表超过 {MAX_LIST_PAGES} 页，已停止继续翻页。")
                break
            page_url = queue.pop(0)
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            searched_pages.append(page_url)

            html, error, final_page_url = await _fetch(client, page_url, "list")
            if html is None:
                _problem(issues, page_url, f"公告列表页抓取失败：{error}", "list")
                continue
            if final_page_url != page_url:
                if final_page_url not in searched_pages:
                    searched_pages.append(final_page_url)
                seen_pages.add(final_page_url)

            try:
                links = extract_links(html, final_page_url)
            except Exception as exc:
                _problem(issues, page_url, f"公告列表页解析失败：{exc.__class__.__name__}", "list")
                continue

            for next_url in links.pagination:
                if next_url not in seen_pages and next_url not in queue:
                    queue.append(next_url)

            candidates: list[tuple[str, Tag | None]] = []
            for index, detail_url in enumerate(links.detail_urls):
                if detail_url in seen_details:
                    continue
                anchor = links.details[index] if index < len(links.details) else None
                hints = _hints_for_anchor(anchor, detail_url) if anchor is not None else (None, None, None)
                project_hint, section_hint, _date_hint = hints
                # Definitely another project or definitely another lot of the
                # same project: do not fetch or mix it in.
                if project_hint and not project_matches(project_no, [project_hint]):
                    continue
                if project_hint and section_hint and not section_matches(section_no, [section_hint]):
                    continue
                candidates.append((detail_url, anchor))

            if detail_count + len(candidates) > MAX_DETAIL_PAGES:
                incomplete = True
                warnings.append(f"公告详情超过 {MAX_DETAIL_PAGES} 页，已停止继续核对。")
                candidates = candidates[: max(0, MAX_DETAIL_PAGES - detail_count)]

            async def fetch_detail(detail_url: str, anchor: Tag | None):
                html, error, final_url = await _fetch(client, detail_url, "detail")
                return detail_url, anchor, html, error, final_url

            results = await asyncio.gather(
                *(fetch_detail(url, anchor) for url, anchor in candidates),
                return_exceptions=False,
            )

            for detail_url, anchor, detail_html, fetch_error, final_detail_url in results:
                if detail_url in seen_details:
                    continue
                seen_details.add(detail_url)
                checked_detail_urls.append(detail_url)
                detail_count += 1
                if detail_html is None:
                    _problem(issues, detail_url, f"公告详情页抓取失败：{fetch_error}", "detail")
                    continue
                source_url = final_detail_url or detail_url
                try:
                    hints = _hints_for_anchor(anchor, detail_url) if anchor is not None else (None, None, None)
                    project_hint, section_hint, date_hint = hints
                    parsed = parse_notice_html(detail_html, source_url, date_hint)
                except Exception as exc:
                    _problem(issues, detail_url, f"公告详情页解析失败：{exc.__class__.__name__}", "detail")
                    continue

                hint_says_relevant = bool(
                    project_hint
                    and project_matches(project_no, [project_hint])
                    and (not section_hint or section_matches(section_no, [section_hint]))
                )
                has_project = bool(parsed.project_candidates)
                has_section = bool(parsed.section_candidates)
                project_ok = project_matches(project_no, parsed.project_candidates)
                section_ok = section_matches(section_no, parsed.section_candidates)

                if hint_says_relevant and not has_project:
                    _problem(
                        issues,
                        detail_url,
                        "候选公告页未解析出项目编号，无法确认该页内容",
                        "detail",
                    )
                    continue
                if hint_says_relevant and not has_section:
                    _problem(
                        issues,
                        detail_url,
                        "候选公告页未解析出标段号，无法确认它是否属于目标标段",
                        "detail",
                    )
                    continue
                if not project_ok and hint_says_relevant:
                    _problem(
                        issues,
                        detail_url,
                        "候选公告页的项目编号与列表信息不一致",
                        "detail",
                    )
                    continue
                if not project_ok:
                    continue
                if not section_ok:
                    # Another lot of the same project: explicitly excluded.
                    continue

                order += 1
                relevant.append(
                    NoticeRecord(
                        parsed=parsed,
                        order=order,
                        list_page=page_url,
                        source_url=source_url,
                    )
                )
    finally:
        if owns_client:
            await client.aclose()

    # ----- Merge in publication order ------------------------------------
    original: NoticeRecord | None = None
    termination: NoticeRecord | None = None
    ordered: list[NoticeRecord] = []
    missing_pub = [record for record in relevant if record.published_at is None]
    if missing_pub:
        if len(relevant) > 1:
            _problem(
                issues,
                missing_pub[0].source_url,
                "有相关公告缺少发布时间，无法按发布时间确定变更顺序",
                "parse",
            )
        else:
            warnings.append("该公告未提供发布时间。")

    if relevant:
        if len(missing_pub) and len(relevant) > 1:
            ordered = sorted(
                relevant,
                key=lambda record: (record.published_at or datetime.min, record.order),
            )
        else:
            ordered = sorted(
                relevant,
                key=lambda record: (record.published_at or datetime.min, record.order),
            )
        originals = [record for record in ordered if record.parsed.notice_type == "original"]
        if originals:
            original = originals[0]
            if len(originals) > 1:
                warnings.append("找到多份原公告，已采用发布时间最早的一份。")
            if original.parsed.deadline.value is None:
                _problem(
                    issues,
                    original.source_url,
                    "原公告未提供明确的投标截止时间，无法建立有效时间基线",
                    "parse",
                )
        else:
            _problem(
                issues,
                start_url,
                "已检索列表及相关公告，但未找到该标段的原公告",
                "missing_original",
            )
        terminations = [record for record in ordered if record.parsed.notice_type == "terminated"]
        if terminations:
            termination = terminations[-1]
    else:
        # The specification requires an address when an original cannot be
        # established.  Point to the index that was exhaustively searched.
        if not issues:
            _problem(
                issues,
                start_url,
                "已检索公告列表，但未找到与该项目编号和标段号同时匹配的原公告",
                "missing_original",
            )

    changes = _build_changes(ordered, original)
    status_code = "unknown"
    status_label = "无法判定"
    effective: datetime | None = None
    effective_raw: str | None = None
    effective_has_time = True
    source_url: str | None = None
    source_title: str | None = None
    reason = ""

    if incomplete:
        reason = "公告未全部核对，不能给出确定结论。"
    elif issues:
        reason = issues[0].reason
    elif not relevant:
        reason = "未找到与该项目编号和标段号同时匹配的公告。"
    elif original is None:
        reason = "未找到原公告，无法建立截止时间基线。"
    elif original.parsed.deadline.value is None:
        reason = "原公告未提供明确的投标截止时间。"
    else:
        effective = original.parsed.deadline.value
        effective_raw = original.parsed.deadline.raw
        effective_has_time = original.parsed.deadline.has_time
        source_url = original.source_url
        source_title = original.parsed.title
        status_code = "ongoing"
        status_label = "进行中"
        extension_applied = False
        unclear_extension = False
        unclear_source: NoticeRecord | None = None
        terminated = False

        for record in ordered:
            if record is original:
                continue
            parsed = record.parsed
            if parsed.notice_type == "terminated":
                terminated = True
                effective = None
                effective_raw = None
                source_url = record.source_url
                source_title = parsed.title
                unclear_extension = False
                continue
            if terminated:
                continue
            if parsed.notice_type == "extension":
                if parsed.deadline.explicit and parsed.deadline.value is not None:
                    effective = parsed.deadline.value
                    effective_raw = parsed.deadline.raw
                    effective_has_time = parsed.deadline.has_time
                    source_url = record.source_url
                    source_title = parsed.title
                    extension_applied = True
                    unclear_extension = False
                elif parsed.deadline.unclear:
                    unclear_extension = True
                    unclear_source = record
                    effective = None
                    effective_raw = None
                    source_url = record.source_url
                    source_title = parsed.title

        if terminated:
            status_code = "terminated"
            status_label = "已终止（不再接受投标）"
            effective = None
            effective_raw = None
            reason = "项目已发布终止公告，原投标截止时间不再有效。"
        elif unclear_extension:
            status_code = "unknown"
            status_label = "无法判定"
            determinable = False
            reason = "延期公告未明确新的投标截止时间，不能沿用旧时间。"
            effective = None
            effective_raw = None
            source_url = None
            source_title = None
            if unclear_source is not None:
                _problem(
                    issues,
                    unclear_source.source_url,
                    "延期公告未给出新的明确截止时间，旧截止时间不能继续作为有效时间",
                    "parse",
                )
        elif extension_applied:
            status_code = "extended"
            status_label = "已延期（投标进行中）"
            reason = "已按发布时间应用明确的延期截止时间。"
        else:
            status_label = "进行中（投标进行中）"
            reason = "未发现终止公告，原公告的投标截止时间仍是当前有效时间。"

    determinable = (
        not incomplete
        and not issues
        and original is not None
        and status_code not in {"unknown"}
    )
    if status_code == "terminated" and not issues and not incomplete:
        determinable = True

    if status_code == "unknown":
        effective = None
        effective_raw = None
        source_url = None
        source_title = None

    return CheckOutcome(
        list_url=start_url,
        project_no=project_no,
        section_no=section_no,
        status_code=status_code,
        status_label=status_label,
        determinable=determinable,
        effective_deadline=effective,
        effective_deadline_raw=effective_raw,
        effective_deadline_has_time=effective_has_time,
        deadline_source_url=source_url,
        deadline_source_title=source_title,
        reason=reason,
        notices=relevant,
        changes=changes,
        issues=issues,
        searched_pages=searched_pages,
        checked_detail_urls=checked_detail_urls,
        detail_page_count=detail_count,
        warnings=warnings,
    )
