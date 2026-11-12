"""로컬 페이지 서버: 호스트 이름 조회(getfqdn) 없이 뜨고, page_app 의 차단 규칙을 그대로 쓴다."""
import socket
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hardening import page_app, serve_local  # noqa: E402


class LocalServerTest(unittest.TestCase):
    def test_starts_without_hostname_lookup_and_serves_page(self):
        # 맥에서 getfqdn 이 끝나지 않으면 http.server 가 뜨지 않았다. 불리면 실패하게 막는다
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("getfqdn called")):
            url = serve_local(page_app(ROOT / "web", "main.html"))
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/$")
        with urllib.request.urlopen(url, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("Content-Security-Policy", r.headers)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url + "widget.py", timeout=5)
        self.assertEqual(e.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
