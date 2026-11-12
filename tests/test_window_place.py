import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def beside():
    tree = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
    keep = [n for n in tree.body if getattr(n, "name", "") == "beside"]
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "widget.py", "exec"), ns)
    return ns["beside"]


class InsightsPlacementTest(unittest.TestCase):
    """인사이트 창은 위젯 옆, 작업 영역 안에 뜬다."""
    WORK = (0, 0, 1920, 1040)

    def setUp(self):
        self.beside = beside()

    def test_widget_on_right_opens_to_its_left(self):
        self.assertEqual(self.beside((1600, 80, 1900, 600), self.WORK, 300, 560, 8), (1292, 80))

    def test_widget_on_left_opens_to_its_right(self):
        self.assertEqual(self.beside((20, 100, 320, 600), self.WORK, 300, 560, 8), (328, 100))

    def test_falls_back_to_other_side_and_stays_on_screen(self):
        # 양쪽 다 자리가 없을 만큼 넓은 창이면 작업 영역 안으로 민다
        self.assertEqual(self.beside((1000, 0, 1500, 300), self.WORK, 1100, 400, 8)[0], 0)
        self.assertEqual(self.beside((300, 0, 600, 300), self.WORK, 1400, 400, 8)[0], 520)
        # 위젯이 아래쪽에 붙어 있으면 창이 화면 밖으로 내려가지 않게 위로 올린다
        self.assertEqual(self.beside((1600, 900, 1900, 1000), self.WORK, 300, 560, 8), (1292, 480))

    def test_second_monitor_work_area(self):
        work = (1920, 0, 3840, 1040)
        self.assertEqual(self.beside((3500, 50, 3800, 400), work, 300, 560, 8), (3192, 50))
        self.assertEqual(self.beside((1930, 50, 2230, 400), work, 300, 560, 8), (2238, 50))


if __name__ == "__main__":
    unittest.main()
