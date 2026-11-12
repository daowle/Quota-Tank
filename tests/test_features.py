"""토큰·비용 집계와 추가 한도 파싱. test_accounts 처럼 필요한 정의만 꺼내 실제 앱 데이터는 건드리지 않는다."""
import ast
import json
import os
import re
import sys
import tempfile
import time
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from providers import claude, codex  # noqa: E402


def iso_to_epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


class FeaturesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        source = ast.parse((Path(__file__).resolve().parents[1] / "widget.py").read_text(encoding="utf-8"))
        names = {"PRICES", "COST_DAYS", "price_of", "_scan_claude_file", "_scan_codex_file", "cost_summary",
                 "claude_extra_limits", "codex_extra_limits", "codex_windows", "codex_window_name", "codex_main_limits",
                 "fetch_gpt_from_sessions", "scan_costs", "is_5h", "scan_recent_claude", "claude_rows", "SHARE_RECENT_HOURS", "sunk_below"}
        keep = [n for n in source.body if getattr(n, "name", "") in names
                or isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names for t in n.targets)]
        self.ns = dict(json=json, time=time, datetime=datetime, timedelta=timedelta, defaultdict=defaultdict,
                       iso_to_epoch=iso_to_epoch, FIVE_H=18000, WEEK=604800, re=re, HOME=self.dir,
                       os=os, COST_CACHE_PATH=self.dir / "cost_cache.json")
        exec(compile(ast.Module(body=keep, type_ignores=[]), "widget.py", "exec"), self.ns)
        # 서비스별 파싱은 providers/ 모듈에 있다. 세션 로그는 임시 폴더를 보게 한다
        patcher = mock.patch.object(codex, "HOME", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ns.update(claude_extra_limits=claude.extra_limits, codex_extra_limits=codex.extra_limits,
                       codex_main_limits=codex.main_limits, codex_windows=codex.windows,
                       fetch_gpt_from_sessions=codex.fetch_from_sessions, is_5h=codex.is_5h)

    def write(self, name, lines):
        p = self.dir / name
        p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        return p

    def test_topmost_window_is_raised_only_when_sunk(self):
        sunk = self.ns["sunk_below"]
        self.assertFalse(sunk(10, 10, True, "WindowsForms10"))   # 내 창이 맨 위
        self.assertFalse(sunk(10, None, False, ""))              # 그 자리에 창이 없다
        self.assertTrue(sunk(10, 20, False, "Chrome_WidgetWin_1"))  # 전체 화면 등 보통 창이 덮었다
        self.assertTrue(sunk(10, 20, True, "WorkerW"))           # 바탕 화면 보기
        self.assertFalse(sunk(10, 20, True, "#32768"))           # 다른 항상 위 창(메뉴)은 그대로 둔다

    def test_price_prefers_longest_prefix_and_overrides(self):
        price_of = self.ns["price_of"]
        self.assertEqual(price_of("claude-opus-5-5")[0], 4)
        self.assertEqual(price_of("claude-opus-5")[0], 5)
        self.assertEqual(price_of("claude-haiku-4-5-20251001")[1], 5)
        self.assertIsNone(price_of("codex-auto-review"))
        self.assertEqual(price_of("codex-auto-review", {"codex-auto-review": [1, 2, 0.1, 1, 1]})[1], 2)

    def test_claude_scan_counts_each_response_once(self):
        now = datetime.now().isoformat()
        msg = {"type": "assistant", "requestId": "r1", "timestamp": now, "message": {
            "id": "m1", "model": "claude-opus-5-5", "usage": {
                "input_tokens": 10, "output_tokens": 1_000_000, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 300, "cache_creation": {"ephemeral_1h_input_tokens": 100}}}}
        synthetic = {"type": "assistant", "requestId": "r2", "timestamp": now,
                     "message": {"id": "m2", "model": "<synthetic>", "usage": {"input_tokens": 5}}}
        rows = self.ns["_scan_claude_file"](self.write("a.jsonl", [msg, msg, synthetic]))
        self.assertEqual(len(rows), 1)
        (row,) = rows.values()
        self.assertEqual(row[1:7], ["claude-opus-5-5", 10, 1_000_000, 0, 200, 100])
        self.assertAlmostEqual(row[7], datetime.fromisoformat(now).timestamp(), delta=1)  # 공유 사용량 나누기용 요청 시각
        # 다른 파일(이어 하기 세션)에 같은 응답이 있어도 합계는 한 번
        cache = {"claude": {"a": {"rows": rows}, "b": {"rows": dict(rows)}}, "gpt": {}}
        s = self.ns["cost_summary"](cache)
        self.assertAlmostEqual(s["svc"]["claude"]["total"], (10 * 4 + 1_000_000 * 20 + 200 * 5 + 100 * 8) / 1e6)

    def test_recent_scan_reads_only_fresh_changed_files(self):
        root = self.dir / "projects" / "p"
        root.mkdir(parents=True)
        now = datetime.now().isoformat()

        def msg(i):
            return {"type": "assistant", "requestId": f"r{i}", "timestamp": now, "message": {
                "id": f"m{i}", "model": "claude-opus-5-5", "usage": {"input_tokens": 1, "output_tokens": 1}}}
        fresh, stale = root / "fresh.jsonl", root / "stale.jsonl"
        fresh.write_text(json.dumps(msg(1)), encoding="utf-8")
        stale.write_text(json.dumps(msg(2)), encoding="utf-8")
        os.utime(stale, (time.time() - 30 * 3600,) * 2)
        top = self.dir / "projects"
        memo = self.ns["scan_recent_claude"]({}, hours=6, root=top)
        self.assertEqual(list(memo), [str(fresh)])
        again = self.ns["scan_recent_claude"](memo, hours=6, root=top)
        self.assertIs(again[str(fresh)], memo[str(fresh)])  # 바뀌지 않았으면 다시 읽지 않는다
        fresh.write_text(json.dumps(msg(1)) + "\n" + json.dumps(msg(3)), encoding="utf-8")
        changed = self.ns["scan_recent_claude"](again, hours=6, root=top)
        self.assertEqual(len(changed[str(fresh)][2]), 2)
        self.assertEqual(self.ns["scan_recent_claude"]({}, root=self.dir / "missing"), {})

    def test_claude_rows_counts_a_response_once_across_files(self):
        row = ["2026-10-08", "claude-opus-5-5", 1, 1, 0, 0, 0, 5]
        cache = {"claude": {"a": {"rows": {"m1:r1": row}}, "b": {"rows": {"m1:r1": row, "m2:r2": row}}}}
        self.assertEqual(set(self.ns["claude_rows"](cache)), {"m1:r1", "m2:r2"})
        self.assertEqual(self.ns["claude_rows"]({}), {})

    def test_codex_scan_uses_cumulative_deltas(self):
        now = datetime.now().isoformat()

        def tc(i, c, o):
            return {"timestamp": now, "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o},
                "last_token_usage": {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}}}
        lines = [{"type": "turn_context", "payload": {"model": "gpt-6-sol"}},
                 tc(100, 40, 10), tc(100, 40, 10), tc(300, 140, 30)]  # 같은 이벤트 반복은 0 으로
        rows = self.ns["_scan_codex_file"](self.write("b.jsonl", lines))
        self.assertEqual([r[1:] for r in rows], [["gpt-6-sol", 300, 140, 0, 30, False]])
        s = self.ns["cost_summary"]({"claude": {}, "gpt": {"b": {"rows": rows}}})
        self.assertAlmostEqual(s["svc"]["gpt"]["total"], (160 * 2 + 140 * 0.2 + 30 * 10) / 1e6)
        self.assertEqual(s["svc"]["gpt"]["tokens"], 330)

    def test_unpriced_models_count_tokens_only(self):
        day = datetime.now().strftime("%Y-%m-%d")
        s = self.ns["cost_summary"]({"claude": {}, "gpt": {"x": {"rows": [[day, "codex-auto-review", 50, 0, 0, 5]]}}})
        g = s["svc"]["gpt"]
        self.assertEqual((g["total"], g["tokens"], g["unpriced"]), (0, 55, 55))
        self.assertFalse(g["models"][0]["known"])

    def test_extra_limits(self):
        c = self.ns["claude_extra_limits"]({"limits": [
            {"kind": "session", "group": "session", "percent": 4},
            {"kind": "weekly_all", "group": "weekly", "percent": 52},
            {"kind": "weekly_opus", "group": "weekly", "percent": 41, "resets_at": "2026-10-05T15:00:00+00:00"}]})
        self.assertEqual([(x["name"], x["u"]) for x in c], [("Opus 주간", 41.0)])
        self.assertEqual(self.ns["claude_extra_limits"]({"seven_day_sonnet": {"utilization": 7}})[0]["name"], "Sonnet 주간")
        g = self.ns["codex_extra_limits"]({"additional_rate_limits": [{"limit_name": "Astra", "rate_limit": {
            "primary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_at": 1}}}]})
        self.assertEqual(g, [{"name": "Astra 5시간", "u": 12.0, "r": 1}])
        self.assertEqual(self.ns["codex_extra_limits"]({"additional_rate_limits": None}), [])

    def test_official_codex_prices_and_unknown_variant(self):
        price = self.ns["price_of"]
        self.assertEqual(price("gpt-6.1-sol"), (2, 10, 0.1, 2.5, 2.5))
        self.assertEqual(price("gpt-5.6-sol"), (4, 20, 0.4, 5, 5))
        self.assertEqual(price("gpt-5.6"), price("gpt-5.6-sol"))
        self.assertEqual(price("gpt-6-luna")[3], 0.125)
        self.assertEqual(price("gpt-5.3-codex")[1], 14)
        self.assertEqual(price("claude-opus-4-1")[0], 15)
        self.assertEqual(price("claude-opus-4-5")[0], 5)
        self.assertIsNone(price("gpt-6-sol-unknown"))
        self.assertEqual(price("gpt-6.1-sol-2026-10-01"), price("gpt-6.1-sol"))

    def test_invalid_overrides_do_not_break_costs(self):
        price = self.ns["price_of"]
        for invalid in ([1, 2], [-1, 2, 3, 4, 5], [float("nan")] * 5, "bad"):
            self.assertEqual(price("gpt-6-sol", {"gpt-6-sol": invalid}), (2, 10, 0.2, 2.5, 2.5))

    def test_cached_inputs_writes_and_reasoning_not_double_counted(self):
        day = datetime.now().strftime("%Y-%m-%d")
        row = [day, "gpt-6.1-sol", 1_000_000, 600_000, 100_000, 100_000, False]
        result = self.ns["cost_summary"]({"gpt": {"x": {"rows": [row]}}})["svc"]["gpt"]
        self.assertAlmostEqual(result["total"], 0.6 + 0.06 + 0.25 + 1)
        self.assertEqual(result["tokens"], 1_100_000)

    def test_codex_long_context_is_per_request_not_session_total(self):
        now = datetime.now().isoformat()
        def event(total, last):
            return {"timestamp": now, "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": total, "output_tokens": total // 10},
                "last_token_usage": {"input_tokens": last}}}}
        rows = self.ns["_scan_codex_file"](self.write("long.jsonl", [
            {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
            event(200_000, 200_000), event(400_000, 200_000), event(700_000, 300_000)]))
        self.assertEqual([r[6] for r in rows], [False, True])
        result = self.ns["cost_summary"]({"gpt": {"x": {"rows": rows}}})["svc"]["gpt"]
        self.assertAlmostEqual(result["total"], (400_000 * 2 + 40_000 * 10 + 300_000 * 4 + 30_000 * 15) / 1e6)

    def test_main_quota_maps_duration_not_primary_slot(self):
        parse = self.ns["codex_main_limits"]
        result = parse({"primary_window": {"used_percent": 44, "limit_window_seconds": 604800, "reset_at": 9},
                        "secondary_window": {"used_percent": 12, "limit_window_seconds": 18000, "reset_at": 8}})
        self.assertEqual((result["u5"], result["uw"], result["r5"], result["rw"]), (12, 44, 8, 9))
        result = parse({"primary": {"usedPercent": 44, "windowDurationMins": 10080, "resetsAt": 9}})
        self.assertFalse(result["has5"])
        self.assertTrue(result["hasw"])
        self.assertIsNone(result["r5"])

    def test_additional_quota_periods_and_bad_windows(self):
        parse = self.ns["codex_extra_limits"]
        def item(seconds):
            return {"limit_name": "Astra", "rate_limit": {"primary_window": {"used_percent": 12, "limit_window_seconds": seconds}}}
        result = parse({"additional_rate_limits": [item(3600), item(2592000), item(None), item("bad"), None]})
        self.assertEqual([r["name"] for r in result], ["Astra 1시간", "Astra 30일", "Astra 기간 미확인"])

    def test_claude_extra_formats_merge_without_duplicates(self):
        result = self.ns["claude_extra_limits"]({"limits": [None, {"kind": "weekly_opus", "percent": 12}],
                                               "seven_day_opus": {"utilization": 12}, "seven_day_sonnet": {"utilization": 22}})
        self.assertEqual([r["name"] for r in result], ["Opus 주간", "Sonnet 주간"])

    def test_log_fallback_preserves_model_bucket_and_weekly_only(self):
        root = self.dir / ".codex" / "sessions"
        root.mkdir(parents=True)
        now = datetime.now().isoformat()
        def event(bucket, minutes, used):
            return {"timestamp": now, "payload": {"rate_limits": {"limit_id": bucket,
                "primary": {"window_minutes": minutes, "used_percent": used, "resets_at": time.time() + 60}}}}
        p = self.write("quota.jsonl", [event("codex", 10080, 44), event("gpt-6-astra", 300, 12)])
        p.rename(root / p.name)
        result = self.ns["fetch_gpt_from_sessions"]()
        self.assertFalse(result["has5"])
        self.assertEqual(result["uw"], 44)
        self.assertEqual(result["extra"][0]["name"], "gpt-6-astra 5시간")

    def test_cost_cache_migrates_without_changing_source_logs(self):
        root = self.dir / ".codex" / "sessions"
        root.mkdir(parents=True)
        p = self.write("tokens.jsonl", [{"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}}])
        p = p.rename(root / p.name)
        original = p.read_bytes()
        cache_path = self.ns["COST_CACHE_PATH"]
        cache_path.write_text(json.dumps({"gpt": {str(p): {"m": p.stat().st_mtime, "s": p.stat().st_size,
                                                           "rows": [["bad old cache"]]}}}), encoding="utf-8")
        result = self.ns["scan_costs"]()
        self.assertEqual(result["version"], 3)
        self.assertEqual(result["gpt"][str(p)]["rows"], [])
        self.assertEqual(p.read_bytes(), original)

    def test_history_import_recognizes_five_hours_in_secondary_only(self):
        rl = {"limit_id": "codex", "primary": {"window_minutes": 10080, "used_percent": 22, "resets_at": 9},
              "secondary": {"window_minutes": 300, "used_percent": 44, "resets_at": 8}}
        self.assertTrue(self.ns["is_5h"](rl))
        self.assertFalse(self.ns["is_5h"]({**rl, "limit_id": "gpt-6-astra"}))


if __name__ == "__main__":
    unittest.main()
