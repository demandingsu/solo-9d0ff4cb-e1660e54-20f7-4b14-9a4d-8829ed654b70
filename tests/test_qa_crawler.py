"""Regression tests for the clarification Q&A reconciliation rules.

Run with:  python -m unittest discover -s tests -v
These tests use httpx's in-process mock transport, so no network or demo
container is required.
"""

from __future__ import annotations

import unittest

import httpx

from app.qa_crawler import (
    canonical_question,
    check_qa,
    extract_reference,
    parse_qa_html,
    split_question_numbers,
)


def qa_detail(
    project: str,
    section: str,
    question: str,
    answer: str,
    title: str | None = None,
    published: str = "2026-11-01 09:00",
) -> str:
    return f"""
    <html><body><article class="qa-detail"
      data-project-no="{project}" data-section-no="{section}"
      data-question-no="{question}" data-publish-time="{published}">
      <h1>{title or '澄清问题 ' + question}</h1>
      <p>发布时间：{published}</p>
      <p>项目编号：{project}；标段编号：{section}；问题编号：{question}。</p>
      <h2>澄清问题</h2>
      <p class="qa-question">关于事项 {question} 的提问。</p>
      <h2>采购人答复</h2>
      <div data-qa-answer>
        {'<p>' + answer + '</p>' if answer else '<p>本问题暂未答复。</p>'}
      </div>
    </article></body></html>
    """


def qa_list(rows: list[tuple[str, str, str, str]], next_href: str | None = None) -> str:
    items = "".join(
        f'<li><a class="notice-link" data-project-no="{project}"'
        f' data-section-no="{section}" data-question-no="{question}"'
        f' href="{href}">答疑 {question}</a></li>'
        for project, section, question, href in rows
    )
    pager = f'<a rel="next" href="{next_href}">下一页</a>' if next_href else ""
    return f"<html><body><main><ul>{items}</ul>{pager}</main></body></html>"


class QAParseTests(unittest.TestCase):
    def test_reference_detection_variants(self):
        cases = {
            "本问题答复参见另一问题 Q003 的答复。": "Q003",
            "请参见问题Q007。": "Q007",
            "该事项请详见第 Q008 问的答复。": "Q008",
            "参见Q004": "Q004",
        }
        for text, expected in cases.items():
            self.assertEqual(extract_reference(text), expected)

    def test_substantive_answer_is_not_a_reference(self):
        text = "保证金可采用银行转账或保函形式，详见招标文件第 3 章附件。"
        self.assertIsNone(extract_reference(text))
        self.assertIsNone(extract_reference(""))

    def test_reference_with_connective_preamble(self):
        text = "本问题与 Q003 属同一事项，具体答复参见另一问题 Q003。"
        self.assertEqual(extract_reference(text), "Q003")

    def test_long_substantive_preamble_blocks_reference(self):
        text = (
            "经采购人研究，保证金可采用银行转账与保证保险两种形式，"
            "保险费率不超过百分之一，其他事项参见另一问题 Q003。"
        )
        self.assertIsNone(extract_reference(text))

    def test_parse_empty_answer(self):
        html = qa_detail("P-001", "01", "Q005", "")
        parsed = parse_qa_html(html, "http://test/qa/q005")
        self.assertEqual(parsed.question_no, "Q005")
        self.assertEqual(parsed.answer, "")
        self.assertIsNone(parsed.reference)

    def test_parse_empty_answer_drops_section_heading(self):
        html = """
        <html><body><article class="qa-detail"
          data-project-no="P-001" data-section-no="01" data-question-no="Q005">
          <h2>采购人答复</h2>
          <div data-qa-answer><h2>采购人答复</h2><p>本问题暂未答复。</p></div>
        </article></body></html>
        """
        parsed = parse_qa_html(html, "http://test/qa/q005")
        self.assertEqual(parsed.answer, "")

    def test_parse_answer_and_project_scope(self):
        html = qa_detail("P-001", "01", "q001", "保证金为 5 万元。")
        parsed = parse_qa_html(html, "http://test/qa/q001")
        self.assertEqual(parsed.question_no, "Q001")
        self.assertEqual(parsed.answer, "保证金为 5 万元。")
        self.assertIn("P-001", parsed.project_candidates)
        self.assertIn("01", parsed.section_candidates)

    def test_question_number_normalisation_and_split(self):
        self.assertEqual(canonical_question(" q012 "), "Q012")
        self.assertEqual(
            split_question_numbers("Q001, Q002；Q003、Q004\nQ005"),
            ["Q001", "Q002", "Q003", "Q004", "Q005"],
        )


class QACrawlerTests(unittest.IsolatedAsyncioTestCase):
    def make_factory(self, routes: dict[str, str], failing: set[str] | None = None):
        failing = failing or set()

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.query
            if isinstance(query, bytes):
                query = query.decode("ascii", errors="replace")
            key = request.url.path
            if query:
                key = f"{key}?{query}"
            if key in failing or request.url.path in failing:
                return httpx.Response(503, text="service unavailable")
            body = routes.get(key, routes.get(request.url.path))
            if body is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                200, text=body, headers={"content-type": "text/html"}
            )

        def factory():
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True
            )

        return factory

    async def _check(self, routes, questions, failing=None):
        return (
            await check_qa(
                "http://test/qa?page=1",
                "P-001",
                "01",
                questions,
                client_factory=self.make_factory(routes, failing),
            )
        ).as_dict()

    async def test_direct_answer_and_missing_question(self):
        routes = {
            "/qa": qa_list([("P-001", "01", "Q001", "/qa/q001")]),
            "/qa/q001": qa_detail("P-001", "01", "Q001", "保证金 5 万元。"),
        }
        result = await self._check(routes, ["Q001", "Q099"])
        by_no = {item["question_no"]: item for item in result["results"]}
        self.assertEqual(by_no["Q001"]["status"], "answered")
        self.assertEqual(by_no["Q001"]["answer"], "保证金 5 万元。")
        self.assertTrue(by_no["Q001"]["answer_source_url"].endswith("/qa/q001"))
        self.assertEqual(by_no["Q099"]["status"], "unanswered")
        self.assertFalse(result["round_incomplete"])

    async def test_empty_answer_is_unanswered(self):
        routes = {
            "/qa": qa_list([("P-001", "01", "Q005", "/qa/q005")]),
            "/qa/q005": qa_detail("P-001", "01", "Q005", ""),
        }
        result = await self._check(routes, ["Q005"])
        self.assertEqual(result["results"][0]["status"], "unanswered")
        self.assertIsNone(result["results"][0]["answer"])

    async def test_reference_chain_followed_across_pages(self):
        routes = {
            "/qa": qa_list(
                [("P-001", "01", "Q002", "/qa/q002")], next_href="/qa?page=2"
            ),
            "/qa?page=2": qa_list([("P-001", "01", "Q003", "/qa/q003")]),
            "/qa/q002": qa_detail(
                "P-001", "01", "Q002", "本问题答复参见另一问题 Q003 的答复。"
            ),
            "/qa/q003": qa_detail(
                "P-001", "01", "Q003", "可采用银行转账、银行保函或保证保险。"
            ),
        }
        result = await self._check(routes, ["Q002"])
        item = result["results"][0]
        self.assertEqual(item["status"], "answered")
        self.assertEqual(item["answer"], "可采用银行转账、银行保函或保证保险。")
        self.assertTrue(item["answer_source_url"].endswith("/qa/q003"))
        kinds = [hop["kind"] for hop in item["chain"]]
        self.assertEqual(kinds, ["direct", "reference"])
        self.assertEqual([hop["question_no"] for hop in item["chain"]], ["Q002", "Q003"])
        self.assertTrue(all(hop["url"] for hop in item["chain"]))
        self.assertEqual(len(result["searched_pages"]), 2)

    async def test_circular_reference_is_needs_review(self):
        routes = {
            "/qa": qa_list(
                [
                    ("P-001", "01", "Q004", "/qa/q004"),
                    ("P-001", "01", "Q007", "/qa/q007"),
                    ("P-001", "01", "Q008", "/qa/q008"),
                ]
            ),
            "/qa/q004": qa_detail("P-001", "01", "Q004", "请参见问题 Q007。"),
            "/qa/q007": qa_detail("P-001", "01", "Q007", "详见第 Q008 问的答复。"),
            "/qa/q008": qa_detail("P-001", "01", "Q008", "请参见问题 Q004 的答复。"),
        }
        result = await self._check(routes, ["Q004"])
        item = result["results"][0]
        self.assertEqual(item["status"], "needs_review")
        self.assertIsNone(item["answer"])
        self.assertTrue(any("循环引用" in reason for reason in item["reasons"]))

    async def test_dangling_reference_is_needs_review(self):
        routes = {
            "/qa": qa_list([("P-001", "01", "Q002", "/qa/q002")]),
            "/qa/q002": qa_detail("P-001", "01", "Q002", "参见另一问题 Q999。"),
        }
        result = await self._check(routes, ["Q002"])
        item = result["results"][0]
        self.assertEqual(item["status"], "needs_review")
        last_hop = item["chain"][-1]
        self.assertEqual(last_hop["question_no"], "Q999")
        self.assertIsNone(last_hop["url"])

    async def test_conflicting_direct_answers_is_needs_review(self):
        routes = {
            "/qa": qa_list(
                [
                    ("P-001", "01", "Q012", "/qa/q012-a"),
                    ("P-001", "01", "Q012", "/qa/q012-b"),
                ]
            ),
            "/qa/q012-a": qa_detail(
                "P-001", "01", "Q012", "投标有效期为 90 日历天。", published="2026-11-04 09:00"
            ),
            "/qa/q012-b": qa_detail(
                "P-001", "01", "Q012", "投标有效期修订为 60 日历天。", published="2026-11-05 09:00"
            ),
        }
        result = await self._check(routes, ["Q012"])
        item = result["results"][0]
        self.assertEqual(item["status"], "needs_review")
        self.assertEqual(item["record_count"], 2)
        self.assertTrue(any("不同的直接答复" in reason for reason in item["reasons"]))

    async def test_duplicate_identical_answers_stay_answered(self):
        body = "保证金为 5 万元。"
        routes = {
            "/qa": qa_list(
                [
                    ("P-001", "01", "Q001", "/qa/q001-a"),
                    ("P-001", "01", "Q001", "/qa/q001-b"),
                ]
            ),
            "/qa/q001-a": qa_detail("P-001", "01", "Q001", body),
            "/qa/q001-b": qa_detail("P-001", "01", "Q001", body),
        }
        result = await self._check(routes, ["Q001"])
        self.assertEqual(result["results"][0]["status"], "answered")

    async def test_other_project_and_other_lot_are_excluded(self):
        routes = {
            "/qa": qa_list(
                [
                    ("P-002", "A", "Q201", "/qa/q201"),
                    ("P-001", "02", "Q301", "/qa/q301"),
                ]
            ),
            "/qa/q201": qa_detail("P-002", "A", "Q201", "另一项目的答复。"),
            "/qa/q301": qa_detail("P-001", "02", "Q301", "同项目其他标段的答复。"),
        }
        result = await self._check(routes, ["Q201", "Q301"])
        for item in result["results"]:
            self.assertEqual(item["status"], "unanswered")
        self.assertFalse(result["round_incomplete"])

    async def test_list_failure_makes_missing_question_indeterminate(self):
        routes = {
            "/qa": qa_list(
                [("P-001", "01", "Q001", "/qa/q001")], next_href="/qa?page=2"
            ),
            "/qa?page=2": "",
            "/qa/q001": qa_detail("P-001", "01", "Q001", "直接答复内容。"),
        }
        result = await self._check(routes, ["Q001", "Q010"], failing={"/qa?page=2"})
        by_no = {item["question_no"]: item for item in result["results"]}
        self.assertTrue(result["round_incomplete"])
        # A proven answer survives a fetch failure.
        self.assertEqual(by_no["Q001"]["status"], "answered")
        # A missing question must not be reported as unanswered.
        self.assertEqual(by_no["Q010"]["status"], "indeterminate")
        failed = [failure["url"] for failure in result["failures"]]
        self.assertTrue(any("/qa?page=2" in url for url in failed))

    async def test_detail_failure_is_indeterminate_not_unanswered(self):
        routes = {
            "/qa": qa_list([("P-001", "01", "Q005", "/qa/q005")]),
            "/qa/q005": "",
        }
        result = await self._check(routes, ["Q005"], failing={"/qa/q005"})
        self.assertEqual(result["results"][0]["status"], "indeterminate")
        self.assertTrue(result["failures"])

    async def test_reference_to_empty_answer_is_unanswered_when_complete(self):
        routes = {
            "/qa": qa_list(
                [
                    ("P-001", "01", "Q002", "/qa/q002"),
                    ("P-001", "01", "Q003", "/qa/q003"),
                ]
            ),
            "/qa/q002": qa_detail("P-001", "01", "Q002", "参见另一问题 Q003。"),
            "/qa/q003": qa_detail("P-001", "01", "Q003", ""),
        }
        result = await self._check(routes, ["Q002"])
        self.assertEqual(result["results"][0]["status"], "unanswered")
        self.assertEqual(
            [hop["question_no"] for hop in result["results"][0]["chain"]],
            ["Q002", "Q003"],
        )

    async def test_direct_answer_and_reference_together_stays_answered(self):
        routes = {
            "/qa": qa_list(
                [
                    ("P-001", "01", "Q002", "/qa/q002"),
                    ("P-001", "01", "Q003", "/qa/q003"),
                ]
            ),
            "/qa/q002": qa_detail(
                "P-001", "01", "Q002", "保证金为 5 万元，其他事项参见另一问题 Q003。"
            ),
            "/qa/q003": qa_detail("P-001", "01", "Q003", "另一事项的答复。"),
        }
        result = await self._check(routes, ["Q002"])
        item = result["results"][0]
        self.assertEqual(item["status"], "answered")
        self.assertIn("5 万元", item["answer"])
        # A substantive answer must not be turned into a reference chain.
        self.assertEqual([hop["question_no"] for hop in item["chain"]], ["Q002"])

    async def test_conflicting_direct_answers_outrank_reference_record(self):
        routes = {
            "/qa": qa_list(
                [
                    ("P-001", "01", "Q012", "/qa/q012-a"),
                    ("P-001", "01", "Q012", "/qa/q012-b"),
                    ("P-001", "01", "Q012", "/qa/q012-c"),
                    ("P-001", "01", "Q099", "/qa/q099"),
                ]
            ),
            "/qa/q012-a": qa_detail("P-001", "01", "Q012", "有效期 90 天。"),
            "/qa/q012-b": qa_detail("P-001", "01", "Q012", "有效期修订为 60 天。"),
            "/qa/q012-c": qa_detail("P-001", "01", "Q012", "参见另一问题 Q099。"),
            "/qa/q099": qa_detail("P-001", "01", "Q099", "被引用的答复。"),
        }
        result = await self._check(routes, ["Q012"])
        self.assertEqual(result["results"][0]["status"], "needs_review")
        self.assertIsNone(result["results"][0]["answer"])

    async def test_empty_record_with_incomplete_round_is_indeterminate(self):
        routes = {
            "/qa": qa_list(
                [("P-001", "01", "Q005", "/qa/q005")], next_href="/qa?page=2"
            ),
            "/qa?page=2": "",
            "/qa/q005": qa_detail("P-001", "01", "Q005", ""),
        }
        result = await self._check(routes, ["Q005"], failing={"/qa?page=2"})
        self.assertEqual(result["results"][0]["status"], "indeterminate")
        self.assertTrue(result["round_incomplete"])

    async def test_invalid_url_returns_indeterminate(self):
        result = (
            await check_qa(
                "ftp://test/qa", "P-001", "01", ["Q001"],
                client_factory=self.make_factory({}),
            )
        ).as_dict()
        self.assertTrue(result["round_incomplete"])
        self.assertEqual(result["results"][0]["status"], "indeterminate")
        self.assertEqual(result["failures"][0]["stage"], "validation")


if __name__ == "__main__":
    unittest.main()
