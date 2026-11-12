"""계정 연결·로그인 연결·서비스 모듈. 실제 로그인 파일과 앱 데이터는 읽지 않는다(임시 폴더 + 필요한 정의만 꺼내 쓴다)."""
import ast
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from hardening import usage_values
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import provider_worker  # noqa: E402
import providers  # noqa: E402
from providers import claude, codex, meta  # noqa: E402


def broker_call(op, key, path=None):
    """테스트용 브로커: 별도 프로세스 대신 같은 검사·처리 코드(provider_worker.answer)를 바로 부른다."""
    reply = provider_worker.answer({"op": op, "provider": key, "path": path})
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["ok"]


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        # 서비스 모듈이 진짜 홈 폴더 대신 임시 폴더를 보게 한다
        for target, attr, value in ((claude, "HOME", self.home), (codex, "HOME", self.home),
                                    (claude, "DEFAULT_FILE", self.home / ".claude" / ".credentials.json"),
                                    (codex, "DEFAULT_FILE", self.home / ".codex" / "auth.json"),
                                    (meta.CLAUDE, "DEFAULT_FILE", self.home / ".claude" / ".credentials.json"),
                                    (meta.CODEX, "DEFAULT_FILE", self.home / ".codex" / "auth.json")):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.http = mock.Mock()
        for mod in (claude, codex):
            p = mock.patch.object(mod, "http_json", self.http)
            p.start()
            self.addCleanup(p.stop)

        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        names = {"Api", "Core", "card_order", "new_profile_dir", "launch_login", "stop_login", "find_cli"}
        self.ns = dict(MAC=False, os=SimpleNamespace(environ={"PATH": "x"}, pathsep=";", path=__import__("ntpath")), sys=SimpleNamespace(platform="win32"),
                       shutil=SimpleNamespace(which=lambda name, path: rf"C:\cli\{name}.exe"), subprocess=mock.MagicMock(),
                       login_supported=lambda p: (True, ""),
Path=Path, json=json, uuid=uuid, re=re, HOME=self.home, threading=threading, time=time,
                       provider_meta=meta, provider_call=broker_call,
                       save_config=mock.Mock(return_value=True), SERVICES=[],
                       ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed, usage_values=usage_values)
        exec(compile(ast.Module(body=[n for n in source.body if getattr(n, "name", "") in names], type_ignores=[]),
                     "widget.py", "exec"), self.ns)
        self.app = SimpleNamespace(cfg={}, lock=threading.Lock(), wake=threading.Event(), data={}, err={}, login=None,
                                   next_at={}, limited={})
        self.api = self.ns["Api"](self.app)

    def credentials(self, provider, folder=None, email=None):
        """서비스의 로그인 파일을 만든다. email 을 주면 그 서비스 방식대로 이메일도 넣는다."""
        folder = self.home / (folder or provider)
        folder.mkdir(parents=True, exist_ok=True)
        if provider == "claude":
            file = folder / ".credentials.json"
            file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-claude"}}), encoding="utf-8")
            if email:
                (folder / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}), encoding="utf-8")
        else:
            file = folder / "auth.json"
            tokens = {"access_token": "test-codex", "account_id": "test-id"}
            if email:
                import base64
                body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
                tokens["id_token"] = f"x.{body}.y"
            file.write_text(json.dumps({"tokens": tokens}), encoding="utf-8")
        self.api._selected_files.add(str(file.resolve()))
        return str(file)

    def test_add_validate_duplicate_remove_and_no_token_storage(self):
        path = self.credentials("claude", email="work@example.com")
        self.assertIn("error", self.api.add_account("gpt", "Work", path))
        self.assertTrue(self.api.add_account("claude", "", path)["ok"])
        account = self.app.cfg["accounts"][0]
        self.assertEqual((account["name"], account["email"]), ("work@example.com", "work@example.com"))
        self.assertNotIn("test-claude", json.dumps(self.app.cfg))
        self.assertIn("error", self.api.add_account("claude", "Duplicate", path))
        self.assertTrue(self.app.wake.is_set())
        self.app.data[account["id"]] = {"u5": 10}
        self.api.remove_account(account["id"])
        self.assertEqual(self.app.cfg["accounts"], [])
        self.assertEqual(self.app.data, {})

    def test_same_email_as_default_or_other_folder_is_rejected(self):
        # 기본 계정과 같은 사람으로 다시 로그인한 경우
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}), encoding="utf-8")
        self.assertIn("me@example.com", self.api.add_account("claude", "", self.credentials("claude", "a", "me@example.com"))["error"])
        # 다른 폴더지만 이미 연결한 계정과 같은 사람
        self.assertTrue(self.api.add_account("gpt", "", self.credentials("gpt", "x", "work@example.com"))["ok"])
        self.assertIn("error", self.api.add_account("gpt", "", self.credentials("gpt", "y", "work@example.com")))

    def test_broker_questions_are_asked_without_holding_the_shared_lock(self):
        def call(op, key, path=None):
            self.assertFalse(self.app.lock.locked(), op)  # 잠금을 쥔 채 기다리면 화면·트레이가 멈춘다
            return broker_call(op, key, path)
        self.ns["provider_call"] = call
        self.assertTrue(self.api.add_account("gpt", "", self.credentials("gpt", "x", "a@example.com"))["ok"])

    def test_large_claude_settings_file_still_gives_email(self):
        path = self.credentials("claude", "big")
        (self.home / "big" / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "big@example.com"}, "projects": {"x": "y" * (3 * 1024 * 1024)}}),
            encoding="utf-8")
        self.assertEqual(claude.email(path), "big@example.com")

    def test_save_failure_does_not_report_success(self):
        self.ns["save_config"].return_value = False
        self.assertIn("error", self.api.add_account("gpt", "Work", self.credentials("gpt")))
        self.assertEqual(self.app.cfg["accounts"], [])

    def test_unsupported_provider_is_rejected(self):
        self.assertIn("error", self.api.add_account("gemini", "", self.credentials("claude")))
        self.assertIn("error", self.api.add_account("nope", "", self.credentials("claude")))

    def test_profile_folder_names_are_unique_and_safe(self):
        first = self.ns["new_profile_dir"](claude, "회사 계정!")
        self.assertEqual(first, self.home / ".claude-회사-계정")
        first.mkdir()
        self.assertEqual(self.ns["new_profile_dir"](claude, "회사 계정!"), self.home / ".claude-회사-계정-2")
        self.assertTrue(self.ns["new_profile_dir"](codex, "../..").name.startswith(".codex-"))
        self.assertEqual(self.ns["new_profile_dir"](codex, "../..").parent, self.home)

    def test_windows_login_runs_cli_without_a_visible_terminal(self):
        sp = self.ns["subprocess"]
        proc = sp.Popen.return_value
        proc.poll.return_value = None
        self.assertTrue(self.api.start_login("gpt", "회사")["ok"])
        args, kwargs = sp.Popen.call_args
        self.assertEqual(args[0], [r"C:\cli\codex.exe", "-c", "cli_auth_credentials_store=file", "login"])  # cmd.exe 를 거치지 않는다
        self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.home / ".codex-회사"))
        self.assertIs(self.app.login["proc"], proc)
        # 취소하면 숨겨 둔 프로세스를 트리째 끝낸다
        self.api.cancel_login()
        self.assertIsNone(self.app.login)
        killed = sp.run.call_args[0][0]
        self.assertTrue(killed[0].endswith(r"System32\taskkill.exe"))  # 이름만으로 찾지 않는다
        self.assertEqual(killed[1:3], ["/PID", str(proc.pid)])
        self.assertIn("/T", killed)

    def test_second_login_is_refused_while_the_first_is_starting(self):
        sp = self.ns["subprocess"]

        def launch(*a, **k):  # 첫 로그인을 띄우는 도중에 버튼이 한 번 더 눌린 경우
            self.assertIn("error", self.api.start_login("gpt", "둘째"))
            return mock.Mock()
        sp.Popen.side_effect = launch
        self.assertTrue(self.api.start_login("gpt", "첫째")["ok"])
        self.assertEqual(sp.Popen.call_count, 1)

    def test_failed_launch_frees_the_login_slot(self):
        self.ns["subprocess"].Popen.side_effect = OSError("no")
        self.assertIn("error", self.api.start_login("gpt", "회사"))
        self.assertIsNone(self.app.login)

    def test_finished_login_process_is_not_killed_again(self):
        proc = mock.Mock()
        proc.poll.return_value = 0
        self.ns["stop_login"](proc)
        self.ns["stop_login"](None)
        self.ns["subprocess"].run.assert_not_called()

    def mac_login_ns(self, installed):
        """맥 분기의 find_cli·launch_login 만 꺼낸다. PATH 에는 시스템 폴더뿐(.app·LaunchAgent 실행과 같다)."""
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        ns = dict(MAC=True, HOME=self.home, MAC_CLI_DIRS=("/opt/homebrew/bin",), json=json,
                  os=SimpleNamespace(environ={"PATH": "/usr/bin:/bin"}, pathsep=":", path=__import__("posixpath"),
                                     close=mock.Mock(), read=mock.Mock(return_value=b""), killpg=mock.Mock()),
                  shutil=SimpleNamespace(which=lambda name, path: installed if "/opt/homebrew/bin" in path.split(":") else None),
                  subprocess=mock.Mock(), threading=mock.Mock(), sys=SimpleNamespace(platform="darwin"))
        exec(compile(ast.Module(body=[n for n in source.body
                                      if getattr(n, "name", "") in ("find_cli", "launch_login", "stop_login")],
                                type_ignores=[]), "widget.py", "exec"), ns)
        return ns

    def test_mac_finds_cli_outside_launchd_path(self):
        ns = self.mac_login_ns("/opt/homebrew/bin/codex")
        self.assertEqual(ns["find_cli"]("codex"), "/opt/homebrew/bin/codex")

    def test_mac_login_runs_cli_on_a_pty_without_terminal_app(self):
        ns = self.mac_login_ns("/opt/homebrew/bin/codex")
        folder = self.home / '.codex-회사 "a"'
        with mock.patch.dict(sys.modules, {"pty": SimpleNamespace(openpty=lambda: (7, 8))}):  # 윈도우에는 pty 가 없다
            proc = ns["launch_login"](meta.CODEX, folder)
        args, kwargs = ns["subprocess"].Popen.call_args
        self.assertEqual(args[0], ["/opt/homebrew/bin/codex", "-c", "cli_auth_credentials_store=file", "login"])  # 셸·osascript 없이
        self.assertEqual(kwargs["env"]["CODEX_HOME"], str(folder))
        self.assertEqual(kwargs["env"]["PATH"].split(":")[0], "/opt/homebrew/bin")  # npm CLI 가 node 를 찾을 수 있게
        self.assertEqual((kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]), (8, 8, 8))
        self.assertTrue(kwargs["start_new_session"])
        ns["os"].close.assert_called_once_with(8)  # 위젯 쪽 slave 는 닫고 master 는 읽는 스레드가 닫는다
        ns["threading"].Thread.call_args.kwargs["target"]()
        ns["os"].close.assert_called_with(7)
        # 취소하면 그룹째 끝낸다
        proc.poll.return_value = None
        ns["stop_login"](proc)
        self.assertEqual(ns["os"].killpg.call_args[0][0], proc.pid)

    def test_mac_failed_launch_closes_the_pty(self):
        ns = self.mac_login_ns("/opt/homebrew/bin/codex")
        ns["subprocess"].Popen.side_effect = OSError("no")
        with mock.patch.dict(sys.modules, {"pty": SimpleNamespace(openpty=lambda: (7, 8))}):
            with self.assertRaises(OSError):
                ns["launch_login"](meta.CODEX, self.home / ".codex-x")
        self.assertEqual(sorted(c.args[0] for c in ns["os"].close.call_args_list), [7, 8])

    def test_find_profiles_skips_default_folder(self):
        self.credentials("claude", ".claude")
        work = self.credentials("claude", ".claude-work")
        self.credentials("gpt", ".codex")
        self.assertEqual([str(p) for p in claude.find_profiles()], [work])
        self.assertEqual(codex.find_profiles(), [])

    def test_extra_codex_failure_never_uses_default_session_logs(self):
        self.http.side_effect = RuntimeError("offline")
        with mock.patch.object(codex, "fetch_from_sessions") as logs:
            with self.assertRaises(RuntimeError):
                codex.fetch(self.credentials("gpt"))
            logs.assert_not_called()
            codex.fetch()
            logs.assert_called_once()

    def test_claude_uses_selected_profile(self):
        self.http.return_value = {"five_hour": {"utilization": 23}}
        with mock.patch.object(claude, "credentials") as default:
            result = claude.fetch(self.credentials("claude"))
            default.assert_not_called()
        self.assertEqual(result["u5"], 23)
        self.assertEqual(self.http.call_args.args[1]["Authorization"], "Bearer test-claude")

    def test_claude_expired_token_is_not_sent(self):
        # 만료된 토큰은 usage 가 429 와 긴 Retry-After 로 답해, 갱신 뒤에도 조회가 막힌다: 보내지 않는다
        file = Path(self.credentials("claude"))
        now = time.time()
        file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-claude", "expiresAt": (now - 3600) * 1000}}), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "토큰 만료"):
            claude.fetch(str(file))
        self.http.assert_not_called()
        self.http.return_value = {"five_hour": {"utilization": 5}}
        file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "test-claude", "expiresAt": (now + 3600) * 1000}}), encoding="utf-8")
        self.assertEqual(claude.fetch(str(file))["u5"], 5)
        self.assertFalse(claude.expired({"accessToken": "x"}))  # 만료 시각이 없으면 조회한다
        self.assertTrue(claude.expired({"expiresAt": (now + 30) * 1000}, now))  # 곧 만료되는 토큰도 보내지 않는다

    def test_claude_missing_five_hour_window_is_not_zero(self):
        # 2026-11 응답: five_hour 가 null 이고 limits 에도 session 이 없다 → 0% 가 아니라 '없음'(화면 —)
        self.http.return_value = {
            "five_hour": None,
            "seven_day": {"utilization": 75.0, "resets_at": "2026-11-08T06:00:00+00:00"},
            "limits": [
                {"kind": "weekly_all", "group": "weekly", "percent": 75, "resets_at": "2026-11-08T06:00:00+00:00", "scope": None},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 3, "resets_at": "2026-11-08T06:00:00+00:00",
                 "scope": {"model": {"id": None, "display_name": "Opus"}, "surface": None}},
            ]}
        result = claude.fetch(self.credentials("claude"))
        self.assertIs(result["has5"], False)
        self.assertIsNone(result["r5"])
        self.assertIs(result["hasw"], True)
        self.assertEqual(result["uw"], 75.0)
        self.assertEqual(result["extra"], [{"name": "Opus 주간", "u": 3.0, "r": result["rw"]}])
        clean = usage_values(result)
        self.assertIs(clean["has5"], False)

    def test_claude_reads_session_from_limits_when_old_field_is_gone(self):
        self.http.return_value = {"limits": [
            {"kind": "session", "group": "session", "percent": 41, "resets_at": "2026-11-06T10:00:00+00:00"},
            {"kind": "weekly_all", "group": "weekly", "percent": 70, "resets_at": "2026-11-08T06:00:00+00:00"}]}
        result = claude.fetch(self.credentials("claude"))
        self.assertEqual((result["u5"], result["uw"]), (41, 70))
        self.assertTrue(result["has5"] and result["hasw"])
        self.assertIsNotNone(result["r5"])
        self.assertEqual(result["extra"], [])  # 5시간·전체 주간은 추가 한도로 다시 보이지 않는다

    def test_codex_weekly_only_live_response_keeps_model_limits(self):
        self.http.return_value = {"plan_type": "pro", "rate_limit": {
            "primary_window": {"used_percent": 44, "limit_window_seconds": 604800, "reset_at": 10}},
            "additional_rate_limits": [{"limit_name": "Astra", "rate_limit": {
                "primary_window": {"used_percent": 12, "limit_window_seconds": 3600}}}]}
        result = codex.fetch(self.credentials("gpt"))
        self.assertEqual(result["src"], "live")
        self.assertFalse(result["has5"])
        self.assertEqual(result["uw"], 44)
        self.assertEqual(result["extra"][0]["name"], "Astra 1시간")

    def test_inflight_fetch_does_not_restore_removed_account(self):
        account = {"id": "account_test", "name": "Work", "provider": "claude", "path": self.credentials("claude")}
        self.app.cfg["accounts"] = [account]

        def fetch(path):
            self.api.remove_account(account["id"])
            return {"u5": 23, "uw": 1, "src": "live"}
        self.app.hist = SimpleNamespace(add=mock.Mock())
        with mock.patch.object(claude, "fetch", fetch):
            self.ns["Core"]._fetch_all(self.app)
        self.assertEqual(self.app.data, {})
        self.app.hist.add.assert_not_called()

    def test_rename_preserves_connection_and_readings(self):
        account = {"id": "account_test", "name": "회사", "provider": "claude", "path": "fake.json", "email": "test@example.com"}
        self.app.cfg["accounts"] = [account]
        self.app.data[account["id"]] = {"u5": 23}
        self.assertEqual(self.api.rename_account(account["id"], "  업무용  "), {"ok": True})
        self.assertEqual(self.app.cfg["accounts"][0], dict(account, name="업무용"))
        self.assertEqual(self.app.data[account["id"]], {"u5": 23})

    def test_rename_rejects_invalid_input_and_unknown_account(self):
        self.app.cfg["accounts"] = [{"id": "account_test", "name": "회사"}]
        for name in (None, "", "  ", "x" * 41, "이름\n줄", "이름\x00끝"):
            self.assertIn("error", self.api.rename_account("account_test", name))
        self.assertIn("error", self.api.rename_account("account_missing", "이름"))
        self.assertEqual(self.app.cfg["accounts"][0]["name"], "회사")

    def test_rename_default_service_card_keeps_its_name_separately(self):
        self.app.cfg.update(accounts=[], gemini_enabled=False)
        self.assertEqual(self.api.rename_account("claude", " 메인 "), {"ok": True})
        self.assertEqual(self.app.cfg["names"], {"claude": "메인"})
        self.assertIn("error", self.api.rename_account("gemini", "꺼 둔 서비스"))
        self.app.cfg["gemini_enabled"] = True
        self.assertEqual(self.api.rename_account("gemini", "제미나이"), {"ok": True})
        self.assertEqual(self.app.cfg["names"], {"claude": "메인", "gemini": "제미나이"})
        self.ns["save_config"].return_value = False
        self.assertIn("error", self.api.rename_account("gpt", "코덱스"))
        self.assertEqual(self.app.cfg["names"], {"claude": "메인", "gemini": "제미나이"})

    def test_rename_rolls_back_if_save_fails(self):
        previous = [{"id": "account_test", "name": "회사"}]
        self.app.cfg["accounts"] = previous
        self.ns["save_config"].return_value = False
        self.assertIn("error", self.api.rename_account("account_test", "업무용"))
        self.assertIs(self.app.cfg["accounts"], previous)

    def test_move_changes_only_the_card_order(self):
        accounts = [{"id": "account_a", "name": "회사"}, {"id": "account_b", "name": "개인"}]
        self.app.cfg.update(accounts=accounts, gemini_enabled=False)
        order = lambda: self.ns["card_order"](self.app.cfg)
        self.assertEqual(order(), ["claude", "gpt", "account_a", "account_b"])  # 저장한 순서가 없으면 연결한 순서
        self.assertEqual(self.api.move_account("account_b", -1), {"ok": True})
        self.assertEqual(self.api.move_account("account_b", -1), {"ok": True})
        self.assertEqual(order(), ["claude", "account_b", "gpt", "account_a"])
        self.assertEqual(self.api.move_account("claude", 1), {"ok": True})
        self.assertEqual(order(), ["account_b", "claude", "gpt", "account_a"])
        self.assertEqual(self.api.move_account("account_b", -1), {"ok": True})  # 맨 앞에서는 그대로
        self.assertEqual(order(), ["account_b", "claude", "gpt", "account_a"])
        self.assertIs(self.app.cfg["accounts"], accounts)
        # 새로 켠 서비스·새 계정은 맨 뒤에 붙고, 삭제한 계정은 순서에서 빠진다
        self.app.cfg.update(gemini_enabled=True, accounts=[accounts[0], {"id": "account_c", "name": "새 계정"}])
        self.assertEqual(order(), ["claude", "gpt", "account_a", "gemini", "account_c"])

    def test_move_rejects_bad_input_and_rolls_back_if_save_fails(self):
        self.app.cfg.update(accounts=[{"id": "account_a", "name": "회사"}], order=["gpt", "claude", "account_a"])
        for key, step in ((None, 1), ("account_a", "1"), ("account_a", 2), ("account_a", True), ("account_a", 1.0),
                          ("account_missing", 1), ("gemini", 1)):
            self.assertIn("error", self.api.move_account(key, step))
        self.ns["save_config"].return_value = False
        self.assertIn("error", self.api.move_account("account_a", -1))
        self.assertEqual(self.app.cfg["order"], ["gpt", "claude", "account_a"])

    def test_gemini_toggle_clears_old_data_and_wakes_polling(self):
        self.assertEqual(self.api.set_gemini_enabled(True), {"ok": True})
        self.assertTrue(self.app.cfg["gemini_enabled"])
        self.assertTrue(self.app.wake.is_set())
        for target in (self.app.data, self.app.err, self.app.next_at, self.app.limited):
            target["gemini"] = 1
        self.assertEqual(self.api.set_gemini_enabled(False), {"ok": True})
        for target in (self.app.data, self.app.err):
            self.assertNotIn("gemini", target)
        self.assertFalse(self.app.cfg["gemini_enabled"])
        # 끄고 켜도 429 대기 시간을 우회하지 않는다.
        self.assertEqual(self.api.set_gemini_enabled(True), {"ok": True})
        self.assertEqual(self.app.next_at["gemini"], 1)
        self.assertEqual(self.app.limited["gemini"], 1)

    def test_gemini_visibility_and_disabling_the_last_visible_ring(self):
        self.app.cfg["compact_hidden"] = ["claude", "gpt", "gemini"]
        self.api.set_gemini_enabled(True)
        self.assertEqual(self.app.cfg["compact_hidden"], ["claude", "gpt"])
        self.api.set_gemini_enabled(False)
        self.assertEqual(self.app.cfg["compact_hidden"], ["gpt"])

    def test_gemini_toggle_rejects_non_boolean_and_rolls_back(self):
        for value in ("true", 1, None, []):
            self.assertIn("error", self.api.set_gemini_enabled(value))
        self.ns["save_config"].return_value = False
        self.assertIn("error", self.api.set_gemini_enabled(True))
        self.assertFalse(self.app.cfg["gemini_enabled"])

    def test_remove_cleans_visibility_and_rolls_back_on_save_failure(self):
        previous = [{"id": "account_test", "name": "회사"}]
        self.app.cfg.update(accounts=previous, compact_hidden=["account_test", "claude"])
        self.ns["save_config"].return_value = False
        self.assertIn("error", self.api.remove_account("account_test"))
        self.assertEqual(self.app.cfg["compact_hidden"], ["account_test", "claude"])
        self.ns["save_config"].return_value = True
        self.assertEqual(self.api.remove_account("account_test"), {"ok": True})
        self.assertEqual(self.app.cfg["compact_hidden"], ["claude"])
        self.assertIn("error", self.api.remove_account("account_test"))

    def test_every_provider_follows_the_module_contract(self):
        self.assertEqual([p.KEY for p in providers.ALL], [p.KEY for p in meta.ALL])
        for p in providers.ALL:
            self.assertTrue(p.KEY and p.NAME and callable(p.fetch))
            info = meta.BY_KEY[p.KEY]  # 화면 프로세스가 보는 정보와 브로커 쪽 모듈이 같아야 한다
            self.assertEqual((p.NAME, p.STATUS_URL, p.LOGIN), (info.NAME, info.STATUS_URL, info.LOGIN))
            if p.LOGIN:
                self.assertTrue({"env", "cmd", "file", "cli"} <= set(p.LOGIN))
                for fn in ("valid", "email", "find_profiles"):
                    self.assertTrue(callable(getattr(p, fn)), f"{p.KEY}.{fn}")


if __name__ == "__main__":
    unittest.main()
