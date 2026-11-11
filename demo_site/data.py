"""In-memory fixture for the offline demonstration notice board.

The pages deliberately put the original notice and its follow-up notices on
different result pages.  A second lot of the same project is included so that
the checker's lot isolation can be demonstrated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Notice:
    slug: str
    project_no: str
    section_no: str
    notice_type: str  # original | extension | terminated | result | other
    title: str
    published_at: str
    deadline: str | None
    deadline_label: str | None
    body: tuple[str, ...]
    project_name: str


NOTICES: tuple[Notice, ...] = (
    Notice(
        slug="DEMO-2026-001-01-original",
        project_no="DEMO-2026-001",
        section_no="01",
        notice_type="original",
        title="滨河智慧园区设备采购项目（01标段）招标公告",
        published_at="2026-10-08 09:00",
        deadline="2026-11-02 09:30",
        deadline_label="投标截止时间",
        body=(
            "受采购人委托，现对滨河智慧园区设备采购项目（01标段）进行公开招标。",
            "项目编号：DEMO-2026-001；标段编号：01。",
            "投标截止时间：2026年11月02日 09:30（北京时间）。",
            "投标文件应在上述截止时间前递交至滨河公共资源交易中心。",
        ),
        project_name="滨河智慧园区设备采购项目",
    ),
    Notice(
        slug="DEMO-2026-001-02-original",
        project_no="DEMO-2026-001",
        section_no="02",
        notice_type="original",
        title="滨河智慧园区设备采购项目（02标段）招标公告",
        published_at="2026-10-09 10:00",
        deadline="2026-11-03 09:30",
        deadline_label="投标截止时间",
        body=(
            "受采购人委托，现对滨河智慧园区设备采购项目（02标段）进行公开招标。",
            "项目编号：DEMO-2026-001；标段编号：02。",
            "投标截止时间：2026年11月03日 09:30（北京时间）。",
            "本标段与 01 标段分别评审，投标人应按标段分别递交文件。",
        ),
        project_name="滨河智慧园区设备采购项目",
    ),
    Notice(
        slug="DEMO-2026-002-A-original",
        project_no="DEMO-2026-002",
        section_no="A",
        notice_type="original",
        title="北岸道路照明提升项目（A包）采购公告",
        published_at="2026-10-10 14:20",
        deadline="2026-11-20 14:00",
        deadline_label="响应文件提交截止时间",
        body=(
            "北岸道路照明提升项目（A包）采用竞争性磋商方式采购。",
            "项目编号：DEMO-2026-002；包号：A。",
            "响应文件提交截止时间：2026年11月20日 14:00。",
            "响应文件提交地点：北岸区公共资源交易中心三楼。",
        ),
        project_name="北岸道路照明提升项目",
    ),
    Notice(
        slug="DEMO-2026-001-01-extension",
        project_no="DEMO-2026-001",
        section_no="01",
        notice_type="extension",
        title="滨河智慧园区设备采购项目（01标段）延期公告",
        published_at="2026-10-18 16:00",
        deadline="2026-11-09 09:30",
        deadline_label="投标截止时间（延期后）",
        body=(
            "因答疑澄清，现对本项目 01 标段投标截止时间作如下延期。",
            "项目编号：DEMO-2026-001；标段编号：01。",
            "原投标截止时间为 2026年11月02日 09:30，现延期至：2026年11月09日 09:30。",
            "开标时间同步顺延至 2026年11月09日 09:30，开标地点不变。",
        ),
        project_name="滨河智慧园区设备采购项目",
    ),
    Notice(
        slug="DEMO-2026-001-02-extension",
        project_no="DEMO-2026-001",
        section_no="02",
        notice_type="extension",
        title="滨河智慧园区设备采购项目（02标段）延期公告",
        published_at="2026-10-20 11:00",
        deadline="2026-11-12 09:30",
        deadline_label="投标截止时间（延期后）",
        body=(
            "因投标人不足三家，现对 02 标段投标截止时间进行延期。",
            "项目编号：DEMO-2026-001；标段编号：02。",
            "原投标截止时间为 2026年11月03日 09:30，现变更为 2026年11月12日 09:30。",
            "其他内容不变。",
        ),
        project_name="滨河智慧园区设备采购项目",
    ),
    Notice(
        slug="DEMO-2026-003-B-original",
        project_no="DEMO-2026-003",
        section_no="B",
        notice_type="original",
        title="西岭绿化养护服务项目（B包）询价公告",
        published_at="2026-10-22 09:30",
        deadline="2026-12-02 10:00",
        deadline_label="报价截止时间",
        body=(
            "西岭绿化养护服务项目（B包）进行询价采购。",
            "项目编号：DEMO-2026-003；包号：B。",
            "报价截止时间：2026年12月02日 10:00。",
        ),
        project_name="西岭绿化养护服务项目",
    ),
    Notice(
        slug="DEMO-2026-002-A-extension",
        project_no="DEMO-2026-002",
        section_no="A",
        notice_type="extension",
        title="北岸道路照明提升项目（A包）延期公告",
        published_at="2026-10-26 15:30",
        deadline="2026-11-25 14:00",
        deadline_label="响应文件提交截止时间（变更后）",
        body=(
            "根据采购人通知，北岸道路照明提升项目 A 包响应文件提交截止时间调整如下。",
            "项目编号：DEMO-2026-002；包号：A。",
            "原响应文件提交截止时间为 2026年11月20日 14:00，现调整为 2026年11月25日 14:00。",
            "磋商时间相应调整为 2026年11月26日 09:30。",
        ),
        project_name="北岸道路照明提升项目",
    ),
    Notice(
        slug="DEMO-2026-001-01-terminated",
        project_no="DEMO-2026-001",
        section_no="01",
        notice_type="terminated",
        title="滨河智慧园区设备采购项目（01标段）终止公告",
        published_at="2026-10-28 10:00",
        deadline=None,
        deadline_label=None,
        body=(
            "因采购需求发生重大调整，经采购人研究决定，终止滨河智慧园区设备采购项目 01 标段本次采购活动。",
            "项目编号：DEMO-2026-001；标段编号：01。",
            "已递交的投标文件将按原渠道退还。后续采购安排另行公告。",
        ),
        project_name="滨河智慧园区设备采购项目",
    ),
    Notice(
        slug="DEMO-2026-002-A-result",
        project_no="DEMO-2026-002",
        section_no="A",
        notice_type="result",
        title="北岸道路照明提升项目（A包）成交结果公告",
        published_at="2026-10-29 09:00",
        deadline=None,
        deadline_label=None,
        body=(
            "北岸道路照明提升项目（A包）成交结果公告。",
            "项目编号：DEMO-2026-002；包号：A。",
            "本公告为结果公告，不改变响应文件提交截止时间。",
        ),
        project_name="北岸道路照明提升项目",
    ),
)


NOTICE_BY_SLUG = {notice.slug: notice for notice in NOTICES}
PAGE_SIZE = 3
NOTICES_ASC = tuple(sorted(NOTICES, key=lambda n: n.published_at))

TYPE_LABELS = {
    "original": "原公告",
    "extension": "延期公告",
    "terminated": "终止公告",
    "result": "结果公告",
    "other": "其他公告",
}


def page_count() -> int:
    return (len(NOTICES_ASC) + PAGE_SIZE - 1) // PAGE_SIZE


def notices_on_page(page: int) -> list[Notice]:
    start = (page - 1) * PAGE_SIZE
    return list(NOTICES_ASC[start : start + PAGE_SIZE])
