"""Input boundaries shared by the desktop app and its offline web UI."""
import base64
import copy
import hashlib
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

_SAVE_LOCK = threading.Lock()
ASSETS = ("main.html", "insights.html", "remote.html", "common.js", "common.css", "icon.ico",
          "reset.wav", "fonts/PretendardVariable.woff2", "fonts/LICENSE.txt")


def bridge_message_allowed(raw, source, expected_url, functions):
    """Validate the native message before pywebview's generic object dispatch."""
    try:
        if len(raw) > 65536 or urlsplit(source)._replace(fragment="") != urlsplit(expected_url)._replace(fragment=""):
            return False
        message = json.loads(raw)
        if not isinstance(message, list) or len(message) != 3:
            return False
        name, encoded, call_id = message
        if not isinstance(name, str) or not isinstance(encoded, str):
            return False
        params = json.loads(encoded)
        if name == "pywebviewMoveWindow":
            return isinstance(params, list) and len(params) == 2 and all(finite(p, -100000, 100000) for p in params)
        return (name in functions and isinstance(params, list) and len(params) <= 8
                and isinstance(call_id, str) and re.fullmatch(r"[0-9]{1,32}", call_id) is not None)
    except (ValueError, TypeError, RecursionError, AttributeError):
        return False


def protect_window(win):
    """Windows: keep the native bridge attached only to the app's own document."""
    def attach():
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionState
        browser = win.native.webview
        expected = urlsplit(win.real_url)

        def message(sender, args):
            if bridge_message_allowed(str(args.get_WebMessageAsJson()), str(args.Source), win.real_url, win._functions):
                win.native.browser.on_script_notify(sender, args)

        browser.WebMessageReceived -= win.native.browser.on_script_notify
        browser.WebMessageReceived += message

        def navigation(sender, args):
            target = urlsplit(str(args.Uri))
            if (target.scheme, target.netloc, target.path, target.query) != (
                    expected.scheme, expected.netloc, expected.path, expected.query):
                args.Cancel = True

        def new_window(sender, args):
            args.Handled = True

        def permission(sender, args):
            args.State = CoreWebView2PermissionState.Deny

        def ready(sender, args):
            if not args.IsSuccess:
                return
            core = browser.CoreWebView2
            core.NewWindowRequested -= win.native.browser.on_new_window_request
            core.NewWindowRequested += new_window
            core.PermissionRequested += permission
            core.Settings.AreDevToolsEnabled = False
            core.Settings.AreDefaultContextMenusEnabled = False

        browser.NavigationStarting += navigation
        browser.CoreWebView2InitializationCompleted += ready
        if browser.CoreWebView2 is not None:
            ready(browser, type("Ready", (), {"IsSuccess": True})())
    win.events.before_show += attach


APP_NAME = "QuotaTank"
LEGACY_APP_NAME = "AIUsageWidget"  # 이름을 바꾸기 전 설정·기록 폴더
REMOTE_PORT = 47113  # 폰에서 보기: 폰에 저장한 주소가 바뀌지 않게 고정한다
REMOTE_TOKEN = re.compile(r"[a-km-np-z2-9]{12}")  # 주소에 들어가는 열쇠. 폰에서 직접 칠 수 있게 헷갈리는 글자(l·o·0·1)를 뺐다
REMOTE_STATIC = ("common.js", "common.css", "icon.ico")  # 폰에서 보기 화면이 쓰는 파일


def app_dirs(home=None):
    """(새 이름 폴더, 예전 이름 폴더)."""
    home = Path(home or Path.home())
    root = home / "Library" / "Application Support" if sys.platform == "darwin" else Path(os.environ.get("APPDATA", home))
    return root / APP_NAME, root / LEGACY_APP_NAME


def app_dir(home=None):
    """설정·기록 폴더. 위젯과 자격 증명 브로커가 같은 config.json 을 본다.
    예전 이름 폴더만 있으면(옮기기에 실패했으면) 그 폴더를 그대로 쓴다."""
    new, old = app_dirs(home)
    return old if not new.exists() and old.is_dir() else new


def migrate_app_dir(home=None):
    """예전 이름 폴더를 새 이름으로 한 번 옮긴다. 예전 버전이 켜져 있어 실패하면 다음 실행에 다시 한다."""
    new, old = app_dirs(home)
    if not new.exists() and old.is_dir():
        try:
            old.rename(new)
        except OSError:
            pass


def atomic_json(path, data):
    """An interrupted save leaves the previous complete file in place."""
    with _SAVE_LOCK:
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def finite(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def config_values(defaults, raw):
    cfg = copy.deepcopy(defaults)
    if not isinstance(raw, dict):
        return cfg
    for key, value in raw.items():
        if key in cfg and type(cfg[key]) is bool and type(value) is bool:
            cfg[key] = value
    for key, low, high in (("poll_sec", 30, 3600), ("day_reset_hour", 0, 23),
                           ("reset_alert_min", 0, 100), ("alpha", .2, 1),
                           ("x", -100000, 100000), ("y", -100000, 100000),
                           ("wx", -100000, 100000), ("wy", -100000, 100000)):
        value = raw.get(key)
        if finite(value, low, high) and (key != "day_reset_hour" or value == int(value)):
            cfg[key] = int(value) if key == "day_reset_hour" else value
    if raw.get("theme") in ("auto", "light", "dark"):
        cfg["theme"] = raw["theme"]
    if raw.get("share_plan") in ("auto", "pro", "max5", "max20"):
        cfg["share_plan"] = raw["share_plan"]
    if "credit_day" in cfg and type(raw.get("credit_day")) is int and 0 <= raw["credit_day"] <= 31:
        cfg["credit_day"] = raw["credit_day"]  # API 크레딧 결제일(0 = 끔)
    if "credit_amount" in cfg and raw.get("credit_amount") in (100, 200) and type(raw["credit_amount"]) is int:
        cfg["credit_amount"] = raw["credit_amount"]
    if "cycle_times" in cfg:
        cfg["cycle_times"] = cycle_times(raw.get("cycle_times"))
    token = raw.get("remote_token")
    if isinstance(token, str) and REMOTE_TOKEN.fullmatch(token):
        cfg["remote_token"] = token
    text = raw.get("resume_text")
    if isinstance(text, str) and 0 < len(text) <= 200 and all(ord(c) >= 32 for c in text):
        cfg["resume_text"] = text
    anchor = raw.get("anchor")
    if isinstance(anchor, list) and len(anchor) == 3 and anchor[0] in ("east", "west") and all(finite(v, -100000, 100000) for v in anchor[1:]):
        cfg["anchor"] = anchor
    hidden = raw.get("compact_hidden")
    if isinstance(hidden, list):
        cfg["compact_hidden"] = [s for s in hidden if isinstance(s, str) and len(s) <= 80][:20]
    order = raw.get("order")
    if isinstance(order, list):  # 카드 순서: 서비스 키와 계정 ID 만, 중복 없이
        keys = [s for s in order if isinstance(s, str) and re.fullmatch(r"claude|gpt|gemini|account_[a-zA-Z0-9_]{1,64}", s)]
        cfg["order"] = list(dict.fromkeys(keys))[:23]
    names = raw.get("names")
    if isinstance(names, dict):  # 기본 서비스 카드에 붙인 이름
        cfg["names"] = {k: v for k, v in names.items() if k in ("claude", "gpt", "gemini") and isinstance(v, str)
                        and 0 < len(v) <= 40 and all(ord(c) >= 32 and ord(c) != 127 for c in v)}
    prices = raw.get("prices")
    if isinstance(prices, dict):
        cfg["prices"] = {k: v for k, v in list(prices.items())[:100] if isinstance(k, str)
                         and len(k) <= 100 and isinstance(v, list) and len(v) == 5
                         and all(finite(n, 0, 100000) for n in v)}
    accounts = raw.get("accounts")
    cfg["accounts"] = []
    if isinstance(accounts, list):
        seen = set()
        for a in accounts[:20]:
            if not isinstance(a, dict) or a.get("provider") not in ("claude", "gpt"):
                continue
            if not all(isinstance(a.get(k), str) for k in ("id", "name", "path")):
                continue
            if not re.fullmatch(r"account_[a-zA-Z0-9_]{1,64}", a["id"]) or a["id"] in seen:
                continue
            if len(a["path"]) > 4096 or not Path(a["path"]).is_absolute():
                continue
            seen.add(a["id"])
            cfg["accounts"].append({"id": a["id"], "provider": a["provider"], "path": a["path"],
                                    "name": a["name"][:80], "email": str(a.get("email", ""))[:254]})
    return cfg


def valid_history(r, now):
    return (isinstance(r, dict) and isinstance(r.get("s"), str) and len(r["s"]) <= 80
            and finite(r.get("t"), now - 35 * 86400, now + 300)
            and all(finite(r.get(k), 0, 100) for k in ("u5", "uw"))
            and all(r.get(k) is None or finite(r[k], 0, 32503680000) for k in ("r5", "rw")))


def percent(value):
    """A finite number clamped to 0..100 (APIs can report 100.5 or -0.1); anything else is None."""
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return min(100, max(0, value))


def usage_values(raw):
    """Only bounded, typed display fields cross from providers into the UI."""
    if not isinstance(raw, dict) or any(percent(raw.get(k)) is None for k in ("u5", "uw")):
        raise ValueError("Invalid usage percentages")
    if not all(raw.get(k) is None or finite(raw[k], 0, 32503680000) for k in ("r5", "rw")):
        raise ValueError("Invalid reset timestamp")
    out = {k: raw.get(k) for k in ("r5", "rw")}
    out.update(u5=percent(raw["u5"]), uw=percent(raw["uw"]))
    out.update(plan=str(raw.get("plan") or "")[:80], src="live" if raw.get("src") == "live" else "log")
    for key in ("has5", "hasw"):
        if type(raw.get(key)) is bool:
            out[key] = raw[key]
    if "observed_at" in raw:
        out["observed_at"] = raw["observed_at"] if finite(raw["observed_at"], 0, 32503680000) else None
    extra = raw.get("extra")
    out["extra"] = []
    for x in extra[:50] if isinstance(extra, list) else []:
        if isinstance(x, dict) and isinstance(x.get("name"), str) and percent(x.get("u")) is not None and (x.get("r") is None or finite(x["r"], 0, 32503680000)):
            out["extra"].append(dict(name=x["name"][:160], u=percent(x["u"]), r=x.get("r")))
    c = raw.get("credit")
    if isinstance(c, dict) and isinstance(c.get("text"), str) and (c.get("used_pct") is None or percent(c["used_pct"]) is not None) and (c.get("r") is None or finite(c["r"], 0, 32503680000)):
        out["credit"] = dict(text=c["text"][:200], used_pct=None if c.get("used_pct") is None else percent(c["used_pct"]), r=c.get("r"))
    return out


def cycle_times(raw):
    """사이클 예약 시각: 목록이나 "8:00, 13:30" 같은 글에서 올바른 시각만 골라 "HH:MM" 으로 맞추고, 겹치는 것을 빼 이른 순서로 4개까지."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw[:200])
    out = set()
    for item in raw[:40] if isinstance(raw, list) else []:
        m = isinstance(item, str) and re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", item.strip())
        if m:
            out.add(f"{int(m[1]):02d}:{m[2]}")
    return sorted(out)[:4]


def new_remote_token():
    return "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(12))


def in_tailnet(ip):
    """Tailscale 이 기기에 주는 IPv4 대역(100.64.0.0/10)인지."""
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except (TypeError, ValueError):
        return False


def tailnet_ip():
    """이 PC 의 Tailscale IPv4. 연결돼 있지 않으면 None.
    Tailscale 안내 주소(100.100.100.100)로 나가는 길의 출발 주소를 묻는다. UDP connect 는 패킷을 보내지 않고 호스트 이름 조회도 없다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 9))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ip if in_tailnet(ip) else None


def remote_values(cards, svc, err, stale_after, day_hour=6):
    """폰에서 보기로 내보내는 값: 카드 이름·서비스 종류·사용률·리셋 시각·이번 5시간 창의 기록·오류 문구만.
    이메일·로그인 파일 경로·설정·비용은 넣지 않는다. cards: [(키, 이름, 서비스)]"""
    out = []
    for key, name, kind in cards[:23]:
        d = svc.get(key)
        card = {"name": str(name)[:80], "kind": kind if kind in ("claude", "gpt", "gemini") else ""}
        if isinstance(err.get(key), str):
            card["err"] = err[key][:120]
        if isinstance(d, dict) and percent(d.get("u5")) is not None and percent(d.get("uw")) is not None:
            card.update(u5=percent(d["u5"]), uw=percent(d["uw"]), src="live" if d.get("src") == "live" else "log")
            for k in ("r5", "rw", "observed_at"):
                card[k] = d[k] if finite(d.get(k), 0, 32503680000) else None
            for k in ("has5", "hasw"):  # 5시간·주간 창이 없는 계정(Claude Max·Codex 주간 전용)
                if type(d.get(k)) is bool:
                    card[k] = d[k]
            if percent(d.get("day0")) is not None:  # 오늘 쓸 몫의 출발점(하루 기준 시각의 주간 사용률)
                card["day0"] = percent(d["day0"])
            hist = d.get("hist")
            card["hist"] = [[p[0], percent(p[1])] for p in (hist[-400:] if isinstance(hist, list) else [])
                            if isinstance(p, (list, tuple)) and len(p) == 2
                            and finite(p[0], 0, 32503680000) and percent(p[1]) is not None]
        out.append(card)
    return {"cards": out, "stale_after": stale_after if finite(stale_after, 0, 86400) else 180,
            "day_hour": day_hour if type(day_hour) is int and 0 <= day_hour <= 23 else 6}


def remote_app(root, origin, token, state):
    """폰에서 보기: 읽기 전용 WSGI. 주소의 첫 칸이 토큰과 같아야 화면(remote.html)·정적 파일 셋·사용량 JSON 을 준다.
    브리지·설정·파일 경로는 없다. state() 가 돌려준 값을 그대로 JSON 으로 내보내므로 부르는 쪽이 remote_values 를 거친다.
    origin: 서버를 띄운 "Tailscale 주소:포트". 다른 Host 로 온 요청(DNS 리바인딩)은 거부한다."""
    if not REMOTE_TOKEN.fullmatch(token):
        raise ValueError("Invalid token")
    html = (root / "remote.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
              for s in re.findall(r"<script>([\s\S]*?)</script>", html)]
    policy = ("default-src 'none'; script-src 'self' " + " ".join(hashes)
              + "; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; "
              "object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    static = {name: (root / name).read_bytes() for name in REMOTE_STATIC}
    page = html.encode("utf-8")

    def app(env, start_response):
        status, mime, body, extra = "404 Not Found", "text/plain", b"Not Found", []
        parts = env.get("PATH_INFO", "/").split("/", 2)
        if (env.get("HTTP_HOST") == origin and env.get("REQUEST_METHOD") in ("GET", "HEAD") and len(parts) > 1
                and hmac.compare_digest(parts[1].encode("utf-8", "replace"), token.encode())):
            rest = parts[2] if len(parts) == 3 else None
            if rest is None:  # 끝의 / 를 빼고 친 주소: 상대 경로가 맞게 붙여 준다
                status, body, extra = "308 Permanent Redirect", b"", [("Location", f"/{token}/")]
            elif rest == "":
                status, mime, body = "200 OK", "text/html; charset=utf-8", page
            elif rest == "state.json" and env.get("HTTP_SEC_FETCH_SITE", "same-origin") == "same-origin":
                try:
                    body = json.dumps(state(), ensure_ascii=False, allow_nan=False).encode("utf-8")
                    status, mime = "200 OK", "application/json; charset=utf-8"
                except Exception:
                    status, body = "500 Internal Server Error", b"Error"
            elif rest in static:
                status, mime, body = "200 OK", mimetypes.guess_type(rest)[0] or "application/octet-stream", static[rest]
                extra = [("Cache-Control", "private, max-age=3600")]
        start_response(status, [
            ("Content-Type", mime), ("Content-Length", str(len(body))),
            ("Content-Security-Policy", policy), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
            *(extra or [("Cache-Control", "no-store")])])
        return [b"" if env.get("REQUEST_METHOD") == "HEAD" else body]
    return app


def page_app(root, page, allow_eval=False):
    """Read-only WSGI: exact public assets only, no filesystem or Python API routes.
    allow_eval: macOS only. pywebview returns every bridge call's value through window.evaluate_js, which wraps
    the code in eval(); WKWebView applies the page's CSP to that eval, so without it no api.* promise resolves.
    WebView2 runs ExecuteScriptAsync outside the page's eval restriction, so Windows keeps eval blocked."""
    if page not in ("main.html", "insights.html"):
        raise ValueError("Unknown page")
    assets = {}
    for name in ASSETS:
        assets["/" + name] = (root / name).read_bytes()
    html = assets["/" + page].decode("utf-8").replace("\r\n", "\n")
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
              for s in re.findall(r"<script>([\s\S]*?)</script>", html)]
    policy = ("default-src 'none'; script-src 'self' " + ("'unsafe-eval' " if allow_eval else "") + " ".join(hashes)
              + "; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; "
              "connect-src 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
              "base-uri 'none'; form-action 'none'")
    assets["/"] = html.encode("utf-8")

    def app(env, start_response):
        host = env.get("HTTP_HOST", "")
        path = env.get("PATH_INFO", "/")
        allowed = (re.fullmatch(r"(127\.0\.0\.1|localhost):[0-9]{1,5}", host)
                   and env.get("REQUEST_METHOD") in ("GET", "HEAD")
                   and env.get("HTTP_SEC_FETCH_SITE", "none") in ("none", "same-origin")
                   and path in assets and (path == "/" or not path.endswith(".html")))
        body = assets[path] if allowed else b"Forbidden"
        mime = "text/html; charset=utf-8" if path == "/" else mimetypes.guess_type(path)[0] or "application/octet-stream"
        start_response("200 OK" if allowed else "403 Forbidden", [
            ("Content-Type", mime if allowed else "text/plain"), ("Content-Length", str(len(body))),
            ("Content-Security-Policy", policy), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store"),
            ("Permissions-Policy", "camera=(), microphone=(), geolocation=()")])
        return [b"" if env.get("REQUEST_METHOD") == "HEAD" else body]
    return app


def _serve(app, host, port, timeout=None, max_conn=None):
    import socketserver
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
    slots = threading.BoundedSemaphore(max_conn) if max_conn else None

    class Server(socketserver.ThreadingMixIn, WSGIServer):
        daemon_threads = True
        allow_reuse_address = sys.platform != "win32"  # 윈도우의 SO_REUSEADDR 는 남이 쓰는 포트에도 겹쳐 붙는다

        def process_request(self, request, client_address):
            if slots and not slots.acquire(blocking=False):  # 동시 연결이 꽉 찼으면 스레드를 만들지 않고 닫는다
                return self.shutdown_request(request)
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                if slots:
                    slots.release()

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)  # HTTPServer.server_bind 의 getfqdn 을 건너뛴다
            self.server_name, self.server_port = host, self.server_address[1]
            self.setup_environ()

    class Handler(WSGIRequestHandler):
        def log_message(self, *args):  # 요청마다 stderr 에 찍지 않는다
            pass
    Handler.timeout = timeout

    server = Server((host, port), Handler)
    server.set_app(app)
    threading.Thread(target=server.serve_forever, name="page-server", daemon=True).start()
    return server


def serve_local(app):
    """WSGI 앱을 127.0.0.1 의 빈 포트에서 띄우고 주소를 돌려준다(위젯이 끝나면 같이 끝나는 데몬 스레드).
    pywebview 내장 서버 대신 쓴다: http.server 는 시작할 때 socket.getfqdn 으로 호스트 이름을 찾는데,
    맥에서 DNS 설정에 따라 이 조회가 끝나지 않아 서버가 뜨지 않고 화면이 빈 채로 남는다."""
    return f"http://127.0.0.1:{_serve(app, '127.0.0.1', 0).server_port}/"


def serve_remote(app, host, port):
    """폰에서 보기 서버를 Tailscale 주소 하나에만 띄운다(0.0.0.0 이 아니다). 끌 때는 돌려준 서버의 shutdown·server_close.
    포트를 다른 프로그램이 쓰고 있으면 OSError. 15초 동안 아무것도 보내지 않는 연결은 끊고, 동시 연결은 16개까지만 받는다."""
    if not in_tailnet(host):
        raise ValueError("Not a tailnet address")
    return _serve(app, host, port, timeout=15, max_conn=16)
