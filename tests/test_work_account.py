"""작업 계정 바꾸기: 기본 로그인 자리와 추가 계정 폴더의 로그인 맞바꾸기. 임시 폴더의 가짜 로그인 파일만 쓴다."""
import ast
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import provider_worker  # noqa: E402
from providers import claude, codex, common, meta  # noqa: E402


def claude_login(folder, token, email, config=None):
    """folder 에 Claude 로그인 파일을 만든다. config: 계정 정보 파일(기본 자리는 ~/.claude.json)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": token, "refreshToken": token + "-r"}, "trustedDeviceToken": token + "-d",
        "mcpOAuth": {"server": folder.name}}), encoding="utf-8")
    (config or folder / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": email}, "projects": {"p": folder.name}}), encoding="utf-8")


def codex_login(folder, token, email):
    folder.mkdir(parents=True, exist_ok=True)
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    (folder / "auth.json").write_text(json.dumps({"tokens": {"access_token": token, "account_id": "id",
                                                             "id_token": f"x.{body}.y"}}), encoding="utf-8")


@unittest.skipIf(meta.MAC, "작업 계정 바꾸기는 윈도우만 지원한다")
class SwitchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        for target, attr, value in ((claude, "HOME", self.home), (codex, "HOME", self.home),
                                    (claude, "DEFAULT_FILE", self.home / ".claude" / ".credentials.json"),
                                    (codex, "DEFAULT_FILE", self.home / ".codex" / "auth.json"),
                                    (provider_worker, "CONFIG_PATH", self.home / "config.json")):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(codex, "running", return_value=False)
        self.running = p.start()
        self.addCleanup(p.stop)
        claude_login(self.home / ".claude", "main", "main@example.com", self.home / ".claude.json")
        claude_login(self.home / ".claude-work", "work", "work@example.com")
        claude_login(self.home / ".claude-side", "side", "side@example.com")
        codex_login(self.home / ".codex", "main", "main@example.com")
        codex_login(self.home / ".codex-work", "work", "work@example.com")
        self.register([("claude", ".claude-work/.credentials.json"), ("claude", ".claude-side/.credentials.json"),
                       ("gpt", ".codex-work/auth.json")])

    def register(self, items):
        accounts = [{"id": f"account_{i}", "provider": key, "name": key, "path": str(self.home / rel)}
                    for i, (key, rel) in enumerate(items)]
        (self.home / "config.json").write_text(json.dumps({"accounts": accounts}), encoding="utf-8")

    def ask(self, op, key, rel=None):
        reply = provider_worker.answer({"op": op, "provider": key, "path": rel and str(self.home / rel)})
        self.assertNotIn("work-r", json.dumps(reply, ensure_ascii=False))  # 토큰은 화면 쪽으로 나가지 않는다
        return reply

    def creds(self, folder):
        return json.loads((self.home / folder / ".credentials.json").read_text(encoding="utf-8"))

    def test_claude_switch_trades_only_the_account_and_back(self):
        self.assertEqual(self.ask("active", "claude"), {"ok": None})
        reply = self.ask("switch", "claude", ".claude-work/.credentials.json")
        self.assertEqual(Path(reply["ok"]), (self.home / ".claude-work" / ".credentials.json").resolve())
        live, parked = self.creds(".claude"), self.creds(".claude-work")
        self.assertEqual((live["claudeAiOauth"]["accessToken"], live["trustedDeviceToken"]), ("work", "work-d"))
        self.assertEqual((parked["claudeAiOauth"]["accessToken"], parked["trustedDeviceToken"]), ("main", "main-d"))
        # MCP 로그인처럼 PC 에 묶인 칸과 계정 정보 파일의 다른 칸은 제자리에 남는다
        self.assertEqual((live["mcpOAuth"], parked["mcpOAuth"]), ({"server": ".claude"}, {"server": ".claude-work"}))
        main_conf = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertEqual((main_conf["oauthAccount"]["emailAddress"], main_conf["projects"]), ("work@example.com", {"p": ".claude"}))
        self.assertEqual(claude.email(str(self.home / ".claude-work" / ".credentials.json")), "main@example.com")
        self.assertTrue((self.home / ".claude-work" / common.PARKED).is_file())
        self.assertEqual(self.ask("active", "claude"), reply)

        self.assertEqual(self.ask("switch", "claude", None), {"ok": None})
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "main")
        self.assertEqual(self.creds(".claude-work")["claudeAiOauth"]["accessToken"], "work")
        self.assertFalse((self.home / ".claude-work" / common.PARKED).exists())
        self.assertEqual([p.name for p in self.home.rglob("*" + common.NEXT)], [])

    def test_switching_between_extra_accounts_returns_the_first_one_home(self):
        self.ask("switch", "claude", ".claude-work/.credentials.json")
        self.ask("switch", "claude", ".claude-side/.credentials.json")
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "side")
        self.assertEqual(self.creds(".claude-work")["claudeAiOauth"]["accessToken"], "work")
        self.assertEqual(self.creds(".claude-side")["claudeAiOauth"]["accessToken"], "main")
        self.assertEqual([p.parent.name for p in self.home.glob("*/" + common.PARKED)], [".claude-side"])

    def test_switch_target_must_be_a_registered_login_folder(self):
        claude_login(self.home / ".claude-other", "other", "other@example.com")
        claude_login(self.home / "usb" / "claude", "usb", "usb@example.com")  # 연결은 됐지만 홈 바로 아래 설정 폴더가 아니다
        self.register([("claude", ".claude-work/.credentials.json"), ("claude", "usb/claude/.credentials.json")])
        for key, rel in (("claude", ".claude-other/.credentials.json"), ("claude", ".claude/.credentials.json"),
                         ("claude", "usb/claude/.credentials.json"),
                         ("gpt", ".claude-work/.credentials.json"), ("gemini", None)):
            self.assertIn("error", self.ask("switch", key, rel), rel)
        self.assertIn("error", self.ask("active", "claude", ".claude-work/.credentials.json"))
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "main")

    def test_interrupted_switch_is_finished_from_the_journal(self):
        folder = self.home / ".claude-work"
        with mock.patch.object(common, "finish_swap"):  # 일지만 남기고 끊겼다
            claude.swap(folder.resolve(), parked=True)  # 실제 전환은 늘 실제 경로(resolve)로 한다
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "main")
        self.assertEqual(Path(self.ask("active", "claude")["ok"]).parent, folder.resolve())
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "work")
        self.assertFalse((folder / common.JOURNAL).exists())

    def test_unfinished_preparation_without_journal_is_discarded(self):
        common.write_next(claude.DEFAULT_FILE, b"{}")
        self.assertEqual(self.ask("active", "claude"), {"ok": None})
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "main")
        self.assertEqual([p.name for p in self.home.rglob("*" + common.NEXT)], [])

    def test_journal_naming_other_files_is_refused(self):
        folder = self.home / ".claude-work"
        (folder / common.JOURNAL).write_text(json.dumps({"files": [str(self.home / "config.json")], "parked": True}),
                                             encoding="utf-8")
        self.assertIn("error", self.ask("active", "claude"))
        self.assertFalse((folder / common.PARKED).exists())

    def test_two_parked_folders_are_reported_instead_of_guessed(self):
        for name in (".claude-work", ".claude-side"):
            (self.home / name / common.PARKED).write_text("x", encoding="utf-8")
        self.assertIn("error", self.ask("switch", "claude", None))
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "main")

    def test_claude_code_locks_are_held_and_released(self):
        seen = []
        real = common.dir_lock

        def spy(path, stale, timeout=5.0):
            seen.append((Path(path).name, stale))
            return real(path, stale, timeout)
        with mock.patch.object(common, "dir_lock", spy):
            self.ask("switch", "claude", ".claude-work/.credentials.json")
        self.assertEqual(seen[:4], [(".oauth_refresh.lock", 60), (".claude.lock", 60),
                                    (".oauth_refresh.lock", 60), (".claude-work.lock", 60)])
        self.assertIn((".claude.json.lock", 10), seen)
        self.assertEqual([p for p in self.home.rglob("*.lock")], [])

    def test_busy_lock_times_out_and_stale_lock_is_taken(self):
        lock = self.home / "busy.lock"
        lock.mkdir()
        with self.assertRaises(RuntimeError):
            with common.dir_lock(lock, stale=60, timeout=0.3):
                pass
        os.utime(lock, (time.time() - 120, time.time() - 120))
        with common.dir_lock(lock, stale=60, timeout=0.3):
            self.assertTrue(lock.is_dir())
        self.assertFalse(lock.exists())

    def test_codex_switch_waits_until_codex_is_closed(self):
        self.running.return_value = True
        self.assertIn("Codex", self.ask("switch", "gpt", ".codex-work/auth.json")["error"])
        self.assertEqual(codex.email(), "main@example.com")
        self.running.return_value = False
        self.assertIn("ok", self.ask("switch", "gpt", ".codex-work/auth.json"))
        self.assertEqual((codex.email(), codex.email(str(self.home / ".codex-work" / "auth.json"))),
                         ("work@example.com", "main@example.com"))
        self.ask("switch", "gpt", None)
        self.assertEqual(codex.email(), "main@example.com")

    def test_broken_login_is_left_untouched(self):
        (self.home / ".claude-work" / ".credentials.json").write_text("{}", encoding="utf-8")
        self.assertIn("error", self.ask("switch", "claude", ".claude-work/.credentials.json"))
        self.assertEqual(self.creds(".claude")["claudeAiOauth"]["accessToken"], "main")
        self.assertEqual(self.ask("active", "claude"), {"ok": None})


class WidgetWorkTest(unittest.TestCase):
    """화면 프로세스 쪽: 작업 계정에 따라 카드마다 맞는 로그인 자리로 조회하고, 바꾸는 동안의 값은 버린다."""

    def setUp(self):
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        self.calls = []

        def call(op, key, path=None):
            self.calls.append((op, key, path))
            if op == "fetch":
                return {"u5": 10, "uw": 1, "src": "live"}
            if op == "active":
                return self.parked.get(key)
            if op == "switch":
                if self.refuse:
                    raise RuntimeError(self.refuse)
                self.parked[key] = path
                return path
        self.parked, self.refuse = {}, None
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from hardening import usage_values
        self.ns = dict(MAC=False, Path=Path, threading=threading, time=time, provider_meta=meta, provider_call=call,
                       SERVICES=[(p.KEY, p.NAME, lambda key=p.KEY: call("fetch", key)) for p in meta.ALL],
                       ThreadPoolExecutor=ThreadPoolExecutor, as_completed=as_completed, usage_values=usage_values,
                       next_poll=lambda *a: (0, 0), save_config=mock.Mock(return_value=True),
                       card_order=lambda cfg: ["claude", "gpt"] + [a["id"] for a in cfg.get("accounts", [])])
        exec(compile(ast.Module(body=[n for n in source.body if getattr(n, "name", "") in ("Api", "Core")], type_ignores=[]),
                     "widget.py", "exec"), self.ns)
        self.accounts = [{"id": "account_w", "provider": "claude", "name": "Work", "path": r"C:\h\.claude-work\.credentials.json"}]
        self.app = SimpleNamespace(cfg={"accounts": self.accounts}, lock=threading.Lock(), wake=threading.Event(),
                                   data={}, err={}, next_at={}, limited={}, work={}, work_gen=0, switching=False,
                                   hist=SimpleNamespace(add=mock.Mock()), fetched_at=0)
        self.app.sync_work = lambda: self.ns["Core"].sync_work(self.app)
        self.api = self.ns["Api"](self.app)

    def fetches(self):
        return [(key, path) for op, key, path in self.calls if op == "fetch" and key == "claude"]

    def test_cards_follow_their_account_after_switching(self):
        self.assertEqual(self.api.switch_account("account_w")["ok"], True)
        self.assertEqual(self.app.work, {"claude": "account_w"})
        self.calls.clear()
        self.ns["Core"]._fetch_all(self.app, force=True)
        # 작업 계정 카드는 기본 자리, 원래 기본 카드는 작업 계정의 폴더(맡겨 둔 곳)로 조회한다
        self.assertCountEqual(self.fetches(), [("claude", None), ("claude", self.accounts[0]["path"])])
        self.assertIn("account_w", self.app.data)

    def test_fetch_started_before_a_switch_is_discarded(self):
        def fetch_then_switch(op, key, path=None):
            self.app.work_gen += 1
            return {"u5": 10, "uw": 1, "src": "live"}
        self.ns["SERVICES"][:] = [("claude", "Claude", lambda: fetch_then_switch("fetch", "claude"))]
        self.app.cfg["accounts"] = []
        self.ns["Core"]._fetch_all(self.app, force=True)
        self.assertEqual(self.app.data, {})
        self.app.hist.add.assert_not_called()

    def test_failed_switch_reports_and_resyncs(self):
        self.refuse = "Claude Code가 로그인을 갱신하는 중이에요"
        self.assertEqual(self.api.switch_account("account_w"), {"error": self.refuse})
        self.assertEqual((self.app.work, self.app.switching), ({}, False))
        self.assertIn(("active", "claude", None), self.calls)

    def test_working_account_cannot_be_removed_and_bad_keys_are_refused(self):
        self.api.switch_account("account_w")
        self.assertIn("error", self.api.remove_account("account_w"))
        for key in ("gemini", "account_none", 3):
            self.assertIn("error", self.api.switch_account(key))
        self.assertIn("잠시 뒤", self.api.switch_account("claude")["error"])  # 연달아 바꾸지 않는다
        self.api._switch_at -= 60
        self.assertEqual(self.api.switch_account("claude")["ok"], True)
        self.assertEqual(self.app.work, {})
        self.assertEqual(self.api.remove_account("account_w"), {"ok": True})

    def test_switch_keeps_rate_limit_waits_and_blocks_removal_meanwhile(self):
        self.app.next_at = {"claude": 9e9, "account_w": 9e9}
        self.app.limited = {"claude": 2}
        self.api.switch_account("account_w")
        self.assertEqual(self.app.next_at, {"claude": 9e9})
        self.app.switching = True
        self.assertIn("error", self.api.remove_account("account_w"))

    def test_results_arriving_during_a_switch_are_not_stored(self):
        self.app.cfg["accounts"], self.app.switching = [], True
        self.ns["Core"]._fetch_all(self.app, force=True)
        self.assertEqual(self.app.data, {})
        self.assertIn("claude", self.app.next_at)  # 조회 간격은 그대로 정한다(곧바로 다시 묻지 않는다)

    def test_mac_is_not_supported_yet(self):
        self.ns["MAC"] = True
        self.assertIn("윈도우", self.api.switch_account("account_w")["error"])
        self.assertNotIn("switch", [c[0] for c in self.calls])


if __name__ == "__main__":
    unittest.main()
