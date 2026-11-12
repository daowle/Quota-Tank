"""폰에서 보기: Tailscale 주소에만 뜨는 읽기 전용 서버. 실제 Tailscale·계정·설정 폴더를 쓰지 않는다."""
import ast
import base64
import hashlib
import json
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hardening  # noqa: E402
import qr  # noqa: E402
from hardening import (REMOTE_PORT, REMOTE_TOKEN, config_values, in_tailnet, new_remote_token,  # noqa: E402
                       remote_app, remote_values, serve_remote)

TOKEN = "abcdefghjkmn"
ORIGIN = f"100.64.0.1:{REMOTE_PORT}"


class RemoteValuesTest(unittest.TestCase):
    def test_only_tailnet_addresses(self):
        for ip in ("100.64.0.1", "100.101.102.103", "100.127.255.254"):
            self.assertTrue(in_tailnet(ip))
        for ip in ("127.0.0.1", "0.0.0.0", "192.168.0.10", "100.63.255.255", "100.128.0.1", "", None, "fd7a:115c:a1e0::1"):
            self.assertFalse(in_tailnet(ip))
        for host in ("127.0.0.1", "0.0.0.0", "192.168.0.10"):  # 다른 주소에는 서버를 띄우지 않는다
            with self.assertRaises(ValueError):
                serve_remote(lambda env, start: [], host, 0)

    def test_token_format(self):
        for _ in range(50):
            self.assertRegex(new_remote_token(), REMOTE_TOKEN)
        self.assertNotEqual(new_remote_token(), new_remote_token())
        defaults = dict(remote_view=False, remote_token="")
        self.assertEqual(config_values(defaults, {"remote_token": TOKEN, "remote_view": True}),
                         dict(remote_view=True, remote_token=TOKEN, accounts=[]))
        for bad in ("short", TOKEN + "x", "ABCDEFGHJKMN", "abcdefghjk/.", "abcdefghjkm0", 5, None, [TOKEN]):
            self.assertEqual(config_values(defaults, {"remote_token": bad})["remote_token"], "")
        self.assertFalse(config_values(defaults, {"remote_view": "yes"})["remote_view"])

    def test_values_keep_numbers_and_names_only(self):
        svc = {"claude": dict(u5=62.4, uw=31, r5=2000000000, rw=2000300000, src="live", observed_at=1999990000,
                              plan="Max", hist=[[1999980000, 50], [1999990000, 62.4], ["x", 1], [1, float("nan")], [1, 2, 3]],
                              starts=[1], extra=[{"name": "Opus", "u": 5}], credit={"text": "secret"},
                              share={"h5": {"m": 12.3}}, email="me@example.com"),
               "account_x": dict(u5=150, uw=-3, r5=None, rw="soon", src="weird", observed_at=None, hist="bad")}
        out = remote_values([("claude", "Claude", "claude"), ("account_x", "n" * 200, "gpt"), ("gemini", "Gemini", "gemini"),
                             ("account_y", "Y", "evil")],
                            svc, {"gemini": "e" * 500, "account_x": {"not": "text"}}, 180)
        self.assertEqual(out["stale_after"], 180)
        a, b, c, d = out["cards"]
        self.assertEqual(a, dict(name="Claude", kind="claude", u5=62.4, uw=31, src="live", r5=2000000000, rw=2000300000,
                                 observed_at=1999990000, hist=[[1999980000, 50], [1999990000, 62.4]]))
        self.assertEqual(b, dict(name="n" * 80, kind="gpt", u5=100, uw=0, src="log", r5=None, rw=None, observed_at=None, hist=[]))
        self.assertEqual(c, dict(name="Gemini", kind="gemini", err="e" * 120))
        self.assertEqual(d, dict(name="Y", kind=""))
        text = json.dumps(out)
        for secret in ("example.com", "secret", "Max", "Opus", "12.3"):
            self.assertNotIn(secret, text)
        self.assertEqual(remote_values([], {}, {}, float("inf")), {"cards": [], "stale_after": 180, "day_hour": 6})
        for bad in (24, -1, 6.5, "6", True):
            self.assertEqual(remote_values([], {}, {}, 180, bad)["day_hour"], 6)
        self.assertEqual(remote_values([], {}, {}, 180, 0)["day_hour"], 0)

    def test_window_flags_pass_only_as_booleans(self):
        # 5시간 창이 없는 계정(Claude Max)은 폰에서도 '5시간 0%' 대신 주간만 보이게 has5 를 넘긴다
        base = dict(u5=0, uw=75, r5=None, rw=2000300000, src="live", observed_at=1999990000)
        out = remote_values([("claude", "Claude", "claude"), ("x", "X", "gpt")],
                            {"claude": dict(base, has5=False, hasw=True), "x": dict(base, has5="no", hasw=1)}, {}, 180)
        a, b = out["cards"]
        self.assertEqual((a["has5"], a["hasw"]), (False, True))
        self.assertNotIn("has5", b)
        self.assertNotIn("hasw", b)
        # 오늘 쓸 몫의 출발점(day0)은 0~100 숫자만
        out = remote_values([("a", "A", "claude"), ("b", "B", "gpt"), ("c", "C", "gpt")],
                            {"a": dict(base, day0=70), "b": dict(base, day0="70"), "c": dict(base, day0=float("nan"))}, {}, 180)
        self.assertEqual([c.get("day0") for c in out["cards"]], [70, None, None])
        self.assertEqual(len(remote_values([(str(i), "n", "gpt") for i in range(50)], {}, {}, 180)["cards"]), 23)


class RemoteAppTest(unittest.TestCase):
    def setUp(self):
        self.state = mock.Mock(return_value={"cards": [{"name": "Claude", "u5": 12}], "stale_after": 180})
        self.app = remote_app(ROOT / "web", ORIGIN, TOKEN, self.state)

    def request(self, path=f"/{TOKEN}/", **kwargs):
        result = {}
        env = dict(HTTP_HOST=ORIGIN, PATH_INFO=path, REQUEST_METHOD="GET")
        env.update(kwargs)
        body = b"".join(self.app(env, lambda status, headers: result.update(status=status, headers=dict(headers))))
        return result, body

    def test_page_needs_the_token(self):
        result, body = self.request()
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")
        self.assertIn("폰에서 보기", body.decode())
        policy = result["headers"]["Content-Security-Policy"]
        for part in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'", "form-action 'none'"):
            self.assertIn(part, policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertNotIn("'unsafe-inline'", re.search(r"script-src ([^;]*)", policy)[1])
        scripts = re.findall(r"<script>([\s\S]*?)</script>", body.decode())
        self.assertTrue(scripts)
        for script in scripts:
            self.assertIn("'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'", policy)
        for path in ("/", "", "/abcdefghjkmm/", "/" + TOKEN.upper() + "/", "/state.json", "/common.js", "//" + TOKEN + "/",
                     "/x/" + TOKEN + "/", "/" + TOKEN + "x/", "/\udcff/"):
            result, body = self.request(path)
            self.assertEqual((result["status"], body), ("404 Not Found", b"Not Found"), path)
        self.state.assert_not_called()

    def test_redirects_address_without_trailing_slash(self):
        result, body = self.request("/" + TOKEN)
        self.assertEqual((result["status"], result["headers"]["Location"], body), ("308 Permanent Redirect", f"/{TOKEN}/", b""))

    def test_only_listed_files(self):
        for name in ("common.js", "common.css", "icon.ico"):
            result, body = self.request(f"/{TOKEN}/{name}")
            self.assertEqual((result["status"], body), ("200 OK", (ROOT / "web" / name).read_bytes()))
        for name in ("main.html", "insights.html", "remote.html", "reset.wav", "fonts/PretendardVariable.woff2", "../widget.py",
                     "..%2fwidget.py", "config.json", "auth.json", ".credentials.json", "js_api/state", "state.json/", "state.json?x"):
            self.assertEqual(self.request(f"/{TOKEN}/{name}")[0]["status"], "404 Not Found", name)

    def test_rejects_other_hosts_methods_and_cross_site_reads(self):
        for env in (dict(HTTP_HOST="attacker.invalid:47113"), dict(HTTP_HOST="100.64.0.1"), dict(HTTP_HOST="127.0.0.1:47113"),
                    dict(HTTP_HOST="100.64.0.1:1"), dict(REQUEST_METHOD="POST"), dict(REQUEST_METHOD="PUT"), dict(REQUEST_METHOD="OPTIONS")):
            for path in (f"/{TOKEN}/", f"/{TOKEN}/state.json"):
                self.assertEqual(self.request(path, **env)[0]["status"], "404 Not Found", env)
        # 보조 장치: 브라우저는 HTTPS 가 아닌 주소에 이 헤더를 붙이지 않을 수 있다. 막는 것은 열쇠와 CORS 없음이다
        for site in ("cross-site", "same-site", "none"):
            self.assertEqual(self.request(f"/{TOKEN}/state.json", HTTP_SEC_FETCH_SITE=site)[0]["status"], "404 Not Found")
        self.state.assert_not_called()

    def test_state_json(self):
        for env in ({}, dict(HTTP_SEC_FETCH_SITE="same-origin")):
            result, body = self.request(f"/{TOKEN}/state.json", **env)
            self.assertEqual(result["status"], "200 OK")
            self.assertEqual(result["headers"]["Content-Type"], "application/json; charset=utf-8")
            self.assertEqual(result["headers"]["Cache-Control"], "no-store")
            self.assertEqual(json.loads(body), self.state.return_value)
        self.assertEqual(self.request(f"/{TOKEN}/state.json", REQUEST_METHOD="HEAD")[1], b"")
        self.state.side_effect = OSError(r"C:\Users\me\secret path")
        result, body = self.request(f"/{TOKEN}/state.json")
        self.assertEqual((result["status"], body), ("500 Internal Server Error", b"Error"))

    def test_bad_token_is_refused(self):
        for bad in ("", "short", "../../etc", TOKEN + "/"):
            with self.assertRaises(ValueError):
                remote_app(ROOT / "web", ORIGIN, bad, self.state)

    def test_serves_over_http(self):
        holder = {}
        server = hardening._serve(lambda env, start: holder["app"](env, start), "127.0.0.1", 0, timeout=15)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"127.0.0.1:{server.server_port}"
        holder["app"] = remote_app(ROOT / "web", origin, TOKEN, self.state)
        with urllib.request.urlopen(f"http://{origin}/{TOKEN}", timeout=5) as r:  # 308 을 따라가 화면을 받는다
            self.assertEqual((r.status, r.url), (200, f"http://{origin}/{TOKEN}/"))
        with urllib.request.urlopen(f"http://{origin}/{TOKEN}/state.json", timeout=5) as r:
            self.assertEqual(json.load(r), self.state.return_value)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://{origin}/abcdefghjkmm/state.json", timeout=5)
        self.assertEqual(e.exception.code, 404)


class RemoteWidgetTest(unittest.TestCase):
    """widget.py 의 WebWidget·Api 를 가짜 서버·가짜 Tailscale 주소로 돌린다."""

    def setUp(self):
        source = ast.parse((ROOT / "widget.py").read_text(encoding="utf-8"))
        self.saved, self.servers = [], []
        self.ip = None

        def serve(app, host, port):
            if self.ip == "busy":
                raise OSError("in use")
            self.servers.append(mock.Mock(app=app, host=host, port=port))
            return self.servers[-1]
        self.ns = dict(threading=threading, time=__import__("time"), qr=qr, save_config=lambda cfg: self.saved.append(dict(cfg)), WEB_DIR=ROOT / "web",
                       REMOTE_PORT=REMOTE_PORT, REMOTE_TOKEN=REMOTE_TOKEN, new_remote_token=lambda: TOKEN,
                       remote_app=remote_app, remote_values=remote_values, serve_remote=serve,
                       tailnet_ip=lambda: "100.64.0.9" if self.ip == "busy" else self.ip,
                       on_main=lambda fn, *args: fn(*args),
                       provider_meta=SimpleNamespace(ALL=[SimpleNamespace(KEY="claude", NAME="Claude"), SimpleNamespace(KEY="gpt", NAME="Codex")]))
        names = {"Core", "WebWidget", "Api"}
        exec(compile(ast.Module(body=[n for n in source.body if getattr(n, "name", "") in names], type_ignores=[]),
                     "widget.py", "exec"), self.ns)
        w = self.ns["WebWidget"]
        self.app = SimpleNamespace(cfg=dict(remote_view=False, remote_token=""), remote=None, remote_msg="",
                                   remote_lock=threading.Lock(), tray=mock.Mock())
        self.app.sync_remote = lambda: w.sync_remote(self.app)
        self.app.remote_state = lambda: w.remote_state(self.app)
        self.api = self.ns["Api"](self.app)

    def test_off_by_default_and_waits_for_tailscale(self):
        self.assertEqual(self.app.sync_remote(), dict(on=False, url="", qr=[], msg=""))
        self.assertEqual((self.servers, self.saved), ([], []))
        out = self.api.toggle_remote_view()
        self.assertEqual((out["on"], out["url"]), (True, ""))
        self.assertIn("Tailscale", out["msg"])
        self.assertEqual(self.servers, [])  # Tailscale 이 없으면 어디에도 서버를 띄우지 않는다
        self.assertTrue(self.saved[-1]["remote_view"])
        self.app.tray.update_menu.assert_called()

    def test_binds_tailnet_address_and_follows_changes(self):
        self.ip = "100.64.0.1"
        out = self.api.toggle_remote_view()
        url = f"http://100.64.0.1:{REMOTE_PORT}/{TOKEN}/"
        self.assertEqual(out, dict(on=True, url=url, qr=qr.rows(url), msg=""))
        self.assertEqual((len(out["qr"]), set("".join(out["qr"]))), (29, {"0", "1"}))
        self.assertEqual((self.servers[0].host, self.servers[0].port), ("100.64.0.1", REMOTE_PORT))
        self.assertEqual(self.saved[-1]["remote_token"], TOKEN)
        self.app.sync_remote()
        self.assertEqual(len(self.servers), 1)  # 그대로면 다시 띄우지 않는다
        self.ip = "100.64.0.2"  # 주소가 바뀌면 옛 서버를 내리고 새 주소에 띄운다. 토큰은 그대로다
        self.assertEqual(self.app.sync_remote()["url"], f"http://100.64.0.2:{REMOTE_PORT}/{TOKEN}/")
        self.servers[0].shutdown.assert_called_once()
        self.servers[0].server_close.assert_called_once()
        self.ip = None  # Tailscale 이 끊기면 내린다
        self.assertEqual(self.app.sync_remote()["url"], "")
        self.servers[1].shutdown.assert_called_once()
        self.assertIsNone(self.app.remote)
        self.ip = "100.64.0.2"
        self.app.sync_remote()
        self.assertEqual(self.api.toggle_remote_view(), dict(on=False, url="", qr=[], msg=""))  # 끄면 내린다
        self.servers[2].shutdown.assert_called_once()
        self.assertFalse(self.saved[-1]["remote_view"])

    def test_busy_port_shows_a_message(self):
        self.ip = "busy"
        out = self.api.toggle_remote_view()
        self.assertEqual((out["on"], out["url"]), (True, ""))
        self.assertIn(str(REMOTE_PORT), out["msg"])
        self.assertIsNone(self.app.remote)

    def test_state_leaves_out_email_and_settings(self):
        self.app.state = lambda: dict(
            svc={"claude": dict(u5=10, uw=20, r5=None, rw=None, src="live", observed_at=5, hist=[], plan="Max")},
            err={"account_a": "조회 실패"}, order=["account_a", "claude", "gone"], names={"claude": "회사"},
            accounts=[dict(id="account_a", name="개인", provider="gpt", email="me@example.com")],
            cfg=dict(stale_after=240, day_reset_hour=5, remote_url="http://100.64.0.1:47113/" + TOKEN + "/"))
        out = self.app.remote_state()
        self.assertEqual(out, dict(stale_after=240, day_hour=5, cards=[
            dict(name="개인", kind="gpt", err="조회 실패"),
            dict(name="회사", kind="claude", u5=10, uw=20, src="live", r5=None, rw=None, observed_at=5, hist=[])]))
        self.assertNotIn(TOKEN, json.dumps(out))
        self.app.state = mock.Mock(side_effect=AssertionError("5초 안에는 다시 만들지 않는다"))
        self.assertIs(self.app.remote_state(), out)

    def test_email_used_as_a_name_is_not_sent(self):
        # 이름을 비우고 연결한 계정은 이메일이 이름으로 저장된다
        self.app.state = lambda: dict(
            svc={}, err={}, order=["claude", "account_a", "account_b"], names={"claude": "work@example.com"},
            accounts=[dict(id="account_a", name="me@example.com", provider="gpt", email="me@example.com"),
                      dict(id="account_b", name="other@example.com", provider="claude", email="")],
            cfg=dict(stale_after=180))
        out = self.app.remote_state()
        self.assertEqual([c["name"] for c in out["cards"]], ["Claude", "Codex", "Claude"])
        self.assertNotIn("@", json.dumps(out))

    def test_connection_limit(self):
        import socket
        server = hardening._serve(lambda env, start: [start("200 OK", [("Content-Length", "0")]) and b"" or b""],
                                  "127.0.0.1", 0, timeout=15, max_conn=2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        held = [socket.create_connection(("127.0.0.1", server.server_port), timeout=5) for _ in range(2)]
        for s in held:
            self.addCleanup(s.close)
        extra = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        self.addCleanup(extra.close)
        try:
            self.assertEqual(extra.recv(1), b"")  # 꽉 차면 바로 닫는다
        except ConnectionError:
            pass
        for s in held:
            s.close()
        for _ in range(50):  # 자리가 나면 다시 받는다
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as r:
                    self.assertEqual(r.status, 200)
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                threading.Event().wait(.1)
        else:
            self.fail("연결 자리가 돌아오지 않았다")


if __name__ == "__main__":
    unittest.main()
