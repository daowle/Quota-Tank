"""창 자리 잡기(윈도우): 더블클릭한 자리에 띄우기. 실제 창을 쓰지 않고 좌표 계산만 mock 으로 돌린다."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
WORK = [0, 0, 1920, 1040]  # 작업 영역


class PlaceAtCursorTest(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.rect = [1700, 300, 1836, 369]  # 지금 창
        self.moves, self.saved = [], mock.Mock()

        def set_win_rect(hwnd, x, y, w, h):
            self.moves.append((x, y, w, h))
            self.rect = [x, y, x + w, y + h]
        self.mouse = None  # 눌린 채 있는 마우스 자리(끄는 중)
        ns = dict(time=SimpleNamespace(time=lambda: self.now[0]), DETAIL_CLICK_Y=44, save_config=self.saved,
                  find_hwnd=lambda title: 1, set_win_rect=set_win_rect, MAC=False,
                  threading=SimpleNamespace(Timer=lambda *a: mock.Mock()),
                  mouse_down=lambda: self.mouse is not None, cursor_monitor=lambda: (*self.mouse, list(WORK), 1.0),
                  win_geometry=lambda hwnd: (list(self.rect), list(WORK), 1.0))
        exec(compile(ast.Module(body=[n for n in SOURCE.body if getattr(n, "name", "") in {"Core", "WebWidget"}],
                                type_ignores=[]), "widget.py", "exec"), ns)
        self.app = SimpleNamespace(cfg=dict(compact=False, anchor=["east", 1836, 300]), hwnd=1, _last_fit=None,
                                   _center_at=None, _placed=None, _save_timer=None)
        self.place = lambda w, h: ns["WebWidget"].place(self.app, w, h)
        self.moved = lambda: ns["WebWidget"].on_moved(self.app, self.rect[0], self.rect[1])

    def drag_to(self, x, y):
        """사용자가 창을 (x, y) 로 끌었다: 창이 옮겨지고 이동 알림이 온다."""
        w, h = self.rect[2] - self.rect[0], self.rect[3] - self.rect[1]
        self.rect = [x, y, x + w, y + h]
        self.mouse = (x + 10, y + 10)
        self.moved()

    def click(self, x, y, compact):
        self.app.cfg["compact"] = compact
        self.app._center_at = (x, y, list(WORK), 1.0, self.now[0])

    def test_detail_view_lands_under_the_cursor_even_when_sizes_arrive_in_steps(self):
        self.click(1000, 500, compact=False)
        for w, h in ((200, 150), (300, 260), (320, 400)):  # 보기가 바뀌는 동안 오는 중간 크기와 마지막 크기
            self.now[0] += 0.05
            self.place(w, h)
        x, y, w, h = self.moves[-1]
        self.assertEqual((x + w // 2, y + 44, w, h), (1000, 500, 320, 400))  # 가로 가운데, 위에서 44px 이 클릭 자리
        self.assertEqual(self.app.cfg["anchor"], ["east", x + w, y])
        self.now[0] += 5  # 그 뒤의 크기 변화는 새 기준점(오른쪽 위)을 지킨다
        self.place(320, 460)
        self.assertEqual(self.moves[-1], (x, y, 320, 460))

    def test_compact_view_is_centered_on_the_cursor(self):
        self.click(400, 600, compact=True)
        for w, h in ((320, 300), (136, 69)):
            self.now[0] += 0.05
            self.place(w, h)
        x, y, w, h = self.moves[-1]
        self.assertEqual((x + w // 2, y + h // 2), (400, 600))
        self.assertEqual(self.app.cfg["anchor"], ["west", x, y])

    def test_same_spot_twice_does_not_drift(self):
        spots = []
        for compact, size in ((True, (136, 69)), (False, (320, 400)), (True, (136, 69)), (False, (320, 400))):
            self.now[0] += 10
            self.click(1000, 500, compact)
            self.place(*size)
            spots.append(self.moves[-1])
        self.assertEqual(spots[0], spots[2])
        self.assertEqual(spots[1], spots[3])

    def test_stays_inside_the_work_area(self):
        self.click(1915, 1035, compact=False)
        self.place(320, 400)
        x, y, w, h = self.moves[-1]
        self.assertEqual((x + w, y + h), (1920, 1040))
        self.click(2, 3, compact=True)
        self.place(136, 69)
        self.assertEqual(self.moves[-1][:2], (0, 0))

    def test_drag_right_after_a_resize_is_not_undone(self):
        self.place(136, 69)          # 크기가 막 바뀌어 위젯이 창을 놓았다
        self.moved()                 # 그 이동 알림은 기준점을 바꾸지 않는다
        self.assertEqual(self.app.cfg["anchor"], ["east", 1836, 300])
        self.drag_to(600, 200)       # 곧바로 끌었다
        self.assertEqual(self.app.cfg["anchor"], ["west", 600, 200])
        self.mouse = None
        self.now[0] += 0.2
        self.place(140, 69)          # 그 뒤 크기가 바뀌어도 끌어 둔 자리에 남는다
        self.assertEqual(self.rect[:2], [600, 200])

    def test_size_change_while_dragging_keeps_the_window_under_the_mouse(self):
        self.drag_to(1500, 400)
        self.place(180, 90)          # 끄는 중에 크기가 바뀌었다
        self.assertEqual(self.rect, [1500, 400, 1680, 490])  # 왼쪽 위는 그대로, 크기만
        self.assertEqual(self.app.cfg["anchor"], ["east", 1680, 400])
        self.drag_to(1400, 420)
        self.assertEqual(self.rect[:2], [1400, 420])

    def test_drag_after_double_click_cancels_the_cursor_spot(self):
        self.click(1000, 500, compact=True)
        self.place(136, 69)
        self.moved()
        self.drag_to(300, 300)       # 더블클릭 직후 끌었다
        self.mouse = None
        self.now[0] += 0.3
        self.place(137, 69)          # 1.5초 안의 크기 요청이 더블클릭한 자리로 되돌리지 않는다
        self.assertEqual(self.rect[:2], [300, 300])

    def test_mouse_pressed_elsewhere_is_not_a_drag(self):
        self.mouse = (50, 900)       # 다른 프로그램을 누르고 있다
        self.place(320, 400)
        self.assertEqual(self.rect, [1516, 300, 1836, 700])  # 평소처럼 오른쪽 위 기준

    def test_no_move_or_save_when_nothing_changes(self):
        self.click(1000, 500, compact=False)
        self.place(320, 400)
        moves, saves = len(self.moves), self.saved.call_count
        self.now[0] += 0.1
        self.place(320, 400)  # 같은 크기 요청이 또 와도 창을 건드리지 않는다
        self.assertEqual((len(self.moves), self.saved.call_count), (moves, saves))


@unittest.skipUnless(__import__("sys").platform == "win32", "윈도우 전용")
class SharedUser32Test(unittest.TestCase):
    def test_widget_never_sets_argument_types_on_the_shared_user32(self):
        # 2026-10-11: keep_on_top 이 ctypes.windll.user32.SetWindowPos 에 인자 형식을 걸어, 앞 창이 한 번 바뀐 뒤로는
        # pywebview 의 창 옮기기(SetWindowPos 에 None 을 넘긴다)가 형식 오류로 실패해 드래그가 완전히 멈췄다
        import ctypes
        from ctypes import wintypes
        shared = ctypes.windll.user32
        before = {name: getattr(shared, name).argtypes for name in ("SetWindowPos", "GetWindowLongPtrW", "ShowWindow")}
        ns = dict(ctypes=ctypes, wintypes=wintypes, os=__import__("os"), sunk_below=lambda *a: False,
                  find_hwnd=lambda title: None)
        names = {"keep_on_top", "hide_from_taskbar"}
        exec(compile(ast.Module(body=[n for n in SOURCE.body if getattr(n, "name", "") in names], type_ignores=[]),
                     "widget.py", "exec"), ns)
        self.assertFalse(ns["keep_on_top"](0))  # 없는 창: 아무것도 하지 않는다
        ns["hide_from_taskbar"]("QuotaTank test window that does not exist")
        for name, value in before.items():
            self.assertEqual(getattr(shared, name).argtypes, value, name)
        shared.SetWindowPos(0, None, 10, 10, None, None, 0x0001 | 0x0004 | 0x0010)  # pywebview 와 같은 모양의 호출이 여전히 된다

    def test_no_argtypes_on_windll_in_the_source(self):
        text = (ROOT / "widget.py").read_text(encoding="utf-8")
        for fn in (n for n in ast.walk(SOURCE) if isinstance(n, ast.FunctionDef)):
            body = ast.get_source_segment(text, fn) or ""
            if ".argtypes" in body:
                self.assertNotRegex(body, r"=\s*ctypes\.windll\.user32", fn.name)  # 주석이 아닌 대입만 본다


if __name__ == "__main__":
    unittest.main()
