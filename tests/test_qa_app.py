"""End-to-end checks for the Q&A feature using in-process ASGI apps.

Both the web application and the local demo site are exercised through
httpx's async ASGI transport, so these tests need neither open network
sockets nor a running Docker Compose stack.
"""

from __future__ import annotations

import os
import unittest

import httpx

os.environ.setdefault("DEMO_INTERNAL_URL", "http://demo.local")
os.environ.setdefault("DEMO_PUBLIC_URL", "http://demo.local")

from app.main import app as web_app  # noqa: E402
from demo_site.main import app as demo_app  # noqa: E402


class DemoQAFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_qa_pages_span_five_pages(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=demo_app), base_url="http://demo.local"
        ) as client:
            for page in range(1, 6):
                response = await client.get(f"/qa?page={page}")
                self.assertEqual(response.status_code, 200)
            self.assertEqual((await client.get("/qa?page=99")).status_code, 404)

    async def test_demo_qa_detail_markers(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=demo_app), base_url="http://demo.local"
        ) as client:
            response = await client.get("/qa/detail/DEMO-2026-001-01-qa-q005")
            self.assertEqual(response.status_code, 200)
            self.assertIn('data-question-no="Q005"', response.text)
            self.assertIn("本问题暂未答复", response.text)

    async def test_demo_cross_page_reference_fixture(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=demo_app), base_url="http://demo.local"
        ) as client:
            q002 = await client.get("/qa/detail/DEMO-2026-001-01-qa-q002")
            q003 = await client.get("/qa/detail/DEMO-2026-001-01-qa-q003")
            self.assertIn("参见另一问题 Q003", q002.text)
            self.assertIn("银行保函", q003.text)


class WebQAAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Server-side outbound requests made by the crawler are routed straight
        # into the demo ASGI app instead of opening real sockets.
        self.demo_transport = httpx.ASGITransport(app=demo_app)
        import app.main as web_main

        def factory():
            return httpx.AsyncClient(transport=self.demo_transport, follow_redirects=True)

        self._original_factory = web_main.QA_CLIENT_FACTORY
        self._original_demo_factory = web_main.DEMO_CLIENT_FACTORY
        web_main.QA_CLIENT_FACTORY = factory
        web_main.DEMO_CLIENT_FACTORY = factory
        self.web_main = web_main
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://web.local"
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.web_main.QA_CLIENT_FACTORY = self._original_factory
        self.web_main.DEMO_CLIENT_FACTORY = self._original_demo_factory

    async def _check(self, *questions: str) -> dict:
        params = [
            ("list_url", "http://demo.local/qa?page=1"),
            ("project_no", "DEMO-2026-001"),
            ("section_no", "01"),
        ]
        params.extend(("question_numbers", q) for q in questions)
        response = await self.client.get("/api/qa-check", params=params)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_full_demo_scenario_matrix(self):
        data = await self._check(
            "Q001", "Q002", "Q004", "Q005", "Q011", "Q012", "Q201", "Q301", "Q099"
        )
        by_no = {item["question_no"]: item for item in data["results"]}
        self.assertFalse(data["round_incomplete"])

        self.assertEqual(by_no["Q001"]["status"], "answered")

        q002 = by_no["Q002"]
        self.assertEqual(q002["status"], "answered")
        self.assertEqual([h["question_no"] for h in q002["chain"]], ["Q002", "Q003"])
        self.assertTrue(q002["answer_source_url"].startswith("/demo/"))

        self.assertEqual(by_no["Q004"]["status"], "needs_review")
        self.assertTrue(any("循环引用" in reason for reason in by_no["Q004"]["reasons"]))

        self.assertEqual(by_no["Q005"]["status"], "unanswered")

        self.assertEqual(by_no["Q011"]["status"], "answered")
        self.assertEqual(
            [h["question_no"] for h in by_no["Q011"]["chain"]], ["Q011", "Q003"]
        )

        self.assertEqual(by_no["Q012"]["status"], "needs_review")
        self.assertEqual(by_no["Q012"]["record_count"], 2)

        for excluded in ("Q201", "Q301", "Q099"):
            self.assertEqual(by_no[excluded]["status"], "unanswered")
            self.assertEqual(by_no[excluded]["record_count"], 0)

    async def test_html_check_page_renders_all_statuses(self):
        response = await self.client.get(
            "/qa-check",
            params=[
                ("list_url", "http://demo.local/qa?page=1"),
                ("project_no", "DEMO-2026-001"),
                ("section_no", "01"),
                ("question_numbers", "Q002,Q004,Q005"),
            ],
        )
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("qa-status-answered", body)
        self.assertIn("qa-status-needs_review", body)
        self.assertIn("qa-status-unanswered", body)
        self.assertIn("引用经过与来源", body)

    async def test_missing_question_numbers_returns_400(self):
        response = await self.client.get(
            "/qa-check",
            params={
                "list_url": "http://demo.local/qa?page=1",
                "project_no": "DEMO-2026-001",
                "section_no": "01",
            },
        )
        self.assertEqual(response.status_code, 400)

    async def test_post_api_accepts_question_list(self):
        response = await self.client.post(
            "/api/qa-check",
            json={
                "list_url": "http://demo.local/qa?page=1",
                "project_no": "DEMO-2026-001",
                "section_no": "01",
                "question_numbers": ["Q010"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["status"], "answered")

    async def test_demo_proxy_serves_qa_pages(self):
        response = await self.client.get("/demo/qa?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertIn("答疑澄清列表", response.text)


if __name__ == "__main__":
    unittest.main()
