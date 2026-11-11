"""In-memory fixture for the offline demonstration Q&A board.

The clarification answers are deliberately spread across five list pages and
include the situations the checker must distinguish:

* a plain direct answer;
* an answer that only says "参见另一问题 Qxxx", including chains that cross
  list pages (Q002 -> Q003, Q011 -> Q003);
* a circular reference chain (Q004 -> Q007 -> Q008 -> Q004);
* questions whose answer field is empty;
* the same question number published twice with different direct answer
  bodies (Q012), which must be flagged for manual verification;
* questions belonging to another project or another lot of the same project,
  which must never be mixed into the target lot's result.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QANotice:
    slug: str
    project_no: str
    section_no: str
    question_no: str
    title: str
    published_at: str
    question: str
    answer: str  # empty string means the publisher has not answered yet
    project_name: str


QA_NOTICES: tuple[QANotice, ...] = (
    QANotice(
        slug="DEMO-2026-001-01-qa-q001",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q001",
        title="关于投标保证金金额的澄清问题",
        published_at="2026-11-01 09:00",
        question="本项目 01 标段投标保证金具体金额是多少？",
        answer="经采购人确认，01 标段投标保证金金额为人民币 5 万元，须从投标人基本账户转出。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q002",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q002",
        title="关于保证金缴纳形式的澄清问题",
        published_at="2026-11-01 09:10",
        question="投标保证金是否可以采用银行保函形式？",
        answer="本问题答复参见另一问题 Q003 的答复。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q005",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q005",
        title="关于付款节点的澄清问题",
        published_at="2026-11-01 09:20",
        question="合同付款节点是否可以结合供货批次进一步细化？",
        answer="",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q003",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q003",
        title="关于保证金担保形式的澄清问题",
        published_at="2026-11-02 10:00",
        question="保证金可以采用哪些担保形式？",
        answer=(
            "投标保证金可采用银行转账、银行保函或保证保险形式；采用保函或保证保险的，"
            "担保金额同样为人民币 5 万元，有效期不得短于投标有效期。"
        ),
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q004",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q004",
        title="关于联合体投标资质的澄清问题",
        published_at="2026-11-02 10:10",
        question="联合体投标时各方资质如何认定？",
        answer="关于联合体资质认定，答复内容请参见问题 Q007。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q006",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q006",
        title="关于质保期起算时间的澄清问题",
        published_at="2026-11-02 10:20",
        question="质保期是从到货验收还是整体竣工验收开始起算？",
        answer="",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q007",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q007",
        title="关于联合体协议提交要求的澄清问题",
        published_at="2026-11-03 14:00",
        question="联合体协议是否需要在投标文件中附原件？",
        answer="该事项请详见第 Q008 问的答复。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q008",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q008",
        title="关于联合体成员数量限制的澄清问题",
        published_at="2026-11-03 14:10",
        question="联合体成员数量上限是多少？",
        answer="请参见问题 Q004 的答复。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q009",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q009",
        title="关于类似业绩年限的澄清问题",
        published_at="2026-11-03 14:20",
        question="类似业绩要求的近三年是指哪三个自然年度？",
        answer="“近三年”指 2023、2024、2025 三个自然年度，以合同签订时间为准。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q010",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q010",
        title="关于现场踏勘安排的澄清问题",
        published_at="2026-11-04 11:00",
        question="是否组织现场踏勘？具体时间和集合地点？",
        answer="统一安排在 2026 年 11 月 5 日上午 9:30 现场踏勘，集合地点为滨河公共资源交易中心南门。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q011",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q011",
        title="关于保函有效期的澄清问题",
        published_at="2026-11-04 11:10",
        question="银行保函有效期能否只覆盖到投标截止当日？",
        answer="本问题与 Q003 属同一事项，具体答复参见另一问题 Q003。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q012",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q012",
        title="关于投标有效期的澄清问题（第一批答复）",
        published_at="2026-11-04 11:20",
        question="本项目投标有效期是多少天？",
        answer="投标有效期为自投标截止之日起 90 日历天。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-001-01-qa-q012-revised",
        project_no="DEMO-2026-001",
        section_no="01",
        question_no="Q012",
        title="关于投标有效期的澄清问题（修订答复）",
        published_at="2026-11-05 08:30",
        question="本项目投标有效期是多少天？",
        answer="投标有效期修订为自投标截止之日起 60 日历天，以此条答复为准。",
        project_name="滨河智慧园区设备采购项目",
    ),
    QANotice(
        slug="DEMO-2026-002-A-qa-q201",
        project_no="DEMO-2026-002",
        section_no="A",
        question_no="Q201",
        title="关于磋商保证金金额的澄清问题",
        published_at="2026-11-05 09:00",
        question="A 包磋商保证金金额是多少？",
        answer="A 包磋商保证金为人民币 2 万元，其他项目不适用本答复。",
        project_name="北岸道路照明提升项目",
    ),
    QANotice(
        slug="DEMO-2026-001-02-qa-q301",
        project_no="DEMO-2026-001",
        section_no="02",
        question_no="Q301",
        title="关于 02 标段供货范围的澄清问题",
        published_at="2026-11-05 09:30",
        question="02 标段是否包含核心交换机供货？",
        answer="02 标段供货清单包含核心交换机两台，详见答疑附件清单第 2 项。",
        project_name="滨河智慧园区设备采购项目",
    ),
)


QA_BY_SLUG = {notice.slug: notice for notice in QA_NOTICES}
QA_PAGE_SIZE = 3
QA_NOTICES_ASC = tuple(sorted(QA_NOTICES, key=lambda n: n.published_at))


def qa_page_count() -> int:
    return (len(QA_NOTICES_ASC) + QA_PAGE_SIZE - 1) // QA_PAGE_SIZE


def qa_notices_on_page(page: int) -> list[QANotice]:
    start = (page - 1) * QA_PAGE_SIZE
    return list(QA_NOTICES_ASC[start : start + QA_PAGE_SIZE])
