"""API 크레딧(Max·Ultra 월간 크레딧). 실제 자격 증명 저장소·네트워크는 쓰지 않는다(mock 만)."""
import ast
import io
import json
import sys
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import provider_worker  # noqa: E402
from hardening import config_values  # noqa: E402
from providers import anthropic_api  # noqa: E402

KEY = "sk-ant-admin01-" + "a" * 40


def widget_parts(*names):
    """widget.py 에서 이름 붙은 함수·클래스·상수만 떼어 실행한다(위젯 전체를 띄우지 않는다)."""
    import calendar
    tree = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
    keep = [n for n in tree.body if getattr(n, "name", "") in names
            or isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names for t in n.targets)]
    import re
    ns = dict(datetime=datetime, calendar=calendar, time=time, re=re)
    exec(compile(ast.Module(body=keep, type_ignores=[]), "widget.py", "exec"), ns)
    return ns


class CreditCycleTest(unittest.TestCase):
    def setUp(self):
        self.cycle = widget_parts("credit_cycle")["credit_cycle"]

    def at(self, *args):
        return datetime(*args).timestamp()

    def test_cycle_starts_on_billing_day(self):
        self.assertIsNone(self.cycle(0, self.at(2026, 11, 12)))
        self.assertEqual(self.cycle(20, self.at(2026, 11, 12, 9)), (self.at(2026, 10, 20), self.at(2026, 11, 20)))
        self.assertEqual(self.cycle(5, self.at(2026, 11, 12)), (self.at(2026, 11, 5), self.at(2026, 12, 5)))
        self.assertEqual(self.cycle(12, self.at(2026, 11, 12)), (self.at(2026, 11, 12), self.at(2026, 12, 12)))

    def test_short_months_and_year_end(self):
        # 31일 결제: 그달에 31일이 없으면 마지막 날
        self.assertEqual(self.cycle(31, self.at(2026, 2, 15)), (self.at(2026, 1, 31), self.at(2026, 2, 28)))
        self.assertEqual(self.cycle(31, self.at(2026, 3, 1)), (self.at(2026, 2, 28), self.at(2026, 3, 31)))
        self.assertEqual(self.cycle(15, self.at(2027, 1, 3)), (self.at(2026, 12, 15), self.at(2027, 1, 15)))
        self.assertEqual(self.cycle(15, self.at(2026, 12, 20)), (self.at(2026, 12, 15), self.at(2027, 1, 15)))

    def test_config_accepts_only_valid_values(self):
        base = {"credit_day": 0, "credit_amount": 100}
        pick = lambda cfg: {k: cfg[k] for k in base}
        self.assertEqual(pick(config_values(base, {"credit_day": 20, "credit_amount": 200})), {"credit_day": 20, "credit_amount": 200})
        for bad in ({"credit_day": 32}, {"credit_day": -1}, {"credit_day": 5.5}, {"credit_day": True},
                    {"credit_amount": 150}, {"credit_amount": "100"}, {"credit_amount": 100.0}):
            self.assertEqual(pick(config_values(base, bad)), base, bad)


class AdminKeyTest(unittest.TestCase):
    def test_key_format(self):
        self.assertTrue(anthropic_api.valid_key(KEY))
        self.assertTrue(anthropic_api.valid_key("sk-ant-api03-" + "a" * 40))  # 작업 공간에 묶이지 않은 키도 받고, 권한은 서버가 가린다
        for bad in ("", "sk-ant-" + "a" * 40, KEY + " ", KEY + "\n", "sk-ant-admin", "sk-ant-admin01-" + "a" * 300,
                    "sk-ant-admin01-" + "a" * 20 + ".x", "sk-proj-" + "a" * 40, None):
            self.assertFalse(anthropic_api.valid_key(bad), bad)

    def test_set_key_saves_clears_and_reports_without_returning_it(self):
        with mock.patch.object(anthropic_api, "write_key", return_value=True) as write, \
                mock.patch.object(anthropic_api, "delete_key", return_value=True) as delete, \
                mock.patch.object(anthropic_api, "read_key", return_value=KEY):
            self.assertEqual(anthropic_api.set_key(KEY), {"saved": True})
            write.assert_called_once_with(KEY)
            self.assertEqual(anthropic_api.set_key(None), {"saved": True})
            self.assertEqual(anthropic_api.set_key(""), {"saved": False})
            delete.assert_called_once()
            with self.assertRaises(ValueError):
                anthropic_api.set_key("sk-ant-" + "b" * 40)
            self.assertEqual(write.call_count, 1)


class CostReportTest(unittest.TestCase):
    def test_sums_cents_across_pages(self):
        pages = [{"data": [{"results": [{"amount": "1234.5", "currency": "USD"}, {"amount": "65.5", "currency": "USD"}]},
                           {"results": []}], "has_more": True, "next_page": "page_2"},
                 {"data": [{"results": [{"amount": "700", "currency": "USD"}]}], "has_more": False, "next_page": None}]
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY), \
                mock.patch.object(anthropic_api, "http_json", side_effect=pages) as http:
            since = datetime(2026, 10, 20, 9).timestamp()
            out = anthropic_api.fetch(since)
        self.assertEqual(out["spent"], 20.0)
        first, second = (c.args for c in http.call_args_list)
        self.assertIn("starting_at=2026-10-20T00%3A00%3A00Z", first[0])
        self.assertIn("page=page_2", second[0])
        self.assertEqual(first[1]["x-api-key"], KEY)
        self.assertTrue(first[0].startswith("https://api.anthropic.com/v1/organizations/cost_report?"))

    def test_daily_amounts_by_utc_date(self):
        page = {"data": [{"starting_at": "2026-11-01T00:00:00Z", "results": [{"amount": "150"}, {"amount": "50"}]},
                         {"starting_at": "2026-11-02T00:00:00Z", "results": []},
                         {"starting_at": "<b>", "results": [{"amount": "100"}]}], "has_more": False}
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY),                 mock.patch.object(anthropic_api, "http_json", return_value=page):
            out = anthropic_api.fetch(time.time())
        self.assertEqual(out["spent"], 3.0)  # 날짜가 이상한 칸은 합계에만 들어간다
        self.assertEqual(out["daily"], [["2026-11-01", 2.0]])

    def test_widget_keeps_only_well_formed_daily_rows(self):
        clean = widget_parts("credit_daily")
        clean["finite"] = lambda v, lo, hi: isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
        rows = [["2026-11-01", 2], ["<script>", 1], ["2026-11-02", -1], ["2026-11-03", "9"], "x", ["2026-11-04", 0.5, 1]]
        self.assertEqual(clean["credit_daily"](rows), [["2026-11-01", 2.0]])
        self.assertEqual(clean["credit_daily"](None), [])

    def test_page_limit_and_bad_values(self):
        endless = {"data": [{"results": [{"amount": "100"}]}], "has_more": True, "next_page": "p"}
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY), \
                mock.patch.object(anthropic_api, "http_json", return_value=endless) as http:
            self.assertEqual(anthropic_api.fetch(time.time())["spent"], anthropic_api.MAX_PAGES * 1.0)
            self.assertEqual(http.call_count, anthropic_api.MAX_PAGES)
        for amount in ("nan", "-5", "inf"):
            with mock.patch.object(anthropic_api, "read_key", return_value=KEY), \
                    mock.patch.object(anthropic_api, "http_json", return_value={"data": [{"results": [{"amount": amount}]}]}):
                with self.assertRaises(RuntimeError):
                    anthropic_api.fetch(time.time())

    def test_server_text_and_request_errors_are_not_forwarded(self):
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY),                 mock.patch.object(anthropic_api, "http_json", return_value={"data": [{"results": [{"amount": "<b>hi</b>"}]}]}):
            with self.assertRaisesRegex(RuntimeError, "^비용 보고서 값을 읽지 못했어요$"):
                anthropic_api.fetch(time.time())
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY),                 mock.patch.object(anthropic_api, "http_json", side_effect=ValueError("Invalid header value " + KEY)):
            with self.assertRaises(RuntimeError) as ctx:
                anthropic_api.fetch(time.time())
            self.assertNotIn(KEY, str(ctx.exception))

    def test_missing_or_rejected_key(self):
        with mock.patch.object(anthropic_api, "read_key", return_value=None), \
                mock.patch.object(anthropic_api, "http_json") as http:
            with self.assertRaisesRegex(RuntimeError, "관리자 키가 없어요"):
                anthropic_api.fetch(time.time())
            http.assert_not_called()
        denied = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY), \
                mock.patch.object(anthropic_api, "http_json", side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, "키가 맞지 않거나"):
                anthropic_api.fetch(time.time())
        # 작업 공간에 묶인 개인 키: 서버가 401 과 함께 Admin API 키가 필요하다고 답한다
        scoped = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(json.dumps({"error": {
            "type": "authentication_error",
            "message": "The Admin API requires an Admin API key or an organization-scoped API key."}}).encode()))
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY), \
                mock.patch.object(anthropic_api, "http_json", side_effect=scoped):
            with self.assertRaises(RuntimeError) as ctx:
                anthropic_api.fetch(time.time())
            self.assertEqual(str(ctx.exception), anthropic_api.NO_ACCESS)


class BrokerCreditTest(unittest.TestCase):
    def test_credit_questions_are_checked(self):
        now = int(time.time())
        with mock.patch.object(anthropic_api, "fetch", return_value={"spent": 3.5, "observed_at": 1.0}) as fetch, \
                mock.patch.object(anthropic_api, "set_key", return_value={"saved": True}) as set_key:
            self.assertEqual(provider_worker.answer({"op": "credit", "provider": "claude", "path": None, "value": now - 86400}),
                             {"ok": {"spent": 3.5, "observed_at": 1.0}})
            self.assertEqual(provider_worker.answer({"op": "credit_key", "provider": "claude", "path": None, "value": KEY}),
                             {"ok": {"saved": True}})
            for bad in ({"op": "credit", "provider": "claude", "value": now - 50 * 86400},   # 40일보다 전
                        {"op": "credit", "provider": "claude", "value": now + 3600},         # 앞날
                        {"op": "credit", "provider": "claude", "value": "1"},
                        {"op": "credit", "provider": "gpt", "value": now},
                        {"op": "credit", "provider": "claude", "path": "C:/x", "value": now},
                        {"op": "credit_key", "provider": "claude", "value": 5},
                        {"op": "credit_key", "provider": "claude", "value": "x" * 300},
                        {"op": "fetch", "provider": "claude", "value": KEY}):            # 다른 질문에 value 를 끼우지 못한다
                self.assertIn("error", provider_worker.answer(bad), bad)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(set_key.call_count, 1)

    def test_reply_never_contains_the_key(self):
        with mock.patch.object(anthropic_api, "read_key", return_value=KEY), \
                mock.patch.object(anthropic_api, "write_key", return_value=True):
            reply = provider_worker.answer({"op": "credit_key", "provider": "claude", "value": None})
        self.assertEqual(reply, {"ok": {"saved": True}})
        self.assertNotIn(KEY, json.dumps(reply))


class CreditScheduleTest(unittest.TestCase):
    """설정을 바꿔도 30분 간격·429 대기를 건너뛰지 않는다."""
    def make(self, limited):
        import threading
        ns = widget_parts("Api")
        app = mock.Mock()
        app.lock, app.cfg = threading.Lock(), {"credit_day": 20, "credit_amount": 100}
        app.next_at, app.limited = {"api_credit": 9e9}, {"api_credit": 2} if limited else {}
        app.credit = {}
        api = ns["Api"].__new__(ns["Api"])
        api._app = app
        ns["save_config"] = mock.Mock()
        ns["provider_call"] = mock.Mock(return_value={"saved": True})
        return api, app

    def test_amount_only_change_does_not_refetch(self):
        api, app = self.make(False)
        api.set_credit(20, 200)
        self.assertEqual(app.next_at, {"api_credit": 9e9})
        api.set_credit(5, 200)
        self.assertEqual(app.next_at, {})

    def test_rate_limited_waits_even_after_changes(self):
        api, app = self.make(True)
        api.set_credit(5, 100)
        api.set_credit_key(KEY)
        self.assertEqual(app.next_at, {"api_credit": 9e9})
        self.assertEqual(app.limited, {"api_credit": 2})


class CreditAlertTest(unittest.TestCase):
    def make(self, day, spent, since_now=True):
        ns = widget_parts("credit_cycle", "Core")
        core = ns["Core"].__new__(ns["Core"])
        import threading
        core.lock, core.alerted = threading.Lock(), set()
        core.cfg = {"credit_day": day, "credit_amount": 100}
        core.credit = {"has_key": spent is not None, "spent": spent, "observed_at": None, "since": None, "err": ""}
        return ns, core

    def test_alert_three_days_and_one_day_before_once(self):
        ns, core = self.make(20, 40.0)
        start, end = ns["credit_cycle"](20, datetime(2026, 11, 12).timestamp())
        core.credit["since"] = int(start)
        msg = core._credit_alert(end - 2.5 * 86400)
        self.assertIn("$60 남음", msg)
        self.assertIsNone(core._credit_alert(end - 2 * 86400))   # 같은 단계는 한 번만
        self.assertIn("내일", core._credit_alert(end - 3600))
        self.assertIsNone(core._credit_alert(end - 10 * 86400 - 1))

    def test_no_alert_when_almost_used_or_off(self):
        ns, core = self.make(20, 97.0)
        start, end = ns["credit_cycle"](20, datetime(2026, 11, 12).timestamp())
        core.credit["since"] = int(start)
        self.assertIsNone(core._credit_alert(end - 3600))
        _, off = self.make(0, None)
        self.assertIsNone(off._credit_alert(time.time()))

    def test_spent_from_previous_cycle_is_not_shown(self):
        ns, core = self.make(20, 40.0)
        start, end = ns["credit_cycle"](20, datetime(2026, 11, 12).timestamp())
        core.credit["since"] = int(start) - 30 * 86400
        view = core.credit_view(end - 86400)
        self.assertIsNone(view["spent"])
        self.assertIn("새로 들어와요", core._credit_alert(end - 3600))


if __name__ == "__main__":
    unittest.main()
