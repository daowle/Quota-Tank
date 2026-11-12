"""VS Code 확장 패널 이어 가기. 실제 창·키 입력·편집기는 쓰지 않고 함수만 골라 mock 으로 돌린다."""
import ast
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# VS Code 패널 이어 가기는 윈도우 전용이고, 아래 테스트는 윈도우 경로(C:\...)로 실행 파일·폴더를 맞춘다
WINDOWS_ONLY = unittest.skipUnless(sys.platform == "win32", "윈도우 전용(VS Code 패널 이어 가기)")

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
EXE = r"C:\Apps\Microsoft VS Code\Code.exe"
CWD = r"C:\work\proj"
LIMIT = "You've hit your session limit · resets 3:45pm (Asia/Seoul)"


def load(names, **ns):
    exec(compile(ast.Module(body=[n for n in SOURCE.body if getattr(n, "name", "") in names], type_ignores=[]),
                 "widget.py", "exec"), ns)
    return ns


class StopLoop(Exception):
    pass


@WINDOWS_ONLY
class PanelTypeTest(unittest.TestCase):
    def setUp(self):
        self.startfile, self.sent, self.raised = mock.Mock(), [], []
        self.front = [(1, EXE)]  # 앞에 나와 있는 창
        self.ui = [(5, "Message input", "", True), (5, "Message input", "continue", False)]  # 입력 전, Enter 직전
        self.markers = []

        def focused_ui(marker=None):
            self.markers.append(marker)
            return self.ui.pop(0) if self.ui else None

        def raise_window(hwnd):
            self.raised.append(hwnd.value)
            self.front[0] = (hwnd.value, EXE)
            return 1
        user32 = SimpleNamespace(AllowSetForegroundWindow=lambda _: 1, SetForegroundWindow=raise_window)
        self.ns = load({"panel_type", "panel_precheck", "panel_window", "limit_marker", "text_events", "enter_events"},
                       re=re, Path=Path, PANEL_INPUT_NAME="Message input",
                       EDITORS={"code.exe": ("vscode", "Code")}, os=SimpleNamespace(startfile=self.startfile),
                       time=SimpleNamespace(sleep=lambda _: None),
                       ctypes=SimpleNamespace(windll=SimpleNamespace(user32=user32), c_void_p=lambda v: SimpleNamespace(value=v)),
                       panel_editor=lambda pid: EXE, user_away=lambda: True, last_input_tick=lambda: 100,
                       editor_windows=lambda exe: [(1, 77, "a.py - proj - Visual Studio Code"), (2, 77, "b.py - other - Visual Studio Code")],
                       foreground_window=lambda: self.front[0], process_exe=lambda pid: EXE, focused_ui=focused_ui,
                       ctrl_enter_to_send=lambda exe: False, send_input=lambda events: self.sent.append(events) or True)

    def run_type(self, sid="sid", text="continue", cwd=CWD, message=LIMIT):
        return self.ns["panel_type"](1, sid, text, cwd, message)

    def test_sends_only_when_every_check_passes(self):
        self.assertEqual(self.run_type(), "sent")
        self.startfile.assert_called_once_with("vscode://anthropic.claude-code/open?session=sid")
        self.assertEqual(self.raised, [])                      # 이미 그 창이 앞에 있으면 건드리지 않는다
        self.assertEqual(self.markers, ["resets3:45pm", None])  # 입력 전에만 그 세션의 한도 안내가 보이는지 확인한다
        self.assertEqual(len(self.sent), 2)                    # 글자 한 번에, Enter 한 번에
        self.assertEqual(len(self.sent[0]), 2 * len("continue"))
        self.assertEqual([e[0] for e in self.sent[1]], [0x0D, 0x0D])

    def test_raises_the_window_of_the_session_folder_first(self):
        self.front[0] = (2, EXE)  # 다른 폴더의 편집기 창이 앞에 있다
        self.assertEqual(self.run_type(), "sent")
        self.assertEqual(self.raised, [1])
        self.startfile.assert_called_once()

    def test_gives_up_before_opening_when_the_window_cannot_come_forward(self):
        self.front[0] = (2, EXE)
        self.ns["ctypes"].windll.user32.SetForegroundWindow = lambda hwnd: 0  # 윈도우가 막았다
        self.assertEqual(self.run_type(), "skip")
        self.startfile.assert_not_called()  # 다른 창에 세션을 열지 않는다
        self.assertEqual(self.sent, [])

    def test_rejects_before_touching_the_editor(self):
        two_same = lambda exe: [(1, 77, "a.py - proj - Visual Studio Code"), (2, 77, "proj - Visual Studio Code")]
        tab_only = lambda exe: [(2, 77, "proj - other - Visual Studio Code")]  # 폴더가 아니라 열린 파일 이름이 같다
        cases = [dict(sid="x&prompt=rm"), dict(text="a\nb"), dict(message="limit reached"), dict(message=None),
                 dict(cwd=""), dict(cwd=r"C:\work\elsewhere"), dict(editor_windows=two_same), dict(editor_windows=tab_only),
                 dict(editor_windows=lambda exe: None), dict(editor_windows=lambda exe: []),
                 dict(panel_editor=lambda pid: None), dict(user_away=lambda: False)]
        for case in cases:
            ns = dict(case)
            args = {k: ns.pop(k) for k in ("sid", "text", "cwd", "message") if k in ns}
            with mock.patch.dict(self.ns, ns):
                self.assertEqual(self.run_type(**args), "skip", case)
        self.startfile.assert_not_called()
        self.assertEqual((self.sent, self.raised), ([], []))

    def test_types_nothing_unless_focus_is_the_empty_input_of_the_limited_session(self):
        for ui in (None, (5, "Terminal input", "", True), (5, "Message input", "draft", True),
                   (5, "Message input", "\0", True), (5, "Message input", "", False)):  # 마지막: 한도 안내가 보이지 않는 다른 세션
            self.ui, self.sent = [ui], []
            self.assertEqual(self.run_type(), "skip", ui)
            self.assertEqual(self.sent, [])
        self.ui = [(5, "Message input", "", True)]
        with mock.patch.dict(self.ns, process_exe=lambda pid: r"C:\Other\app.exe"):
            self.assertEqual(self.run_type(), "skip")
        ticks = iter([100, 101])  # 확인하는 사이 사용자가 돌아왔다
        self.ui = [(5, "Message input", "", True)]
        with mock.patch.dict(self.ns, last_input_tick=lambda: next(ticks)):
            self.assertEqual(self.run_type(), "skip")
        self.assertEqual(self.sent, [])

    def test_does_not_press_enter_when_the_text_did_not_land(self):
        for after in (None, (5, "Message input", "continu", False), (5, "Search", "continue", False)):
            self.ui, self.sent = [(5, "Message input", "", True), after], []
            self.assertEqual(self.run_type(), "typed", after)
            self.assertEqual(len(self.sent), 1)  # 글자만 들어갔고 Enter 는 보내지 않았다

    def test_does_not_press_enter_when_another_window_came_forward(self):
        def focused_ui(marker=None):
            if marker is None:
                self.front[0] = (2, EXE)
                return (5, "Message input", "continue", False)
            return (5, "Message input", "", True)
        with mock.patch.dict(self.ns, focused_ui=focused_ui):
            self.assertEqual(self.run_type(), "typed")
        self.assertEqual(len(self.sent), 1)

    def test_failure_after_typing_is_never_retried(self):
        def broken(marker=None):
            if marker is None:
                raise OSError("uia failed")
            return (5, "Message input", "", True)
        with mock.patch.dict(self.ns, focused_ui=broken):
            self.assertEqual(self.run_type(), "typed")  # "skip" 이면 루프가 다시 시도한다
        self.assertEqual(len(self.sent), 1)

    def test_ctrl_enter_setting(self):
        with mock.patch.dict(self.ns, ctrl_enter_to_send=lambda exe: True):
            self.assertEqual(self.run_type(), "sent")
        self.assertEqual([e[0] for e in self.sent[1]], [0x11, 0x0D, 0x0D, 0x11])


@WINDOWS_ONLY
class HelperTest(unittest.TestCase):
    def test_limit_marker(self):
        marker = load({"limit_marker"}, re=re)["limit_marker"]
        self.assertEqual(marker(LIMIT), "resets3:45pm")
        self.assertEqual(marker("Limit reached · Resets 11PM"), "resets11pm")
        for text in ("", None, "limit reached", "resets tomorrow", "resets Oct 14"):
            self.assertIsNone(marker(text))

    def test_window_is_matched_by_folder_name(self):
        wins = [(1, 7, "a.py - proj - Visual Studio Code"), (2, 7, "proj2 - Visual Studio Code"),
                (3, 7, "x - my proj - Visual Studio Code"), (4, 7, "solo - Visual Studio Code"),
                (6, 7, "proj - elsewhere - Visual Studio Code"), (7, 7, "a - b - proj - Profile - Visual Studio Code")]  # 파일 이름·프로필 칸은 보지 않는다
        window = load({"panel_window"}, Path=Path, editor_windows=lambda exe: wins)["panel_window"]
        self.assertEqual(window(EXE, r"C:\work\proj"), (1, 7))
        self.assertEqual(window(EXE, r"C:\work\solo"), (4, 7))   # 파일을 열지 않은 창
        self.assertEqual(window(EXE, r"C:\work\proj2"), (2, 7))
        for cwd in (r"C:\work\none", "", None):
            self.assertIsNone(window(EXE, cwd))
        wins.append((5, 7, "b.py - proj - Visual Studio Code"))  # 같은 이름의 폴더를 두 창에서 열었다
        self.assertIsNone(window(EXE, r"C:\work\proj"))
        wins[-1] = (5, 7, "b.py - many (Workspace) - Visual Studio Code")  # 폴더를 알 수 없는 창이 하나라도 있으면 그만둔다
        self.assertIsNone(window(EXE, r"C:\work\solo"))

    def test_focus_reader_refuses_odd_markers(self):
        run = mock.Mock()
        focused = load({"focused_ui"}, re=re, subprocess=SimpleNamespace(run=run, TimeoutExpired=TimeoutError), os=SimpleNamespace(environ={}),
                       POWERSHELL="powershell.exe", UIA_FOCUS_SCRIPT="script", PANEL_FRAME_ID="active-frame")["focused_ui"]
        for bad in ("", "a", "resets 3pm", "$(calc)", "x" * 30, "RESETS3PM"):
            self.assertIsNone(focused(bad))
        run.assert_not_called()
        run.return_value = SimpleNamespace(stdout="12\tMessage input\t1\thello\tworld")
        self.assertEqual(focused("resets3:45pm"), (12, "Message input", "hello\tworld", True))
        env = run.call_args[1]["env"]
        self.assertEqual((env["QT_MARKER"], env["QT_FRAME"]), ("resets3:45pm", "active-frame"))
        self.assertEqual(run.call_args[0][0], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "script"])
        run.return_value = SimpleNamespace(stdout="broken")
        self.assertIsNone(focused())


class AuthorizedResumeTest(unittest.TestCase):
    def setUp(self):
        self.fetch = mock.Mock(return_value=dict(u5=3, uw=10))
        self.panel_type = mock.Mock(return_value="sent")
        self.sessions = [[(1, "sid", CWD, LIMIT, 10)]]
        self.ns = load({"authorized_panel_resume"}, PANEL_ENTRY=("claude-vscode",), panel_type=self.panel_type,
                       limited_claude_sessions=lambda entry=("cli",): self.sessions[0], provider_call=self.fetch,
                       resume_at=lambda *a: 1, panel_precheck=lambda pid, cwd: (EXE, 1, 77),
                       time=SimpleNamespace(time=lambda: 10 ** 9, sleep=lambda _: self.sessions.__setitem__(0, [])),
                       load_config=lambda: dict(auto_resume=True, panel_resume=True, resume_text="continue"))

    def test_disabled_option_or_other_text_never_types(self):
        for cfg, text in ((dict(auto_resume=True, panel_resume=False, resume_text="continue"), "continue"),
                          (dict(auto_resume=False, panel_resume=True, resume_text="continue"), "continue"),
                          (dict(auto_resume=True, panel_resume=True, resume_text="continue"), "rm -rf")):
            self.ns["load_config"] = lambda cfg=cfg: cfg
            self.assertEqual(self.ns["authorized_panel_resume"](1, text), "skip")
        self.panel_type.assert_not_called()

    def test_local_checks_run_before_asking_the_server(self):
        self.ns["panel_precheck"] = lambda pid, cwd: None
        self.assertEqual(self.ns["authorized_panel_resume"](1, "continue"), "skip")
        self.fetch.assert_not_called()

    def test_still_limited_or_another_session_never_types(self):
        for d in (dict(u5=100, uw=10), dict(u5=3, uw=100)):
            self.fetch.return_value = d
            self.assertEqual(self.ns["authorized_panel_resume"](1, "continue"), "skip")
        self.assertEqual(self.ns["authorized_panel_resume"](2, "continue"), "skip")  # 한도에 걸린 세션이 아니다
        self.panel_type.assert_not_called()

    def test_reports_sent_or_unverified(self):
        self.assertEqual(self.ns["authorized_panel_resume"](1, "continue"), "sent")  # 기록에서 리밋 에러가 사라졌다
        self.panel_type.assert_called_once_with(1, "sid", "continue", CWD, LIMIT)
        self.sessions[0] = [(1, "sid", CWD, LIMIT, 10)]
        self.panel_type.return_value = "typed"
        self.assertEqual(self.ns["authorized_panel_resume"](1, "continue"), "unverified")


class ResumeLoopTest(unittest.TestCase):
    limited, reset_at = {}, 100

    def run_loop(self, results, away):
        ticks = [1000]

        def sleep(_):
            ticks[0] += 60
            if ticks[0] > 1500:
                raise StopLoop()
        panel = mock.Mock(side_effect=results)
        ns = load({"Core", "WebWidget"}, time=SimpleNamespace(time=lambda: ticks[0], sleep=sleep), Path=Path, MAC=False,
                  PANEL_ENTRY=("claude-vscode",), user_away=lambda: away(ticks[0]), authorized_panel_resume=panel,
                  resume_at=lambda *a: self.reset_at, type_into_console=mock.Mock(), PANEL_MAX_AGE=6 * 3600,
                  limited_claude_sessions=lambda entry=("cli",): [(1, "sid", "C:/work/proj", "limit", 10)]
                  if entry == ("claude-vscode",) else [])
        app = SimpleNamespace(lock=threading.Lock(), data={}, tray_notify=mock.Mock(), limited=self.limited,
                              cfg=dict(auto_resume=True, panel_resume=True, resume_text="continue"))
        with self.assertRaises(StopLoop):
            ns["WebWidget"].resume_loop(app)
        ns["type_into_console"].assert_not_called()
        return panel, app.tray_notify

    def test_waits_while_the_user_is_present_and_retries_only_untouched_failures(self):
        panel, notify = self.run_loop(["skip", "sent"], away=lambda t: t >= 1180)
        self.assertEqual(panel.call_count, 2)  # 자리에 있는 동안은 부르지 않고, 입력 전 실패만 1분 뒤 다시
        notify.assert_called_once_with("리밋이 풀려 Claude 를 이어 갔어요: proj (VS Code)")

    def test_never_retries_after_keys_were_typed(self):
        panel, notify = self.run_loop(["unverified", "sent"], away=lambda t: True)
        self.assertEqual(panel.call_count, 1)
        self.assertIn("확인하지 못했어요", notify.call_args[0][0])

    def test_stops_after_four_untouched_failures(self):
        panel, notify = self.run_loop(["skip"] * 9, away=lambda t: True)
        self.assertEqual(panel.call_count, 4)
        notify.assert_called_once()
        self.assertIn("실패", notify.call_args[0][0])

    def test_waits_while_the_server_is_rate_limiting(self):
        self.limited = {"claude": 2}
        panel, notify = self.run_loop(["sent"], away=lambda t: True)
        panel.assert_not_called()  # 확인 조회를 보내지 않고, 시도 횟수도 쓰지 않는다
        notify.assert_not_called()

    def test_leaves_long_expired_limits_alone(self):
        self.reset_at = 1000 - 7 * 3600  # 7시간 전에 풀린 한도
        panel, _ = self.run_loop(["sent"], away=lambda t: True)
        panel.assert_not_called()

    def test_panel_option_off_never_calls(self):
        panel = mock.Mock()
        ticks = [1000]

        def sleep(_):
            ticks[0] += 60
            if ticks[0] > 1300:
                raise StopLoop()
        ns = load({"Core", "WebWidget"}, time=SimpleNamespace(time=lambda: ticks[0], sleep=sleep), Path=Path, MAC=False,
                  PANEL_ENTRY=("claude-vscode",), user_away=lambda: True, authorized_panel_resume=panel,
                  resume_at=lambda *a: 100, type_into_console=mock.Mock(), PANEL_MAX_AGE=6 * 3600,
                  limited_claude_sessions=lambda entry=("cli",): [(1, "sid", "C:/work/proj", "limit", 10)] if entry != ("cli",) else [])
        app = SimpleNamespace(lock=threading.Lock(), data={}, tray_notify=mock.Mock(), limited={},
                              cfg=dict(auto_resume=True, panel_resume=False, resume_text="continue"))
        with self.assertRaises(StopLoop):
            ns["WebWidget"].resume_loop(app)
        panel.assert_not_called()


@WINDOWS_ONLY
class CtrlEnterSettingTest(unittest.TestCase):
    def test_reads_editor_setting(self):
        with tempfile.TemporaryDirectory() as home:
            settings = Path(home) / "Code" / "User" / "settings.json"
            settings.parent.mkdir(parents=True)
            ns = load({"ctrl_enter_to_send"}, re=re, Path=Path, HOME=Path(home), EDITORS={"code.exe": ("vscode", "Code")},
                      os=SimpleNamespace(environ={"APPDATA": home}))
            self.assertFalse(ns["ctrl_enter_to_send"](EXE))  # 설정 파일 없음
            settings.write_text('{\n  // 주석\n  "claudeCode.useCtrlEnterToSend": true,\n}', encoding="utf-8")
            self.assertTrue(ns["ctrl_enter_to_send"](EXE))
            settings.write_text('{"claudeCode.useCtrlEnterToSend": false}', encoding="utf-8")
            self.assertFalse(ns["ctrl_enter_to_send"](EXE))


if __name__ == "__main__":
    unittest.main()
