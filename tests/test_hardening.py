import ast
import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import urllib.error
import urllib.request

import share
from hardening import app_dir, migrate_app_dir, atomic_json, config_values, valid_history, page_app, finite, bridge_message_allowed, usage_values
from providers.common import NoRedirect, RateLimited, read_json

ROOT = Path(__file__).resolve().parents[1]


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        self.saved_share = []
        self.ns = dict(time=time, json=json, threading=threading, Path=Path, share=share,
                       save_share_state=self.saved_share.append,
                       valid_history=valid_history, finite=finite, usage_values=usage_values,
                       ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed,
                       HISTORY_PATH=self.root / "history.jsonl", SERVICES=[], RESUME_DELAY=1, RESUME_LEAD=8,
                       provider_meta=SimpleNamespace(BY_KEY={}),
                       wkey=lambda v: v, on_main=lambda fn, *args: fn(*args), RateLimited=RateLimited,
                       hm=lambda t: time.strftime("%H:%M", time.localtime(t)))
        names = {"History", "Core", "WebWidget", "Api", "authorized_console_resume", "next_poll"}
        exec(compile(ast.Module(body=[n for n in source.body if getattr(n, "name", "") in names],
                                type_ignores=[]), "widget.py", "exec"), self.ns)

    def test_malformed_history_does_not_prevent_startup(self):
        row = dict(t=time.time(), s="claude", u5=25, uw=50, r5=None, rw=None)
        self.ns["HISTORY_PATH"].write_text('\n'.join(map(json.dumps, [{}, [], {**row, "u5": float('nan')}, row])), encoding="utf-8")
        h = self.ns["History"]()
        self.assertEqual(h.rows, [row])

    def test_history_remembers_missing_five_hour_window(self):
        # 5시간 창이 없는 계정(Claude Max)은 기록에 남겨, 켤 때 기록 값으로도 '5시간 한도 없음'을 보인다
        h = self.ns["History"]()
        h.add("claude", dict(u5=0, r5=None, uw=75, rw=None, has5=False))
        h.add("gpt", dict(u5=10, r5=None, uw=20, rw=None))
        again = self.ns["History"]()
        self.assertIs(again.last["claude"]["has5"], False)
        self.assertNotIn("has5", again.last["gpt"])

    def test_bad_config_defaults_and_control_characters(self):
        defaults = dict(poll_sec=60, auto_resume=False, resume_text="continue", theme="auto", prices={})
        for bad in (None, [], "bad"):
            self.assertEqual(config_values(defaults, bad), defaults)
        cfg = config_values(defaults, dict(poll_sec=-1, auto_resume="yes", resume_text="x\r\ncommand",
                                          prices={"bad": [float('nan')] * 5}, accounts=[{}, None]))
        self.assertEqual(cfg["poll_sec"], 60)
        self.assertFalse(cfg["auto_resume"])
        self.assertEqual(cfg["resume_text"], "continue")
        self.assertEqual(cfg["accounts"], [])
        self.assertEqual(cfg["prices"], {})

    def test_initial_two_service_defaults_and_saved_gemini_setting(self):
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        defaults = next(ast.literal_eval(n.value) for n in source.body if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "DEFAULTS" for t in n.targets))
        cfg = config_values(defaults, {})
        self.assertTrue(cfg["compact"])
        self.assertFalse(cfg["gemini_enabled"])
        cfg = config_values(defaults, {"gemini_enabled": True, "compact": False})
        self.assertTrue(cfg["gemini_enabled"])
        self.assertFalse(cfg["compact"])
        self.assertFalse(config_values(defaults, {"gemini_enabled": "true"})["gemini_enabled"])

    def test_service_card_names_are_limited_to_known_keys_and_plain_text(self):
        cfg = config_values(dict(names={}), {"names": {"claude": "메인", "gpt": "", "gemini": "줄\n바꿈", "account_x": "x",
                                                    "other": "x", 3: "x"}})
        self.assertEqual(cfg["names"], {"claude": "메인"})
        self.assertEqual(config_values(dict(names={}), {"names": ["claude"]})["names"], {})
        self.assertEqual(config_values(dict(names={}), {"names": {"gpt": "x" * 41}})["names"], {})

    def test_card_order_keeps_only_known_keys_once(self):
        defaults = dict(order=[])
        self.assertEqual(config_values(defaults, {"order": "gpt"})["order"], [])
        cfg = config_values(defaults, {"order": ["gpt", "account_a1", "gpt", "../x", 7, "account_", "claude", "gemini"]})
        self.assertEqual(cfg["order"], ["gpt", "account_a1", "claude", "gemini"])
        self.assertEqual(len(config_values(defaults, {"order": [f"account_{i}" for i in range(99)]})["order"]), 23)

    def test_legacy_app_dir_moves_to_new_name(self):
        with mock.patch.dict("os.environ", {"APPDATA": str(self.root)}), mock.patch("hardening.sys.platform", "win32"):
            old = self.root / "AIUsageWidget"
            old.mkdir()
            (old / "history.jsonl").write_text("x", encoding="utf-8")
            with mock.patch.object(Path, "rename", side_effect=OSError("in use")):
                migrate_app_dir(self.root)
            self.assertEqual(app_dir(self.root), old)  # 옮기지 못하면 예전 폴더를 계속 쓴다
            migrate_app_dir(self.root)
            self.assertEqual(app_dir(self.root), self.root / "QuotaTank")
            self.assertEqual((self.root / "QuotaTank" / "history.jsonl").read_text(encoding="utf-8"), "x")
            self.assertFalse(old.exists())

    def test_mac_quit_socket_moves_to_temp_when_path_is_too_long(self):
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        ns = dict(os=os, Path=Path)
        exec(compile(ast.Module(body=[n for n in source.body if getattr(n, "name", "") == "quit_socket_path"],
                                type_ignores=[]), "widget.py", "exec"), ns)
        short = self.root / "QuotaTank"
        self.assertEqual(ns["quit_socket_path"](short, "/tmp/u", 501), str(short / "quit.sock"))
        long = self.root / ("긴사용자이름" * 10) / "QuotaTank"
        self.assertEqual(ns["quit_socket_path"](long, "/tmp/u", 501), os.path.join("/tmp/u", "quotatank-501.sock"))

    def test_startup_migration_keeps_old_entry_when_new_one_fails(self):
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        old, new = self.root / "AIUsageWidget.vbs", self.root / "QuotaTank.vbs"
        old.write_text("old", encoding="utf-8")

        def failing_toggle():
            raise OSError("disk full")
        ns = dict(PACKAGED=False, LEGACY_STARTUP_FILE=old, STARTUP_FILE=new, toggle_startup=failing_toggle)
        exec(compile(ast.Module(body=[n for n in source.body if getattr(n, "name", "") == "migrate_startup"],
                                type_ignores=[]), "widget.py", "exec"), ns)
        ns["migrate_startup"]()
        self.assertTrue(old.exists())  # 새 등록이 실패하면 예전 등록을 지우지 않는다
        ns["toggle_startup"] = lambda: new.write_text("new", encoding="utf-8")
        ns["migrate_startup"]()
        self.assertEqual((old.exists(), new.read_text(encoding="utf-8")), (False, "new"))

    def test_atomic_save_keeps_previous_file_on_failure(self):
        path = self.root / "config.json"
        atomic_json(path, {"a": 1})
        with mock.patch("hardening.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                atomic_json(path, {"a": 2})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_redirect_cannot_forward_bearer_token(self):
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={"Authorization": "Bearer secret"})
        for code in (301, 302, 303, 307, 308):
            with self.assertRaises(urllib.error.HTTPError):
                NoRedirect().redirect_request(req, None, code, "redirect", {}, "https://attacker.invalid/")

    def test_credential_file_size_limit(self):
        path = self.root / "auth.json"
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            read_json(path)

    def request(self, app, path="/", **kwargs):
        result = {}
        env = dict(HTTP_HOST="127.0.0.1:12345", PATH_INFO=path, REQUEST_METHOD="GET")
        env.update(kwargs)
        body = b"".join(app(env, lambda status, headers: result.update(status=status, headers=dict(headers))))
        return result, body

    def test_web_server_denies_files_api_cross_site_and_rebinding(self):
        app = page_app(ROOT / "web", "main.html")
        for path in ("/../widget.py", "/auth.json", "/.credentials.json", "/js_api/test", "/main.html", "/remote.html", "/state.json", "/fonts/../../auth.json"):
            self.assertEqual(self.request(app, path)[0]["status"], "403 Forbidden")
        for env in (dict(HTTP_HOST="attacker.invalid:12345"), dict(HTTP_SEC_FETCH_SITE="cross-site"), dict(REQUEST_METHOD="POST")):
            self.assertEqual(self.request(app, **env)[0]["status"], "403 Forbidden")
        self.assertEqual(self.request(app, "/common.js")[0]["status"], "200 OK")
        result, body = self.request(app)
        self.assertEqual(result["status"], "200 OK")
        policy = result["headers"]["Content-Security-Policy"]
        self.assertIn("connect-src 'none'", policy)
        self.assertNotIn("'unsafe-eval'", policy)
        for script in re.findall(r"<script>([\s\S]*?)</script>", body.decode()):
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertIn("'sha256-" + digest + "'", policy)

    def test_mac_policy_allows_bridge_eval_but_not_inline_scripts(self):
        # WKWebView 는 pywebview 가 반환값을 돌려주는 eval 에도 CSP 를 건다. eval 만 열고 나머지는 그대로다
        policy = self.request(page_app(ROOT / "web", "main.html", allow_eval=True))[0]["headers"]["Content-Security-Policy"]
        script_src = re.search(r"script-src ([^;]*)", policy)[1].split()
        self.assertIn("'unsafe-eval'", script_src)
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertIn("connect-src 'none'", policy)

    def test_failed_refresh_keeps_success_timestamp(self):
        self.ns["SERVICES"] = [("claude", "Claude", mock.Mock(side_effect=OSError("secret path")))]
        app = SimpleNamespace(lock=threading.Lock(), cfg={}, next_at={}, limited={}, data={"claude": {"u5": 83}}, err={}, fetched_at=123, hist=mock.Mock())
        self.ns["Core"]._fetch_all(app)
        self.assertEqual(app.fetched_at, 123)
        self.assertEqual(app.data["claude"]["u5"], 83)
        self.assertNotIn("secret", app.err["claude"])

    def test_log_fallback_does_not_create_fresh_history(self):
        self.ns["SERVICES"] = [("gpt", "Codex", lambda: dict(src="log", observed_at=100, u5=83, uw=12))]
        app = SimpleNamespace(lock=threading.Lock(), cfg={}, next_at={}, limited={}, data={}, err={}, fetched_at=123, hist=mock.Mock())
        self.ns["Core"]._fetch_all(app)
        self.assertEqual(app.fetched_at, 123)
        app.hist.add.assert_not_called()

    def test_fetches_run_concurrently(self):
        barrier = threading.Barrier(3)
        def fetch():
            barrier.wait(timeout=2)
            return dict(src="live", u5=10, uw=12)
        self.ns["SERVICES"] = [(str(i), str(i), fetch) for i in range(3)]
        app = SimpleNamespace(lock=threading.Lock(), cfg={}, next_at={}, limited={}, data={}, err={}, fetched_at=0, hist=mock.Mock())
        self.ns["Core"]._fetch_all(app)
        self.assertEqual(len(app.data), 3)
        self.assertFalse(app.err)

    def test_resume_retries_failed_send_and_stops_after_success(self):
        class StopLoop(Exception): pass
        ticks = [1000]
        def sleep(_):
            ticks[0] += 60
            if ticks[0] > 1300:
                raise StopLoop()
        sender = mock.Mock(side_effect=[False, True])
        self.ns.update(time=SimpleNamespace(time=lambda: ticks[0], sleep=sleep),
                       limited_claude_sessions=lambda: [(1, "sid", "cwd", "limit", 10)],
                       resume_at=lambda *args: 100, type_into_console=sender)
        app = SimpleNamespace(lock=threading.Lock(), cfg=dict(auto_resume=True, resume_text="continue"), data={}, tray_notify=mock.Mock())
        with self.assertRaises(StopLoop):
            self.ns["WebWidget"].resume_loop(app)
        self.assertEqual(sender.call_count, 2)

    def test_resume_starts_the_helper_just_before_the_reset_and_times_it_to_the_second(self):
        class StopLoop(Exception): pass
        now = [5000.0]
        def sleep(_):
            now[0] += .5
            if now[0] > 5030:
                raise StopLoop()
        started = []
        def sender(pid, text, not_before):
            started.append((now[0], not_before))
            return True
        self.ns.update(time=SimpleNamespace(time=lambda: now[0], sleep=sleep),
                       limited_claude_sessions=lambda: [(1, "sid", "cwd", "limit", 10)],
                       resume_at=lambda *args: 5020.4, type_into_console=sender)
        app = SimpleNamespace(lock=threading.Lock(), cfg=dict(auto_resume=True, resume_text="continue"), data={}, tray_notify=mock.Mock())
        with self.assertRaises(StopLoop):
            self.ns["WebWidget"].resume_loop(app)
        self.assertEqual(len(started), 1)  # 보낸 뒤에는 다시 보내지 않는다
        at, not_before = started[0]
        self.assertTrue(5020.4 - 8 <= at <= 5020.4 - 7)  # 리셋 8초 전에 띄운다
        self.assertEqual(not_before, 5021.4)             # 입력은 리셋 1초 뒤

    def test_helper_waits_for_the_reset_but_never_skips_the_fresh_check(self):
        now, slept, send = [1000.0], [], mock.Mock(return_value=True)
        def sleep(sec):
            slept.append(sec)
            now[0] += sec
        session = (1, "sid", "cwd", "limit", 10)
        usage = dict(u5=100, uw=10)
        self.ns.update(time=SimpleNamespace(time=lambda: now[0], sleep=sleep),
                       load_config=lambda: dict(auto_resume=True, resume_text="continue"),
                       limited_claude_sessions=lambda: [session], provider_call=lambda op, key: dict(usage),
                       resume_at=lambda *args: 1005, console_type=send)
        self.assertFalse(self.ns["authorized_console_resume"](1, "continue", 1006))  # 아직 100%
        self.assertEqual(slept, [6])
        send.assert_not_called()
        usage["u5"] = 0
        self.assertTrue(self.ns["authorized_console_resume"](1, "continue", now[0] + 9999))  # 먼 시각을 줘도 13초만 기다린다
        self.assertEqual(slept[-1], 13)
        send.assert_called_once_with(1, "continue")
        # 기다리는 사이에 옵션을 끄면 보내지 않는다
        configs = iter([dict(auto_resume=True, resume_text="continue"), dict(auto_resume=False, resume_text="continue")])
        self.ns["load_config"] = lambda: next(configs)
        self.assertFalse(self.ns["authorized_console_resume"](1, "continue", now[0] + 5))
        send.assert_called_once()
        self.ns["load_config"] = lambda: dict(auto_resume=True, resume_text="continue")
        now[0] = 1000.0  # 리셋 전: 기다리라는 시각이 없으면 보내지 않는다
        self.assertFalse(self.ns["authorized_console_resume"](1, "continue"))
        send.assert_called_once()

    def test_helper_rejects_disabled_and_arbitrary_input(self):
        send = mock.Mock()
        self.ns.update(load_config=lambda: dict(auto_resume=False, resume_text="continue"), console_type=send)
        self.assertFalse(self.ns["authorized_console_resume"](1, "continue"))
        self.ns["load_config"] = lambda: dict(auto_resume=True, resume_text="continue")
        self.assertFalse(self.ns["authorized_console_resume"](1, "dangerous command"))
        send.assert_not_called()

    def test_reset_alert_survives_a_failed_poll_at_reset_time(self):
        app = SimpleNamespace(lock=threading.Lock(), alerted=set(), cfg=dict(reset_alert_min=80, poll_sec=60, accounts=[]),
                              stale_after=lambda: 180, data={"claude": dict(src="live", observed_at=1000, u5=95, r5=1100, uw=10, rw=None)})
        due = lambda now: self.ns["WebWidget"]._due_resets(app, watch, {"claude": "Claude"}, now)
        watch = {}
        self.assertEqual(due(1050), [])          # 최신 값으로 리셋 시각을 기억해 둔다
        app.data["claude"]["observed_at"] = 800  # 리셋 무렵 조회 실패 → 값이 오래됐다
        self.assertEqual(len(due(1101)), 1)      # 그래도 알린다
        self.assertEqual(due(1110), [])          # 한 번만

    def test_poll_interval_backs_off_on_429_and_slows_when_idle(self):
        next_poll, now = self.ns["next_poll"], 10000
        live = dict(src="live", u5=10, uw=5, r5=now + 3600, rw=now + 86400)
        self.assertEqual(next_poll(60, 0, live, None, now), (now + 60, 0))
        # 429: 2 → 4 → 8분, 이후 8분 유지. 서버가 더 기다리라면 그만큼(최대 1시간)
        self.assertEqual([next_poll(60, n, None, 0, now)[0] - now for n in range(4)], [120, 240, 480, 480])
        self.assertEqual(next_poll(60, 0, None, 900, now)[0] - now, 900)
        self.assertEqual(next_poll(60, 0, None, 99999, now)[0] - now, 3600)
        # 5시간 창 시작 전(대기)은 5분. 리셋이 그 전에 오면 리셋 직후
        idle = dict(live, r5=None)
        self.assertEqual(next_poll(60, 0, idle, None, now), (now + 300, 0))
        self.assertEqual(next_poll(60, 0, dict(idle, rw=now + 100), None, now)[0], now + 105)
        self.assertEqual(next_poll(60, 0, dict(live, r5=now + 20), None, now)[0], now + 25)
        # 주간 전용 계정(has5 False)은 대기로 보지 않는다
        self.assertEqual(next_poll(60, 0, dict(idle, has5=False), None, now)[0], now + 60)

    def test_poll_interval_default_hot_and_slow_recovery(self):
        next_poll, now = self.ns["next_poll"], 10000
        live = dict(src="live", u5=10, uw=5, r5=now + 3600, rw=now + 86400)
        self.assertEqual(self._config_ns()["DEFAULTS"]["poll_sec"], 300)
        self.assertEqual(next_poll(300, 0, live, None, now), (now + 300, 0))
        # 5시간·주간 사용률이 80% 이상이면 2분마다. 창이 끝난 값은 보지 않는다
        self.assertEqual(next_poll(300, 0, dict(live, u5=80), None, now)[0], now + 120)
        self.assertEqual(next_poll(300, 0, dict(live, uw=91, r5=None, has5=False), None, now)[0], now + 120)
        self.assertEqual(next_poll(300, 0, dict(live, u5=95, r5=now - 10), None, now)[0], now + 300)
        # 429 뒤 성공: 단계를 하나씩 내리며 8 → 4 → 2분 → 기본으로 돌아온다(한도 가까이라 기본이 2분일 때)
        hot = dict(live, u5=90)
        steps, level = [], 3
        while level:
            due, level = next_poll(300, level, hot, None, now)
            steps.append(due - now)
        self.assertEqual(steps, [480, 240, 120])
        self.assertEqual(next_poll(300, 0, hot, None, now)[0], now + 120)
        # 리셋 직후 조회는 단계와 상관없이 지킨다
        self.assertEqual(next_poll(300, 3, dict(hot, r5=now + 20), None, now)[0], now + 25)

    def _config_ns(self):
        """widget.py 의 DEFAULTS·load_config 만 떼어 실행한다(설정 파일 경로는 임시 폴더)."""
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        keep = [n for n in source.body if getattr(n, "name", "") == "load_config"
                or isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "DEFAULTS" for t in n.targets)]
        ns = dict(json=json, config_values=config_values, CONFIG_PATH=self.root / "config.json")
        exec(compile(ast.Module(body=keep, type_ignores=[]), "widget.py", "exec"), ns)
        return ns

    def test_old_default_poll_interval_is_raised(self):
        ns = self._config_ns()
        for stored, expected in ((60, 300), (120, 120), (None, 300)):
            ns["CONFIG_PATH"].write_text(json.dumps({} if stored is None else {"poll_sec": stored}), encoding="utf-8")
            self.assertEqual(ns["load_config"]()["poll_sec"], expected)

    def test_welcome_shows_only_on_first_run(self):
        ns = self._config_ns()
        self.assertFalse(ns["load_config"]()["onboarded"])  # 설정 파일이 없다: 처음 실행
        ns["CONFIG_PATH"].write_text(json.dumps({"poll_sec": 300}), encoding="utf-8")
        self.assertTrue(ns["load_config"]()["onboarded"])  # 예전부터 쓰던 사용자
        ns["CONFIG_PATH"].write_text(json.dumps({"onboarded": False}), encoding="utf-8")
        self.assertFalse(ns["load_config"]()["onboarded"])

    def _welcome_api(self):
        app = SimpleNamespace(lock=threading.Lock(), cfg={"onboarded": False, "reset_alert": True}, tray=None, wake=mock.Mock())
        api = self.ns["Api"](app)
        api.add_account = mock.Mock(return_value={"ok": True})
        api.set_gemini_enabled = mock.Mock(return_value={"ok": True})
        g = self.ns
        patches = {"save_config": mock.Mock(return_value=True), "PACKAGED": False,
                   "startup_enabled": mock.Mock(return_value=False), "toggle_startup": mock.Mock(return_value=True)}
        old = {k: g.get(k) for k in patches}
        g.update(patches)
        self.addCleanup(g.update, old)
        return app, api, patches

    def test_finish_welcome_skip_changes_nothing_but_the_flag(self):
        app, api, p = self._welcome_api()
        self.assertEqual(api.finish_welcome(None), {"ok": True, "failed": []})
        self.assertTrue(app.cfg["onboarded"])
        api.add_account.assert_not_called(); api.set_gemini_enabled.assert_not_called(); p["toggle_startup"].assert_not_called()
        p["save_config"].reset_mock()
        self.assertEqual(api.finish_welcome(None), {"ok": True, "failed": []})  # 이미 마쳤으면 다시 저장하지 않는다
        p["save_config"].assert_not_called()

    def test_finish_welcome_applies_choices(self):
        app, api, p = self._welcome_api()
        api.add_account.side_effect = [{"ok": True}, {"error": "이미 연결된 계정이에요"}]
        r = api.finish_welcome({"accounts": [{"provider": "claude", "path": "C:/a/.credentials.json"},
                                             {"provider": "gpt", "path": "C:/b/auth.json"}],
                                "gemini": True, "startup": True, "alerts": False})
        self.assertEqual(r["failed"], ["이미 연결된 계정이에요"])
        api.add_account.assert_any_call("claude", "", "C:/a/.credentials.json")
        api.set_gemini_enabled.assert_called_once_with(True)
        p["toggle_startup"].assert_called_once()
        self.assertFalse(app.cfg["reset_alert"])
        self.assertTrue(app.cfg["onboarded"])

    def test_finish_welcome_rejects_bad_input(self):
        app, api, p = self._welcome_api()
        for bad in ("x", {"accounts": "x", "gemini": True, "startup": True, "alerts": True},
                    {"accounts": [], "gemini": 1, "startup": True, "alerts": True},
                    {"accounts": [{}] * 21, "gemini": True, "startup": True, "alerts": True}):
            self.assertIn("error", api.finish_welcome(bad))
        self.assertFalse(app.cfg["onboarded"])
        p["save_config"].assert_not_called()

    def test_fetch_skips_services_waiting_after_429_even_on_refresh(self):
        calls = []
        def fetch(key):
            calls.append(key)
            if key == "a":
                raise RateLimited(None)
            return dict(src="live", u5=10, uw=5, r5=time.time() + 3600)
        self.ns["SERVICES"] = [(k, k, lambda k=k: fetch(k)) for k in ("a", "b")]
        app = SimpleNamespace(lock=threading.Lock(), cfg=dict(poll_sec=60), next_at={}, limited={}, data={}, err={},
                              fetched_at=0, hist=mock.Mock())
        self.ns["Core"]._fetch_all(app)
        self.assertIn("요청 많음", app.err["a"])
        self.assertEqual(app.limited, {"a": 1, "b": 0})
        calls.clear()
        self.ns["Core"]._fetch_all(app, force=True)  # 새로고침: b 는 바로, 429 로 기다리는 a 는 건너뛴다
        self.assertEqual(calls, ["b"])
        calls.clear()
        self.ns["Core"]._fetch_all(app)  # 둘 다 아직 때가 아니다
        self.assertEqual(calls, [])

    def test_gemini_is_not_polled_until_enabled_and_late_results_are_discarded(self):
        fetch = mock.Mock(return_value=dict(src="live", u5=10, uw=5))
        self.ns["SERVICES"] = [("gemini", "Gemini", fetch)]
        app = SimpleNamespace(lock=threading.Lock(), cfg=dict(poll_sec=60), next_at={}, limited={}, data={}, err={},
                              fetched_at=0, hist=mock.Mock())
        self.ns["Core"]._fetch_all(app, force=True)
        fetch.assert_not_called()
        app.cfg["gemini_enabled"] = True
        self.ns["Core"]._fetch_all(app)
        fetch.assert_called_once()
        self.assertIn("gemini", app.data)
        app.data.clear()
        app.hist.reset_mock()
        def turn_off():
            app.cfg["gemini_enabled"] = False
            return dict(src="live", u5=10, uw=5)
        fetch.side_effect = turn_off
        self.ns["Core"]._fetch_all(app, force=True)
        self.assertNotIn("gemini", app.data)
        app.hist.add.assert_not_called()

    def test_bridge_only_exposes_explicit_functions(self):
        api = self.ns["Api"](SimpleNamespace())
        win = mock.Mock()
        api._expose(win, insights=True)
        names = {fn.__name__ for fn in win.expose.call_args.args}
        self.assertEqual(names, {"insights", "theme", "detail_size", "fit_insights", "ins_minimize", "ins_close"})
        self.assertNotIn("_add_account", names)
        api._expose(win)
        names = {fn.__name__ for fn in win.expose.call_args.args}
        self.assertEqual(names, {
            "state", "refresh", "choose_account_file", "add_account", "login_options", "start_login", "cancel_login",
            "remove_account", "rename_account", "move_account", "switch_account", "set_gemini_enabled", "fit", "set_compact",
            "set_compact_hidden", "toggle_topmost", "toggle_startup", "theme", "set_theme", "open_insights",
            "toggle_shared_split", "set_share_plan", "calibrate_share", "reset_share_calibration", "toggle_remote_view",
            "toggle_cycle_start", "set_cycle_times", "toggle_boot_start", "set_credit", "set_credit_key", "welcome", "finish_welcome", "open_update", "quit"})
        self.assertIn("error", api.add_account("gpt", "bad", str(ROOT / "widget.py")))

    def test_share_settings_are_validated(self):
        defaults = dict(shared_split=False, share_plan="auto")
        for plan in ("pro", "max5", "max20", "auto"):
            self.assertEqual(config_values(defaults, {"share_plan": plan})["share_plan"], plan)
        for bad in ("max100", 5, None, ["pro"], ""):
            self.assertEqual(config_values(defaults, {"share_plan": bad})["share_plan"], "auto")
        self.assertTrue(config_values(defaults, {"shared_split": True})["shared_split"])
        self.assertFalse(config_values(defaults, {"shared_split": "yes"})["shared_split"])

    def share_app(self, shared_split=False, **data):
        now = time.time()
        d = dict(src="live", plan="Pro", u5=30.0, r5=now + 3600, uw=10.0, rw=now + 86400, observed_at=now)
        d.update(data)
        events = [(now - 600, 10.0, "opus"), (now - 100, 10.0, "fable")]
        return SimpleNamespace(lock=threading.Lock(), share_lock=threading.RLock(), share_ready=True, data={"claude": d},
                               hist=SimpleNamespace(svc_rows=lambda key: []), share_state={}, update_share=mock.Mock(), cfg=dict(shared_split=shared_split, share_plan="pro"),
                               share_events=lambda refresh=False: events), now

    def test_ended_window_teaches_the_coefficient_only_while_split_is_off(self):
        app, now = self.share_app()
        old = dict(src="live", plan="Pro", u5=30.0, r5=now - 100, uw=10.0, rw=now + 86400, observed_at=now - 50)
        new = dict(old, u5=1.0, r5=now + 18000 - 100)
        events = [(now - 5000, 10.0, "opus"), (now - 200, 5.0, "opus"), (now - 10, 50.0, "opus")]  # 마지막은 확인 뒤 사용
        app.share_events = lambda refresh=False: events
        self.ns["Core"].learn_share(app, old, new)
        self.assertEqual(app.share_state, {"learned": {"pro": {"h5": [[30.0, 15.0]]}}})
        self.assertEqual(self.saved_share, [app.share_state])
        # 켜져 있으면(공유 중)·비용 집계 전에는 배우지 않는다
        for split, ready in ((True, True), (False, False)):
            app2, _ = self.share_app(shared_split=split)
            app2.share_ready = ready
            app2.share_events = lambda refresh=False: events
            self.ns["Core"].learn_share(app2, old, new)
            self.assertEqual(app2.share_state, {})

    def test_reset_time_jitter_or_early_change_is_not_a_finished_window(self):
        app, now = self.share_app()
        old = dict(src="live", plan="Pro", u5=30.0, r5=now + 3600, uw=10.0, rw=now + 86400, observed_at=now)
        self.ns["Core"].learn_share(app, old, dict(old, r5=now + 3601))             # 같은 창(흔들림)
        self.ns["Core"].learn_share(app, old, dict(old, r5=now + 7200))             # 리셋 시각 전에 바뀜
        self.ns["Core"].learn_share(app, dict(old, src="log"), dict(old, r5=now))   # 이전 기록
        self.ns["Core"].learn_share(app, None, old)
        self.assertEqual(app.share_state, {})

    def test_calibrate_share_uses_current_window_and_rejects_bad_input(self):
        app, now = self.share_app(shared_split=True, u5=40.0)
        ok, msg = self.ns["Core"].calibrate_share(app, "h5")
        self.assertTrue(ok)
        self.assertEqual(app.share_state, {"manual": {"pro": {"h5": [40.0, 20.0]}}})  # 20달러에 40%
        app.update_share.assert_called_once()
        self.assertEqual(self.saved_share, [app.share_state])
        before = app.share_state
        for span in (None, "day"):
            self.assertFalse(self.ns["Core"].calibrate_share(app, span)[0])
        small, _ = self.share_app(shared_split=True, u5=3.0)
        self.assertFalse(self.ns["Core"].calibrate_share(small, "h5")[0])
        stale, _ = self.share_app(shared_split=True, src="log")
        self.assertFalse(self.ns["Core"].calibrate_share(stale, "h5")[0])
        ended, now = self.share_app(shared_split=True)
        ended.data["claude"]["r5"] = now - 1
        self.assertFalse(self.ns["Core"].calibrate_share(ended, "h5")[0])
        off, _ = self.share_app(u5=40.0)  # 꺼져 있으면 맞추지 않는다
        self.assertFalse(self.ns["Core"].calibrate_share(off, "h5")[0])
        early, _ = self.share_app(shared_split=True, u5=40.0)
        early.share_ready = False  # 비용 집계 전에는 주간 합계가 모자라 잘못 맞춘다
        self.assertFalse(self.ns["Core"].calibrate_share(early, "h5")[0])
        self.assertEqual(app.share_state, before)

    def test_update_share_waits_for_cost_scan_and_follows_the_switch(self):
        app, now = self.share_app(shared_split=True)
        app.share_ready, app.share = False, {"old": 1}
        self.ns["Core"].update_share(app)
        self.assertEqual(app.share, {})  # 비용 집계 전에는 주간 합계가 모자라 나누지 않는다
        app.share_ready = True
        self.ns["Core"].update_share(app)
        self.assertEqual(app.share["plan"], "pro")
        self.assertAlmostEqual(app.share["h5"]["m"], 20.0)
        app.cfg["shared_split"] = False
        self.ns["Core"].update_share(app)
        self.assertEqual(app.share, {})

    def test_native_bridge_rejects_traversal_forged_origin_and_callback_injection(self):
        origin = "http://127.0.0.1:12345/"
        def valid(name="state", call_id="123", source=origin, params="[]"):
            return bridge_message_allowed(json.dumps([name, params, call_id]), source, origin, {"state"})
        self.assertTrue(valid())
        self.assertFalse(valid("_app.api._add_account"))
        self.assertFalse(valid("__class__.__subclasses__"))
        self.assertFalse(valid(call_id='\"];alert(1);//'))
        self.assertFalse(valid(source="https://attacker.invalid/"))
        self.assertFalse(valid(params='{"unexpected": true}'))
        self.assertFalse(valid(params="[" * 2000))

    def test_usage_boundary_rejects_nonfinite_numbers_and_drops_unknown_fields(self):
        for value in (float('nan'), float('inf'), '<img>', None, True):
            with self.assertRaises(ValueError):
                usage_values(dict(u5=value, uw=10))
        # 서버가 100.5 나 -0.1 을 보내도 읽은 값은 살린다(0~100 으로 자른다)
        clamped = usage_values(dict(u5=-0.1, uw=100.5, extra=[dict(name="x", u=101)]))
        self.assertEqual((clamped["u5"], clamped["uw"], clamped["extra"][0]["u"]), (0, 100, 100))
        result = usage_values(dict(u5=10, uw=20, src="live", access_token="secret", extra=[dict(name="x", u=float('nan'))]))
        self.assertNotIn("access_token", result)
        self.assertEqual(result["extra"], [])


if __name__ == "__main__":
    unittest.main()
