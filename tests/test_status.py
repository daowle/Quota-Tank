"""장애 배지 판정. 실제 계정·네트워크·앱 설정은 쓰지 않는다."""
import ast
from pathlib import Path
import unittest
from unittest import mock


class StatusTest(unittest.TestCase):
    def setUp(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / "widget.py").read_text(encoding="utf-8"))
        self.http = mock.Mock()
        self.ns = {"http_json": self.http, "STATUS_URLS": {"claude": "https://example.test/summary"}}
        nodes = [n for n in source.body if getattr(n, "name", "") == "fetch_status"]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "widget.py", "exec"), self.ns)

    def result(self, code="operational", chat="operational", console="major_outage"):
        self.http.return_value = {
            "status": {"indicator": "critical", "description": "Major Service Outage"},
            "components": [{"name": name, "status": status} for name, status in
                           [("Claude Code", code), ("claude.ai", chat),
                            ("Claude Console (platform.claude.com)", console)]]}
        return self.ns["fetch_status"]()

    def test_console_outage_does_not_mark_subscription_as_down(self):
        self.assertEqual(self.result()["claude"], {"level": "none", "text": "정상"})

    def test_relevant_outage_and_recovery(self):
        self.assertEqual(self.result(code="partial_outage")["claude"],
                         {"level": "major", "text": "Claude Code 상태 이상"})
        self.assertEqual(self.result()["claude"]["level"], "none")

    def test_highest_relevant_severity(self):
        self.assertEqual(self.result(code="degraded_performance", chat="major_outage")["claude"]["level"], "critical")

    def test_maintenance(self):
        self.assertEqual(self.result(code="under_maintenance")["claude"]["level"], "maintenance")

    def test_incomplete_or_unknown_status_is_not_a_global_outage(self):
        self.http.return_value = {"status": {"indicator": "major"}, "components": []}
        self.assertEqual(self.ns["fetch_status"](), {})
        self.assertEqual(self.result(code="unknown"), {})

    def test_failed_request_does_not_reuse_an_outage(self):
        self.result(code="major_outage")
        self.http.side_effect = RuntimeError("접속 실패")
        self.assertEqual(self.ns["fetch_status"](), {})

    def codex(self, **status):
        self.ns["STATUS_URLS"] = {"gpt": "https://example.test/summary"}
        names = ["Chat Completions", "Login", "Login", "Codex in ChatGPT Desktop", "Compliance API"]
        self.http.return_value = {
            "status": {"indicator": "minor", "description": "Partial System Degradation"},
            "components": [{"name": n, "status": status.get(n, "operational")} for n in names]}
        return self.ns["fetch_status"]()

    def test_codex_ignores_unrelated_products(self):
        # 2026-10-09: Compliance API 만 느려졌는데 페이지 전체가 "부분 장애"로 떠서 Codex 카드에 배지가 남았다
        self.assertEqual(self.codex(**{"Compliance API": "degraded_performance", "Chat Completions": "major_outage"}),
                         {"gpt": {"level": "none", "text": "정상"}})

    def test_codex_relevant_outage(self):
        self.assertEqual(self.codex(**{"Codex in ChatGPT Desktop": "partial_outage"}),
                         {"gpt": {"level": "major", "text": "Codex in ChatGPT Desktop 상태 이상"}})
        self.assertEqual(self.codex(Login="degraded_performance"), {"gpt": {"level": "minor", "text": "Login 상태 이상"}})

    def test_codex_without_a_codex_component_is_not_judged(self):
        self.ns["STATUS_URLS"] = {"gpt": "https://example.test/summary"}
        self.http.return_value = {"status": {"indicator": "major"}, "components": [{"name": "Login", "status": "major_outage"}]}
        self.assertEqual(self.ns["fetch_status"](), {})
        self.http.return_value = {"status": {"indicator": "major", "description": "x"}}  # 예전 형식(components 없음)
        self.assertEqual(self.ns["fetch_status"](), {})


if __name__ == "__main__":
    unittest.main()
