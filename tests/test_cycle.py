"""사이클 예약: 정한 시각에 공식 CLI 로 한 마디를 보내 Claude 5시간 창을 시작한다. 실제 CLI·계정·설정 폴더를 쓰지 않는다."""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hardening import config_values, cycle_times  # noqa: E402

SOURCE = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))


def load(names, **ns):
    body = [n for n in SOURCE.body if getattr(n, "name", "") in names
            or isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in names]
    exec(compile(ast.Module(body=body, type_ignores=[]), "widget.py", "exec"), ns)
    return ns


class CycleTimesTest(unittest.TestCase):
    def test_accepts_only_valid_times(self):
        self.assertEqual(cycle_times("8:00, 13:30"), ["08:00", "13:30"])
        self.assertEqual(cycle_times(" 23:59;0:00  08:00,08:00 "), ["00:00", "08:00", "23:59"])
        self.assertEqual(cycle_times(["13:00", "7:05", 800, None, "24:00", "12:60", "1:5", "rm -rf", "09:00\n"]), ["07:05", "09:00", "13:00"])
        self.assertEqual(cycle_times("01:00 02:00 03:00 04:00 05:00 06:00"), ["01:00", "02:00", "03:00", "04:00"])  # 4개까지
        for bad in (None, 5, {}, "", "아침 8시", "08-00"):
            self.assertEqual(cycle_times(bad), [])

    def test_config_is_validated(self):
        defaults = dict(cycle_start=False, cycle_times=[])
        cfg = config_values(defaults, dict(cycle_start=True, cycle_times=["8:00", "bad", "13:00"]))
        self.assertEqual((cfg["cycle_start"], cfg["cycle_times"]), (True, ["08:00", "13:00"]))
        cfg = config_values(defaults, dict(cycle_start="yes", cycle_times="8:00"))
        self.assertEqual((cfg["cycle_start"], cfg["cycle_times"]), (False, ["08:00"]))
        self.assertEqual(config_values(defaults, {})["cycle_times"], [])


class DayStartUsageTest(unittest.TestCase):
    # 오늘 쓸 몫의 출발점: 하루 기준 시각의 주간 사용률
    f = staticmethod(load({"day_start_usage", "WEEK"})["day_start_usage"])

    def test_takes_the_last_reading_before_the_day_start(self):
        rw, start = 2_000_000_000, 2_000_000_000 - 3 * 86400
        rows = [dict(t=start - 7200, uw=40, rw=rw), dict(t=start - 60, uw=44, rw=rw + 60), dict(t=start + 600, uw=50, rw=rw)]
        self.assertEqual(self.f(rows, rw + 30, start), 44)  # resets_at 이 1분 안팎 흔들려도 같은 주

    def test_falls_back_to_the_first_reading_today(self):
        rw, start = 2_000_000_000, 2_000_000_000 - 3 * 86400
        rows = [dict(t=start - 600, uw=90, rw=rw - WEEK_S), dict(t=start + 600, uw=50, rw=rw), dict(t=start + 900, uw=52, rw=rw)]
        self.assertEqual(self.f(rows, rw, start), 50)  # 지난주 기록은 쓰지 않는다

    def test_new_week_today_starts_from_zero_and_unknown_is_none(self):
        start = 2_000_000_000
        self.assertEqual(self.f([], start + WEEK_S + 3600, start), 0.0)
        self.assertIsNone(self.f([dict(t=start, uw=5, rw=start + 86400)], start + 3 * 86400, start))
        self.assertIsNone(self.f([dict(t=start, uw=5, rw=None)], None, start))


WEEK_S = 7 * 86400


class CycleDueTest(unittest.TestCase):
    def setUp(self):
        self.due = load({"cycle_due", "CYCLE_GRACE"})["cycle_due"]

    def test_due_only_within_grace_and_once_a_day(self):
        at = lambda h, m, s=0: datetime(2026, 10, 12, h, m, s)
        self.assertEqual(self.due(["08:00"], at(7, 59, 59), set()), [])
        self.assertEqual(self.due(["08:00"], at(8, 0), set()), [("2026-10-12", "08:00")])
        self.assertEqual(self.due(["08:00"], at(8, 9, 59), set()), [("2026-10-12", "08:00")])
        self.assertEqual(self.due(["08:00"], at(8, 10), set()), [])  # PC 가 꺼져 있어 놓쳤으면 건너뛴다
        self.assertEqual(self.due(["08:00"], at(8, 1), {("2026-10-12", "08:00")}), [])
        self.assertEqual(self.due(["08:00"], at(8, 1), {("2026-10-11", "08:00")}), [("2026-10-12", "08:00")])  # 어제 보낸 것과 별개
        self.assertEqual(self.due(["08:00", "08:05", "13:00"], at(8, 6), set()), [("2026-10-12", "08:00"), ("2026-10-12", "08:05")])


class RunCycleTest(unittest.TestCase):
    NOW = datetime(2026, 10, 12, 8, 0, 30)

    def setUp(self):
        self.start = mock.Mock(return_value=True)
        self.ns = load({"Core", "WebWidget", "Api", "cycle_due", "CYCLE_GRACE"}, start_claude_window=self.start,
                       save_config=mock.Mock(), on_main=lambda fn, *a: fn(*a), cycle_times=cycle_times)
        self.app = SimpleNamespace(cfg=dict(cycle_start=True, cycle_times=["08:00"]), lock=threading.Lock(), data={},
                                   wake=threading.Event(), tray_notify=mock.Mock(), tray=mock.Mock())
        self.run = lambda fired, now=self.NOW: self.ns["WebWidget"].run_cycle(self.app, fired, now)

    def test_starts_an_idle_window_once(self):
        ts = self.NOW.timestamp()
        for data in ({}, {"claude": dict(src="live", r5=None, u5=0, uw=10, rw=ts + 9000)},
                     {"claude": dict(src="live", r5=ts - 60, u5=0, uw=10, rw=ts + 9000)},   # 지난 창
                     {"claude": dict(src="log", r5=ts + 3600, u5=50, uw=10, rw=ts + 9000)}):  # 오래된 기록은 믿지 않는다
            self.app.data, fired = data, set()
            self.start.reset_mock()
            self.app.wake.clear()
            self.run(fired)
            self.run(fired)  # 같은 예약은 하루에 한 번만
            self.start.assert_called_once_with()
            self.assertTrue(self.app.wake.is_set())
        self.assertIn("08:00", self.app.tray_notify.call_args[0][0])

    def test_leaves_a_running_window_or_a_full_week_alone(self):
        ts = self.NOW.timestamp()
        for d in (dict(src="live", r5=ts + 3600, u5=40, uw=10, rw=ts + 9000),
                  dict(src="live", r5=None, u5=0, uw=100, rw=ts + 9000),
                  dict(src="live", has5=False, r5=None, u5=0, uw=10, rw=ts + 9000),   # 5시간 창이 없는 계정(Max)
                  dict(src="log", has5=False, r5=None, u5=0, uw=10, rw=ts + 9000)):  # 켤 때 기록 값으로도 안다
            self.app.data, fired = {"claude": d}, set()
            self.run(fired)
            self.assertEqual(fired, {("2026-10-12", "08:00")})  # 다뤘다고 적어 두어, 창이 곧 끝나도 늦게 보내지 않는다
        self.start.assert_not_called()
        self.app.tray_notify.assert_not_called()

    def test_reports_failure_without_retrying(self):
        self.start.return_value = False
        fired = set()
        self.run(fired)
        self.run(fired)
        self.start.assert_called_once_with()
        self.assertIn("시작하지 못했어요", self.app.tray_notify.call_args[0][0])
        self.assertFalse(self.app.wake.is_set())

    def test_close_schedules_send_only_once(self):
        self.app.cfg["cycle_times"] = ["08:00", "08:05"]
        fired = set()
        self.run(fired, datetime(2026, 10, 12, 8, 6))  # PC 가 08:06 에 깨어났다: 둘 다 때가 됐어도 한 번만 보낸다
        self.run(fired, datetime(2026, 10, 12, 8, 7))
        self.start.assert_called_once_with()
        self.assertEqual(len(fired), 2)

    def test_nothing_outside_the_schedule(self):
        self.run(set(), datetime(2026, 10, 12, 9, 0))
        self.app.cfg["cycle_times"] = []
        self.run(set())
        self.start.assert_not_called()

    def test_bridge_functions(self):
        api = self.ns["Api"](self.app)
        self.assertFalse(api.toggle_cycle_start())
        self.assertTrue(api.toggle_cycle_start())
        self.app.tray.update_menu.assert_called()
        self.assertEqual(api.set_cycle_times("7:30, x, 26:00, 19:00"), ["07:30", "19:00"])
        self.assertEqual(self.app.cfg["cycle_times"], ["07:30", "19:00"])
        for bad in (None, 5, ["rm -rf"], {"a": 1}):
            self.assertEqual(api.set_cycle_times(bad), [])
        self.assertEqual(self.ns["save_config"].call_count, 7)


class BootStartTest(unittest.TestCase):
    """켤 때 Claude 시작과, PC 가 꺼져 있어 놓친 예약에 클라우드 루틴을 권하는 알림."""
    NOW = datetime(2026, 10, 12, 9, 0, 0)

    def setUp(self):
        self.start = mock.Mock(return_value=True)
        threads = SimpleNamespace(Thread=lambda target, daemon: SimpleNamespace(start=target), Lock=threading.Lock)
        self.ns = load({"Core", "WebWidget", "Api", "cycle_missed", "CYCLE_GRACE", "BOOT_WINDOW", "CLOUD_TIP"},
                       start_claude_window=self.start, threading=threads, time=SimpleNamespace(time=lambda: self.NOW.timestamp()),
                       provider_meta=SimpleNamespace(TOKEN_EXPIRED="토큰 만료"), save_config=mock.Mock(), on_main=lambda fn, *a: fn(*a))
        W = self.ns["WebWidget"]
        self.app = SimpleNamespace(cfg=dict(boot_start=True, cycle_start=True, cycle_times=["08:00", "13:00"]),
                                   lock=threading.Lock(), err={"claude": "토큰 만료 · Claude Code를 켜면 곧 다시 조회해요"},
                                   wake=threading.Event(), tray_notify=mock.Mock(), tray=mock.Mock(), cycle_fired=set(),
                                   boot_until=self.NOW.timestamp() + 600, boot_done=False)
        for name in ("boot_start", "_boot_send", "woke", "tip_missed_cycles"):
            setattr(self.app, name, getattr(W, name).__get__(self.app))

    def test_expired_token_sends_once_per_boot(self):
        self.assertTrue(self.app.boot_start())
        self.assertFalse(self.app.boot_start())  # 켤 때마다 한 번
        self.start.assert_called_once_with()
        self.assertTrue(self.app.wake.is_set())
        self.assertIn("토큰을 갱신", self.app.tray_notify.call_args[0][0])
        self.app.woke(self.NOW, self.NOW)  # 잠에서 깨면 다시 한 번 할 수 있다
        self.assertTrue(self.app.boot_start())
        self.assertEqual(self.start.call_count, 2)

    def test_only_when_on_expired_and_just_booted(self):
        for change in (dict(cfg={**self.app.cfg, "boot_start": False}), dict(err={}),
                       dict(err={"claude": "요청 많음 · 10:00에 다시 조회"}), dict(boot_until=self.NOW.timestamp() - 1)):
            app = SimpleNamespace(**{**vars(self.app), **change})
            app.boot_start = self.ns["WebWidget"].boot_start.__get__(app)
            self.assertFalse(app.boot_start())
        self.start.assert_not_called()

    def test_failure_is_reported(self):
        self.start.return_value = False
        self.app.boot_start()
        self.assertIn("시작하지 못했어요", self.app.tray_notify.call_args[0][0])
        self.assertFalse(self.app.wake.is_set())

    def test_missed_cycles(self):
        missed = self.ns["cycle_missed"]
        at = lambda h, m: datetime(2026, 10, 12, h, m)
        self.assertEqual(missed(["08:00", "13:00"], at(0, 0), at(9, 0)), [("2026-10-12", "08:00")])
        self.assertEqual(missed(["08:00"], at(8, 5), at(9, 0)), [])     # 08:00 에는 깨어 있었다
        self.assertEqual(missed(["08:00"], at(7, 0), at(8, 9)), [])     # 아직 예약을 보낼 수 있는 10분 안
        self.assertEqual(missed(["08:00"], at(7, 0), at(8, 10)), [("2026-10-12", "08:00")])

    def test_missed_cycle_recommends_cloud_routine_once(self):
        self.app.tip_missed_cycles(datetime(2026, 10, 12, 0, 0), self.NOW)
        self.app.tip_missed_cycles(datetime(2026, 10, 12, 0, 0), self.NOW)
        self.app.tray_notify.assert_called_once()
        msg = self.app.tray_notify.call_args[0][0]
        self.assertIn("08:00", msg)
        self.assertIn("/schedule", msg)
        self.assertLessEqual(len(msg), 255)  # 윈도우 알림 길이
        self.app.cfg["cycle_start"] = False
        self.app.cycle_fired.clear()
        self.app.tip_missed_cycles(datetime(2026, 10, 12, 0, 0), self.NOW)
        self.app.tray_notify.assert_called_once()  # 예약을 끈 사람에게는 권하지 않는다

    def test_bridge_toggle(self):
        api = self.ns["Api"](self.app)
        self.assertFalse(api.toggle_boot_start())
        self.assertTrue(api.toggle_boot_start())
        self.app.tray.update_menu.assert_called()


class StartWindowTest(unittest.TestCase):
    def setUp(self):
        self.proc = mock.Mock()
        self.proc.wait.return_value = 0
        self.run = mock.Mock(return_value=self.proc)
        self.stop = mock.Mock()
        self.cli = [os.path.abspath(os.path.join(os.sep, "Tools", "claude.exe"))]
        self.environ = dict(PATH="p", ANTHROPIC_API_KEY="k", anthropic_base_url="u", CLAUDE_CODE_USE_BEDROCK="1",
                            CLAUDE_CONFIG_DIR="d", CLAUDE_OTHER="keep")
        fake_os = SimpleNamespace(environ=self.environ, path=os.path, pathsep=os.pathsep)
        self.ns = load({"start_claude_window", "CYCLE_ARGS"}, find_cli=lambda name: self.cli[0] if name == "claude" else None,
                       tempfile=tempfile, shutil=shutil, os=fake_os, MAC=False, MAC_CLI_DIRS=(), stop_login=self.stop,
                       subprocess=SimpleNamespace(Popen=self.run, DEVNULL=subprocess.DEVNULL, CREATE_NO_WINDOW=0x08000000))

    def test_runs_the_official_cli_with_fixed_arguments(self):
        self.assertTrue(self.ns["start_claude_window"]())
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], [self.cli[0], "-p", "hi", "--safe-mode", "--model", "haiku",
                                   "--no-session-persistence", "--tools", ""])
        self.assertEqual((kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]), (subprocess.DEVNULL,) * 3)
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.proc.wait.assert_called_once_with(timeout=120)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"], dict(PATH="p", CLAUDE_OTHER="keep"))  # 다른 인증·프로필을 가리키는 값은 넘기지 않는다
        self.stop.assert_not_called()
        folder = Path(kwargs["cwd"])
        self.assertTrue(folder.name.startswith("quotatank-cycle-"))
        self.assertFalse(folder.exists())  # 빈 임시 폴더에서 돌리고 지운다

    def test_reports_failure(self):
        self.proc.wait.return_value = 1
        self.assertFalse(self.ns["start_claude_window"]())
        self.stop.assert_not_called()
        self.proc.wait.side_effect = subprocess.TimeoutExpired("claude", 120)
        self.assertFalse(self.ns["start_claude_window"]())
        self.stop.assert_called_once_with(self.proc)  # 끝나지 않은 CLI 는 자식까지 끝낸다
        self.assertFalse(Path(self.run.call_args[1]["cwd"]).exists())

    def test_missing_or_unsafe_cli_is_not_run(self):
        self.run.reset_mock()
        root = os.path.abspath(os.sep)
        for cli in (None, "claude.cmd", os.path.join(".", "claude.cmd"), os.path.join(root, "Tools & more", "claude.cmd"),
                    os.path.join(root, "Tools", "claude%x%.cmd"), os.path.join(root, "a,b", "claude.cmd")):
            self.cli[0] = cli
            self.assertFalse(self.ns["start_claude_window"]())
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
