import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parts(*names):
    tree = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
    keep = [n for n in tree.body if getattr(n, "name", "") in names
            or isinstance(n, ast.Assign) and any(getattr(t, "id", "") in names for t in n.targets)]
    ns = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "widget.py", "exec"), ns)
    return ns


class UpdateCheckTest(unittest.TestCase):
    """새 버전 알림: GitHub 최신 릴리스의 태그 번호만 보고, 지금보다 클 때만 알린다."""

    def setUp(self):
        self.ns = parts("VERSION", "parse_version", "newer_version")

    def test_parse_version(self):
        p = self.ns["parse_version"]
        self.assertEqual(p("v1.2.3"), (1, 2, 3))
        self.assertEqual(p("0.10.0"), (0, 10, 0))
        for bad in ("v1.2", "1.2.3-beta", "<b>1.2.3</b>", None, 123, "v99999.0.0"):
            self.assertIsNone(p(bad))

    def test_only_newer_release_is_reported(self):
        newer = self.ns["newer_version"]
        self.assertEqual(newer({"tag_name": "v0.2.1"}, "0.2.0"), "0.2.1")
        self.assertEqual(newer({"tag_name": "v0.10.0"}, "0.9.9"), "0.10.0")  # 글자가 아니라 숫자로 비교한다
        self.assertIsNone(newer({"tag_name": "v0.2.0"}, "0.2.0"))
        self.assertIsNone(newer({"tag_name": "v0.1.9"}, "0.2.0"))
        self.assertIsNone(newer({"tag_name": "nightly"}, "0.2.0"))
        self.assertIsNone(newer(["v9.9.9"], "0.2.0"))
        self.assertIsNone(newer({"tag_name": "v1.0.0"}, "dev"))

    def test_version_matches_tag_format(self):
        self.assertIsNotNone(self.ns["parse_version"](self.ns["VERSION"]))

    def test_release_page_is_fixed(self):
        src = (ROOT / "widget.py").read_text(encoding="utf-8")
        self.assertIn('RELEASES_URL = "https://github.com/daowle/Quota-Tank/releases/latest"', src)
        self.assertIn("webbrowser.open(RELEASES_URL)", src)


if __name__ == "__main__":
    unittest.main()
