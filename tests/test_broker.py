"""자격 증명 브로커 경계. 실제 로그인 파일·네트워크는 쓰지 않는다(임시 폴더의 가짜 로그인 파일만 읽는다)."""
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import provider_worker  # noqa: E402


def fake_codex_login(folder, email="work@example.com"):
    body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    file = Path(folder) / "auth.json"
    file.write_text(json.dumps({"tokens": {"access_token": "secret-token", "account_id": "id",
                                           "id_token": f"x.{body}.y"}}), encoding="utf-8")
    return file


class Pipe:
    """Broker 가 쓰는 출력 스트림 대역."""
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.append(json.loads(data))

    def flush(self):
        pass


class BrokerProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_ui_process_never_loads_credential_modules(self):
        code = ("import sys, widget; bad = [m for m in ('providers.claude', 'providers.codex', 'providers.gemini', 'providers.anthropic_api',"
                " 'providers.common', 'provider_worker') if m in sys.modules]; print(bad)")
        home = str(self.dir)  # import 가 실제 설정 폴더를 만들지 않게
        env = {**os.environ, "APPDATA": home, "HOME": home, "USERPROFILE": home}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_widget_and_broker_agree_on_the_questions(self):
        import ast
        tree = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ProviderBroker")
        ops = next(ast.literal_eval(n.value) for n in cls.body
                   if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "OPS")
        self.assertEqual(ops, provider_worker.OPS)
        self.assertIn("switch", ops)

    def test_only_typed_results_leave_the_broker(self):
        file = fake_codex_login(self.dir)
        reply = provider_worker.answer({"op": "inspect", "provider": "gpt", "path": str(file)})
        self.assertEqual(reply, {"ok": {"valid": True, "email": "work@example.com"}})
        self.assertNotIn("secret-token", json.dumps(reply))

    def test_rejects_unknown_ops_extra_fields_and_other_files(self):
        file = fake_codex_login(self.dir)
        other = self.dir / "notes.txt"
        other.write_text("private", encoding="utf-8")
        for request in ({"op": "read", "provider": "gpt", "path": str(file)},
                        {"op": "inspect", "provider": "gpt", "path": str(file), "raw": True},
                        {"op": "inspect", "provider": "gpt", "path": str(other)},
                        {"op": "inspect", "provider": "gpt", "path": "auth.json"},
                        {"op": "inspect", "provider": "gemini", "path": str(file)},
                        {"op": "profiles", "provider": "gpt", "path": str(file)},
                        ["inspect"]):
            self.assertIn("error", provider_worker.answer(request), request)
        # 다른 서비스의 로그인 파일은 유효하지 않다고만 답한다
        self.assertEqual(provider_worker.answer({"op": "inspect", "provider": "claude", "path": str(file)}),
                         {"ok": {"valid": False, "email": None}})

    def test_renamed_login_file_needs_registration_before_fetch(self):
        renamed = self.dir / "work-codex.json"
        fake_codex_login(self.dir).rename(renamed)
        config = self.dir / "config.json"
        # 연결 전 확인은 JSON 파일이면 받는다(답은 유효 여부·이메일뿐)
        self.assertTrue(provider_worker.answer({"op": "inspect", "provider": "gpt", "path": str(renamed)})["ok"]["valid"])
        with mock.patch.object(provider_worker, "CONFIG_PATH", config),                 mock.patch("providers.codex.fetch", return_value={"u5": 1, "uw": 2}) as fetch:
            self.assertIn("error", provider_worker.answer({"op": "fetch", "provider": "gpt", "path": str(renamed)}))
            fetch.assert_not_called()
            config.write_text(json.dumps({"accounts": [{"id": "account_x", "provider": "gpt", "name": "Work",
                                                        "path": str(renamed)}]}), encoding="utf-8")
            self.assertEqual(provider_worker.answer({"op": "fetch", "provider": "gpt", "path": str(renamed)}),
                             {"ok": {"u5": 1, "uw": 2}})
            # 다른 서비스로 등록된 경로는 안 된다
            self.assertIn("error", provider_worker.answer({"op": "fetch", "provider": "claude", "path": str(renamed)}))

    def test_symlink_to_another_file_is_rejected(self):
        target = self.dir / "secret.json"
        target.write_text(json.dumps({"tokens": {"access_token": "x", "account_id": "y"}}), encoding="utf-8")
        link = self.dir / "link" / "auth.json"
        link.parent.mkdir()
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("이 환경에서는 심볼릭 링크를 만들 수 없다")
        with mock.patch.object(provider_worker, "CONFIG_PATH", self.dir / "none.json"):
            self.assertIn("error", provider_worker.answer({"op": "fetch", "provider": "gpt", "path": str(link)}))

    def test_rate_limit_is_passed_as_a_signal(self):
        from providers.net import RateLimited
        with mock.patch.object(provider_worker, "handle", side_effect=RateLimited(120)):
            self.assertEqual(provider_worker.answer({})["retry_after"], 120)
        # Codex 기본 계정은 429 여도 세션 로그 값을 보여 주고, 간격을 늘리라고 알린다
        from providers import codex
        with mock.patch.object(codex, "http_json", side_effect=RateLimited(None)),                 mock.patch.object(codex, "read_json", return_value={"tokens": {"access_token": "x", "account_id": "y"}}),                 mock.patch.object(codex, "fetch_from_sessions", return_value={"u5": 1, "uw": 2, "src": "log"}):
            self.assertEqual(codex.fetch()["retry_after"], 0)
            with self.assertRaises(RateLimited):
                codex.fetch(str(self.dir / "auth.json"))

    def test_os_error_details_are_not_forwarded(self):
        with mock.patch.object(provider_worker, "handle", side_effect=OSError(r"C:\Users\me\.codex\auth.json denied")):
            reply = provider_worker.answer({"op": "fetch", "provider": "gpt", "path": None})
        self.assertEqual(reply, {"error": "계정 조회 실패"})
        with mock.patch.object(provider_worker, "handle", side_effect=RuntimeError("토큰 만료 — 다시 로그인")):
            self.assertEqual(provider_worker.answer({})["error"], "토큰 만료 — 다시 로그인")

    def serve(self, *messages, rate=None):
        out = Pipe()
        broker = provider_worker.Broker(out)
        raw = b"".join(m if isinstance(m, bytes) else json.dumps(m).encode() + b"\n" for m in messages)
        with mock.patch.object(provider_worker, "RATE", rate or provider_worker.RATE):
            broker.serve(io.BytesIO(raw))
        broker.pool.shutdown(wait=True)
        return out.lines

    def test_rate_limit_answers_without_running_requests(self):
        with mock.patch.object(provider_worker, "handle", return_value=1) as handle:
            lines = self.serve(*({"id": i, "op": "default_email", "provider": "gpt"} for i in range(1, 6)), rate=(3, 60))
        self.assertEqual(handle.call_count, 3)
        self.assertEqual(sorted(line["id"] for line in lines), [1, 2, 3, 4, 5])
        self.assertEqual(sum("error" in line for line in lines), 2)

    def test_protocol_violation_ends_the_session(self):
        with mock.patch.object(provider_worker, "handle", return_value=1) as handle:
            lines = self.serve({"id": "x", "op": "fetch", "provider": "gpt"},
                               {"id": 2, "op": "fetch", "provider": "gpt"})
            self.assertEqual((lines, handle.call_count), ([], 0))
            lines = self.serve(b"x" * (provider_worker.MAX_REQUEST + 10) + b"\n", {"id": 2, "op": "fetch", "provider": "gpt"})
            self.assertEqual((lines, handle.call_count), ([], 0))


class BrokerClientTest(unittest.TestCase):
    """widget.ProviderBroker 가 실제 브로커 프로세스(python widget.py --provider-worker)와 이야기한다."""

    @classmethod
    def setUpClass(cls):
        # 위젯 모듈과 브로커 프로세스가 실제 설정 폴더 대신 임시 폴더를 보게 한다(브로커는 환경 변수를 물려받는다)
        cls.home = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.home.cleanup)
        env = mock.patch.dict(os.environ, {"APPDATA": cls.home.name, "HOME": cls.home.name, "USERPROFILE": cls.home.name})
        env.start()
        cls.addClassCleanup(env.stop)
        from hardening import app_dirs
        cls.new, cls.legacy = app_dirs(cls.home.name)
        cls.legacy.mkdir(parents=True)
        import widget
        cls.widget = widget

    def test_import_does_not_move_settings_folder(self):
        self.assertTrue(self.legacy.is_dir())
        self.assertFalse(self.new.exists())
        self.assertEqual(self.widget.APP_DIR, self.legacy)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.broker = self.widget.ProviderBroker()
        self.addCleanup(lambda: self.broker.proc and self.broker.proc.kill())

    def test_round_trip_reuses_one_process(self):
        file = fake_codex_login(self.temp.name)
        for _ in range(3):
            self.assertEqual(self.broker.call("inspect", "gpt", str(file)), {"valid": True, "email": "work@example.com"})
        self.assertEqual(len(self.broker.starts), 1)
        with self.assertRaises(RuntimeError):
            self.broker.call("inspect", "gpt", str(Path(self.temp.name) / "missing" / "auth.json"))

    def test_crashing_broker_is_not_restarted_endlessly(self):
        self.broker.starts = [time.monotonic()] * 5
        with mock.patch.object(self.widget.subprocess, "Popen") as popen:
            with self.assertRaises(RuntimeError):
                self.broker.call("default_email", "gpt")
            popen.assert_not_called()

    def test_client_refuses_ops_outside_the_list(self):
        with self.assertRaises(ValueError):
            self.broker.call("read_file", "gpt", "C:/x")
        self.assertIsNone(self.broker.proc)

    def test_dead_broker_fails_pending_call_and_restarts(self):
        file = fake_codex_login(self.temp.name)
        self.broker.call("inspect", "gpt", str(file))
        first = self.broker.proc
        first.kill()
        first.wait(10)
        deadline = time.time() + 10
        while self.broker.proc is first and time.time() < deadline:
            threading.Event().wait(0.05)
        self.assertEqual(self.broker.call("inspect", "gpt", str(file))["valid"], True)
        self.assertIsNot(self.broker.proc, first)
        self.assertEqual(len(self.broker.starts), 2)


if __name__ == "__main__":
    unittest.main()
