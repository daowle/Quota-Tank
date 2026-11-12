"""QuotaTank — Claude / ChatGPT(Codex) / Gemini 5시간·주간 한도를 바탕화면에 띄워 둔다.

데이터 소스
  Claude : ~/.claude/.credentials.json 의 OAuth 토큰 → api.anthropic.com/api/oauth/usage
  GPT    : ~/.codex/auth.json 의 토큰 → chatgpt.com/backend-api/wham/usage
           (실패 시 ~/.codex/sessions/**.jsonl 의 마지막 rate_limits 이벤트)
  Gemini : 윈도우 자격증명/맥 키체인(gemini:antigravity) → cloudcode-pa.googleapis.com
  토큰은 자격 증명 브로커 프로세스(provider_worker.py)만 읽는다. 이 프로세스는 사용량 숫자만 받는다.
기록     : %APPDATA%/QuotaTank/history.jsonl  (예측·인사이트용)
           맥: ~/Library/Application Support/QuotaTank/
"""
import sys

if __name__ == "__main__" and sys.argv[1:2] == ["--provider-worker"]:
    # 자격 증명 브로커(provider_worker.py): 화면 코드(tkinter·pywebview·위젯 상태)는 불러오지 않는다
    from provider_worker import main
    main()

import calendar
import ctypes
import json
import os
import re
import shutil
import socket
import tempfile
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from hardening import app_dir, app_dirs, migrate_app_dir, atomic_json, config_values, valid_history, page_app, finite, protect_window, usage_values, serve_local
from hardening import cycle_times, REMOTE_PORT, REMOTE_TOKEN, new_remote_token, remote_app, remote_values, serve_remote, tailnet_ip
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    import tkinter as tk
except ImportError:  # 맥 Homebrew 파이썬은 tkinter 가 따로 설치된다(brew install python-tk). Tk 화면에서만 쓴다.
    tk = None

HOME = Path.home()
MAC = sys.platform == "darwin"
FROZEN = getattr(sys, "frozen", False)  # PyInstaller 로 묶은 실행파일
# web 폴더·알림음 위치. 묶은 실행파일은 풀어 놓은 임시 폴더(_MEIPASS)에 있다.
APP_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if MAC:
    STARTUP_FILE = HOME / "Library" / "LaunchAgents" / "com.quotatank.plist"
    LEGACY_STARTUP_FILE = STARTUP_FILE.with_name("com.aiusagewidget.plist")
else:
    STARTUP_FILE = Path(os.environ.get("APPDATA", HOME)) / r"Microsoft\Windows\Start Menu\Programs\Startup\QuotaTank.vbs"
    LEGACY_STARTUP_FILE = STARTUP_FILE.with_name("AIUsageWidget.vbs")


def other_instance_running():
    """새 이름이나 예전 이름(AIUsageWidget)의 위젯이 이미 떠 있는지. 모르면 떠 있다고 본다."""
    try:
        if MAC:
            import fcntl
            for folder in app_dirs(HOME):
                try:
                    with open(folder / "widget.lock") as f:  # 없으면 만들지 않는다
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except FileNotFoundError:
                    continue
                except OSError:  # 다른 프로세스가 잠금을 쥐고 있다
                    return True
            return False
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenMutexW.restype = ctypes.c_void_p
        for name in (r"Local\QuotaTank", r"Local\AIUsageWidget"):
            handle = k32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
            if handle:
                k32.CloseHandle(ctypes.c_void_p(handle))
                return True
            if ctypes.get_last_error() == 5:  # ERROR_ACCESS_DENIED: 관리자 권한으로 떠 있다
                return True
        return False
    except (AttributeError, OSError):
        return True


# 예전 이름 설정 폴더는 사용자가 위젯을 켤 때, 옛·새 위젯이 모두 꺼져 있을 때만 옮긴다.
# 테스트의 import·보조 프로세스(--type-into)는 폴더를 건드리지 않는다. 브로커는 위에서 이미 갈라져 나갔다.
if __name__ == "__main__" and sys.argv[1:2] != ["--type-into"] and not other_instance_running():
    migrate_app_dir(HOME)
APP_DIR = app_dir(HOME)
APP_DIR.mkdir(parents=True, exist_ok=True)


def msix_packaged():
    """MSIX 로 설치돼 실행 중인지(패키지 ID 가 있는지). 시작 시 실행 방식이 달라진다."""
    if MAC:
        return False
    try:
        size = ctypes.c_uint32(0)
        return ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(size), None) != 15700  # APPMODEL_ERROR_NO_PACKAGE
    except (AttributeError, OSError):
        return False


PACKAGED = msix_packaged()
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.jsonl"

FIVE_H = 5 * 3600
WEEK = 7 * 86400
DEFAULTS = {"x": None, "y": 80, "topmost": True, "alpha": 0.94, "poll_sec": 300,
            "day_reset_hour": 6, "compact": True, "gemini_enabled": False,
            "reset_alert": True, "reset_alert_min": 80, "theme": "auto",
            "auto_resume": False, "resume_text": "continue", "panel_resume": False,
            "menubar_text": True, "compact_hidden": [], "order": [], "names": {}, "status_check": True, "prices": {},
            "shared_split": False, "share_plan": "auto", "remote_view": False, "remote_token": "",
            "cycle_start": False, "cycle_times": [], "boot_start": False,
            "credit_day": 0, "credit_amount": 100, "onboarded": False, "update_check": True}  # onboarded: 처음 실행 안내를 마쳤는지. API 크레딧: 결제일(1~31, 0 = 끔), 한 달 크레딧(Max 100 · Ultra 200달러)
VERSION = "0.2.0"  # 릴리스 태그(v0.2.0)와 같아야 한다(release.yml 이 확인한다)
UPDATE_URL = "https://api.github.com/repos/daowle/Quota-Tank/releases/latest"  # 공개된 최신 릴리스(초안·시험판 제외)
RELEASES_URL = "https://github.com/daowle/Quota-Tank/releases/latest"  # 받는 곳. 서버가 준 주소는 쓰지 않는다
UPDATE_POLL = 86400  # 새 버전 확인은 하루 한 번
CREDIT_POLL = 1800  # API 크레딧 쓴 금액 조회 간격(초). 비용 보고서는 몇 분 늦게 쌓이고 크레딧은 천천히 줄어든다

# ── colors ──────────────────────────────────────────────────────────────
BG, CARD, FG, SUB, TRACK = "#15171c", "#1d2027", "#e8eaed", "#8b919c", "#2c303a"
GREEN, AMBER, RED, TICK = "#4ade80", "#fbbf24", "#f87171", "#e8eaed"
BRAND = {"claude": "#d97757", "gpt": "#10a37f", "gemini": "#4c8bf5"}
FONT = "Apple SD Gothic Neo" if MAC else "Malgun Gothic"


def load_config():
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if raw and "onboarded" not in raw:  # 이 값이 생기기 전부터 쓰던 사용자에게는 처음 실행 안내를 띄우지 않는다
        raw = {**raw, "onboarded": True}
    if raw.get("poll_sec") == 60:  # 예전 기본값(1분)이 그대로 저장된 설정은 새 기본값(5분)으로 올린다. 1분은 서버가 429 로 막았다
        raw = {**raw, "poll_sec": DEFAULTS["poll_sec"]}
    return config_values(DEFAULTS, raw)


def save_config(cfg):
    try:
        atomic_json(CONFIG_PATH, cfg)
        return True
    except Exception:
        return False


def iso_to_epoch(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


if not MAC:
    from ctypes import wintypes

# ── 서비스: 이름·장애 주소·계정 추가 방법(providers.meta)만 이 프로세스에서 쓴다 ──
# 로그인 토큰을 읽는 providers.<서비스> 모듈은 자격 증명 브로커(provider_worker.py) 프로세스에서만 불러온다.
import qr
import share
from providers import codex_log
from providers import meta as provider_meta
from providers.net import RateLimited, http_json

STATUS_URLS = {p.KEY: p.STATUS_URL for p in provider_meta.ALL if p.STATUS_URL}


class ProviderBroker:
    """자격 증명 브로커와의 연결. 브로커는 처음 요청할 때 띄워 두고 계속 쓴다(위젯이 끝나면 같이 끝난다).
    정해진 요청만 보내고 사용량 숫자·이메일·유효 여부만 받는다. 브로커가 죽으면 다음 요청 때 다시 띄운다."""
    OPS = ("fetch", "inspect", "profiles", "default_email", "active", "switch", "credit", "credit_key")
    MAX_REPLY = 65536
    TIMEOUT = 25

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.pending = {}  # 요청 id → (브로커 프로세스, Future)
        self.last_id = 0
        self.starts = []   # 최근 시작 시각 — 계속 죽는 브로커를 쉬지 않고 다시 띄우지 않게

    def _start(self):
        now = time.monotonic()
        self.starts = [t for t in self.starts if now - t < 60]
        if len(self.starts) >= 5:
            raise RuntimeError("계정 조회 프로세스를 시작하지 못했어요")
        self.starts.append(now)
        args = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
        self.proc = subprocess.Popen(args + ["--provider-worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        try:
            while True:
                line = proc.stdout.readline(self.MAX_REPLY + 1)
                if not line or len(line) > self.MAX_REPLY:
                    break
                reply = json.loads(line.decode("utf-8"))
                if (not isinstance(reply, dict) or type(reply.get("id")) is not int
                        or set(reply) not in ({"id", "ok"}, {"id", "error"}, {"id", "error", "retry_after"})
                        or "retry_after" in reply and (type(reply["retry_after"]) is not int or not 0 <= reply["retry_after"] <= 86400)):
                    break
                with self.lock:
                    owner, future = self.pending.pop(reply["id"], (None, None))
                if owner is proc:
                    future.set_result(reply)
        except (OSError, ValueError):
            pass
        try:
            proc.kill()  # 응답 규칙을 어겼거나 끝난 브로커
        except OSError:
            pass
        with self.lock:
            if self.proc is proc:
                self.proc = None
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass
            lost = [rid for rid, (owner, _) in self.pending.items() if owner is proc]
            futures = [self.pending.pop(rid)[1] for rid in lost]
        for future in futures:
            future.set_result({"error": "계정 조회 프로세스 오류"})

    def warm(self):
        """요청 없이 브로커만 미리 띄워 둔다(곧 보낼 첫 요청이 시작 시간을 기다리지 않게)."""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()

    def call(self, op, key, path=None, value=None):
        """value 는 API 크레딧 질문에만 쓴다(credit: 결제 주기 시작 epoch, credit_key: 관리자 키 | "" 지우기 | None 저장 여부)."""
        timeout = 120 if op == "switch" else self.TIMEOUT  # 전환은 Claude Code 잠금을 기다릴 수 있다
        if op not in self.OPS or key not in provider_meta.BY_KEY or not (path is None or isinstance(path, str)):
            raise ValueError("지원하지 않는 계정 요청")
        if value is not None and op not in ("credit", "credit_key"):
            raise ValueError("지원하지 않는 계정 요청")
        future = Future()
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            proc = self.proc
            self.last_id += 1
            rid = self.last_id
            self.pending[rid] = (proc, future)
            try:
                body = {"id": rid, "op": op, "provider": key, "path": path, **({"value": value} if op in ("credit", "credit_key") else {})}
                proc.stdin.write(json.dumps(body).encode("utf-8") + b"\n")
                proc.stdin.flush()
            except (OSError, ValueError):  # 방금 끝나 파이프가 닫힌 브로커
                self.pending.pop(rid, None)
                raise RuntimeError("계정 조회 프로세스 오류") from None
        try:
            reply = future.result(timeout)
        except FutureTimeout:
            with self.lock:
                self.pending.pop(rid, None)
            raise RuntimeError("계정 조회 시간 초과 · 네트워크를 확인하세요") from None
        if "retry_after" in reply:
            raise RateLimited(reply["retry_after"] or None)
        if "error" in reply:
            raise RuntimeError(str(reply["error"])[:120])
        return reply["ok"]


BROKER = ProviderBroker()
provider_call = BROKER.call
SERVICES = [(p.KEY, p.NAME, lambda key=p.KEY: provider_call("fetch", key)) for p in provider_meta.ALL]


def parse_version(tag):
    """"v1.2.3" · "1.2.3" → (1, 2, 3). 다른 모양이면 None."""
    m = re.fullmatch(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})", tag) if isinstance(tag, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def newer_version(reply, current=VERSION):
    """GitHub 최신 릴리스 응답에서 지금보다 새 버전이면 "1.2.3", 아니면 None. 응답의 다른 값(주소·본문)은 쓰지 않는다."""
    latest = parse_version(reply.get("tag_name")) if isinstance(reply, dict) else None
    mine = parse_version(current)
    return ".".join(map(str, latest)) if latest and mine and latest > mine else None


def fetch_update():
    """새 버전 확인: 버전 번호만 받는다. 사용자 정보는 보내지 않는다."""
    return newer_version(http_json(UPDATE_URL, {"User-Agent": f"QuotaTank/{VERSION}", "Accept": "application/vnd.github+json"}))


def fetch_status():
    """{서비스: {"level": none|minor|major|critical|maintenance, "text": 설명}}. 실패한 곳은 뺀다."""
    out = {}
    for key, url in STATUS_URLS.items():
        try:
            reply = http_json(url, {"User-Agent": "ai-usage-widget/1.0"})
            # 다른 제품(Console·Compliance API 등)의 장애를 구독 사용량 카드에 옮기지 않는다: 관련 기능만 본다.
            names = [str(c.get("name") or "") for c in reply["components"]]
            if key == "claude":
                wanted = {"claude.ai", "Claude Code"}
                found = wanted <= set(names)
            else:  # Codex: 이름에 Codex 가 든 기능과 로그인. Codex 기능이 목록에 없으면 판단하지 않는다
                wanted = {n for n in names if "codex" in n.lower()} | {"Login"}
                found = len(wanted) > 1
            relevant = [c for c in reply["components"] if c.get("name") in wanted]
            if not found:
                continue  # 관련 기능을 확인하지 못하면 전체 장애로 추측하지 않는다.
            levels = {"operational": "none", "degraded_performance": "minor",
                      "partial_outage": "major", "major_outage": "critical",
                      "under_maintenance": "maintenance"}
            rank = {"none": 0, "maintenance": 1, "minor": 2, "major": 3, "critical": 4}
            if any(c.get("status") not in levels for c in relevant):
                continue
            level = max((levels[c["status"]] for c in relevant), key=rank.get)
            affected = list(dict.fromkeys(c["name"] for c in relevant if c["status"] != "operational"))
            out[key] = {"level": level, "text": " · ".join(affected) + " 상태 이상" if affected else "정상"}
        except Exception:
            continue
    return out



# ── 계정 추가: 공식 로그인을 새 설정 폴더로 띄우고, 로그인 파일이 생기면 자동으로 연결한다 ──
LOGIN_TIMEOUT = 600  # 10분 안에 로그인을 마치지 않으면 취소
# 맥에서 .app·LaunchAgent 로 켜면 PATH 가 /usr/bin:/bin:/usr/sbin:/sbin 뿐이라 Homebrew·npm 으로 깐 CLI 를 못 찾는다
MAC_CLI_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".npm-global" / "bin"),
                str(HOME / ".local" / "bin"), str(HOME / ".bun" / "bin"), str(HOME / ".volta" / "bin"))


def find_cli(name):
    """CLI 실행 파일 경로. 맥은 터미널 셸이 보통 PATH 에 넣는 설치 위치도 찾아본다."""
    path = os.environ.get("PATH", "")
    if MAC:
        path = os.pathsep.join([path, *MAC_CLI_DIRS])
    return shutil.which(name, path=path)


def login_supported(p):
    """(가능 여부, 안 되는 이유). 맥의 Claude Code 는 로그인 정보를 파일이 아닌 키체인에 둬서 아직 못 읽는다."""
    if not getattr(p, "LOGIN", None):
        return False, "계정 추가를 지원하지 않아요"
    if MAC and p.KEY == "claude":
        return False, "맥에서는 아직 Claude 추가 계정을 지원하지 않아요"
    if not find_cli(p.LOGIN["cmd"][0]):
        return False, f"{p.LOGIN['cli']} 가 설치돼 있지 않아요"
    return True, ""


def card_order(cfg):
    """카드 순서: 저장한 순서(order)를 따르고, 거기 없는 서비스·계정은 뒤에 붙인다. 꺼 둔 Gemini 는 뺀다."""
    keys = [p.KEY for p in provider_meta.ALL if p.KEY != "gemini" or cfg.get("gemini_enabled", False)]
    keys += [a["id"] for a in cfg.get("accounts", [])]
    return list(dict.fromkeys([k for k in cfg.get("order", []) if k in keys] + keys))


def new_profile_dir(p, name):
    """~/.claude-회사 처럼 CLI 에서도 그대로 쓸 수 있는 표준 위치에 새 설정 폴더를 고른다."""
    base = ".claude" if p.KEY == "claude" else ".codex"
    slug = re.sub(r"[^0-9A-Za-z가-힣]+", "-", name).strip("-").lower()[:24] or uuid.uuid4().hex[:6]
    path, i = HOME / f"{base}-{slug}", 2
    while path.exists():
        path, i = HOME / f"{base}-{slug}-{i}", i + 1
    return path


def launch_login(p, folder):
    """공식 CLI 로그인을 띄운다. 설정 폴더 환경변수만 바꿔 기존 로그인과 섞이지 않게 한다.
    터미널 창 없이 브라우저만 열리고, 끝낼 수 있게 프로세스를 돌려준다."""
    info = p.LOGIN
    if MAC:
        import pty
        cli = [find_cli(info["cmd"][0]) or info["cmd"][0], *info["cmd"][1:]]
        # 터미널 앱 대신 가상 터미널(pty)을 준다: CLI 는 터미널에서처럼 브라우저를 열고 기다리지만 창은 뜨지 않는다.
        # npm 으로 깐 CLI 는 node 를 PATH 에서 찾으므로 CLI 가 있는 폴더와 흔한 설치 위치를 PATH 에 넣는다
        path = os.pathsep.join([os.path.dirname(cli[0]), *MAC_CLI_DIRS, os.environ.get("PATH", "")])
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(cli, env=dict(os.environ, PATH=path, **{info["env"]: str(folder)}),
                                    stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)

        def drain():  # 출력을 읽어 버리지 않으면 pty 버퍼가 차서 CLI 가 멈춘다
            try:
                while os.read(master, 4096):
                    pass
            except OSError:
                pass
            finally:
                os.close(master)
        threading.Thread(target=drain, daemon=True).start()
        return proc
    cli = [find_cli(info["cmd"][0]), *info["cmd"][1:]]
    if any(c in cli[0] for c in "&|<>^%!\r\n"):  # .cmd 로 깔린 CLI 는 cmd.exe 를 거쳐 실행된다
        raise RuntimeError("CLI 설치 경로에 셸 특수문자가 있어 실행할 수 없어요")
    # 콘솔은 만들되 숨긴다: CLI 는 터미널에서처럼 브라우저를 열고 기다리지만 화면에는 창이 뜨지 않는다
    hidden = subprocess.STARTUPINFO()
    hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    hidden.wShowWindow = 0  # SW_HIDE
    return subprocess.Popen(cli, env=dict(os.environ, **{info["env"]: str(folder)}),
                            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden)


def stop_login(proc):
    """숨겨 둔 로그인 프로세스를 끝낸다. 남겨 두면 보이지 않는 채로 로그인 포트를 잡고 있어 다음 로그인이 막힌다."""
    if not proc or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":  # .cmd 로 띄운 경우 실제 CLI 는 자식 프로세스라 트리째 끝낸다
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:  # 새 세션으로 띄웠으므로 CLI 가 만든 자식(node 등)까지 그룹째 끝낸다
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        pass


# ── history & analytics ─────────────────────────────────────────────────
class History:
    def __init__(self):
        self.lock = threading.RLock()
        self.rows = []
        self.last = {}
        cutoff = time.time() - 35 * 86400
        try:
            with HISTORY_PATH.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        if valid_history(r, time.time()):
                            self.rows.append(r)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            pass
        self.rows.sort(key=lambda r: r["t"])  # 가져온 지난 기록이 뒤에 붙어 있을 수 있다
        for r in self.rows:
            self.last[r["s"]] = r

    def merge(self, svc, pts):
        with self.lock:
            return self._merge(svc, pts)

    def _merge(self, svc, pts):
        """지난 기록 (t, u5, r5, uw, rw) 을 끼워 넣는다. 값이 그대로면 10분 간격으로만 남긴다."""
        have = {r["t"] for r in self.svc_rows(svc)}
        new, prev = [], None
        for t, u5, r5, uw, rw in sorted(pts):
            if not valid_history(dict(t=t, s=svc, u5=u5, r5=r5, uw=uw, rw=rw), time.time()):
                continue
            row = {"t": round(t), "s": svc, "u5": u5, "r5": wkey(r5), "uw": uw, "rw": wkey(rw)}
            if prev and all(prev[k] == row[k] for k in ("u5", "r5", "uw", "rw")) and row["t"] - prev["t"] < 600:
                continue
            prev = row
            if row["t"] not in have:
                new.append(row)
        if not new:
            return 0
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(r) + "\n" for r in new)
        self.rows = sorted(self.rows + new, key=lambda r: r["t"])  # 통째로 바꿔 끼워 다른 스레드가 안전하게 읽게
        return len(new)

    def add(self, svc, d):
        with self.lock:
            return self._add(svc, d)

    def _add(self, svc, d):
        now = time.time()
        row = {"t": round(now), "s": svc, "u5": d["u5"], "r5": wkey(d["r5"]), "uw": d["uw"], "rw": wkey(d["rw"])}
        if d.get("has5") is False:  # 5시간 창이 없는 계정: 켤 때 기록 값으로도 '5시간 한도 없음'을 보이게
            row["has5"] = False
        prev = self.last.get(svc)
        same = prev and all(prev.get(k) == row[k] for k in ("u5", "r5", "uw", "rw"))
        if same and now - prev["t"] < 600:
            return
        self.rows.append(row)
        self.last[svc] = row
        with HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def svc_rows(self, svc):
        with self.lock:
            return [r for r in self.rows if r["s"] == svc]


def wkey(ts):
    """resets_at 값은 호출마다 미세하게 흔들려서 분 단위로 맞춘다."""
    return None if ts is None else int(round(ts / 60) * 60)


def burn_rate(hist, svc, r5, now):
    """현재 5시간 창에서 최근 90분 소모 속도 (%/시간)."""
    k = wkey(r5)
    pts = [r for r in hist.svc_rows(svc) if r["r5"] == k and now - r["t"] <= 5400]
    if len(pts) < 2:
        return None
    a, b = pts[0], pts[-1]
    dt = (b["t"] - a["t"]) / 3600
    if dt < 5 / 60 or b["u5"] <= a["u5"]:
        return 0.0 if dt >= 5 / 60 else None
    return (b["u5"] - a["u5"]) / dt


def day_end(now_dt, hour):
    end = now_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now_dt >= end:
        end += timedelta(days=1)
    return end


def day_start_usage(rows, rw, day_start):
    """오늘 기준 시각(day_start)의 주간 사용률: 오늘 쓸 몫의 출발점.
    주간 창이 오늘 새로 시작됐으면 0, 기준 시각 전 기록이 없으면 오늘 첫 기록 값, 같은 주의 기록이 없으면 None."""
    if rw is None:
        return None
    if rw - WEEK >= day_start:
        return 0.0
    same = [r for r in rows if r.get("rw") is not None and abs(r["rw"] - rw) <= 120]  # resets_at 은 1분 안팎으로 흔들린다
    before = [r for r in same if r["t"] <= day_start]
    if before:
        return before[-1]["uw"]
    return same[0]["uw"] if same else None


def plan_cycles(now, r5, active, end_hour):
    """오늘(기준 종료 시각까지) 시작 가능한 5시간 사이클 시작 시각 목록."""
    end = day_end(datetime.fromtimestamp(now), end_hour).timestamp()
    t = r5 if active else now
    out = []
    while t < end and len(out) < 6:
        out.append(t)
        t += FIVE_H
    return out


def windows(hist, svc):
    """기록에서 5시간 창별 (시작 시각, 최고 사용률) 추출."""
    peak = {}
    for r in hist.svc_rows(svc):
        if r["r5"] and r["u5"] > 0:
            peak[r["r5"]] = max(peak.get(r["r5"], 0), r["u5"])
    out = []
    for k in sorted(peak):
        if out and k - (out[-1][0] + FIVE_H) < 600:  # resets_at 흔들림으로 갈라진 같은 창은 합친다
            out[-1] = (out[-1][0], max(out[-1][1], peak[k]))
        else:
            out.append((k - FIVE_H, peak[k]))
    return out


# ── formatting ──────────────────────────────────────────────────────────
def hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def dur(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}"


def dur_short(sec):
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f"{d}일 {h}시간"
    return f"{h}시간 {m}분" if h else f"{m}분"


WEEKDAY = "월화수목금토일"


def day_label(ts):
    d = datetime.fromtimestamp(ts)
    return f"{d.month}/{d.day}({WEEKDAY[d.weekday()]}) {d:%H:%M}"


def level_color(p):
    return GREEN if p < 60 else AMBER if p < 85 else RED


def startup_enabled():
    """시작 시 실행이 켜져 있는지. MSIX 설치본은 윈도우 설정에서 켜고 꺼서 여기서는 알 수 없다(None)."""
    return None if PACKAGED else STARTUP_FILE.exists()


def toggle_startup():
    if PACKAGED:
        # MSIX 앱이 시작 프로그램 폴더에 쓴 파일은 패키지 전용 위치로 옮겨져 동작하지 않는다.
        # 대신 매니페스트의 StartupTask 를 윈도우 설정 > 앱 > 시작 프로그램에서 켜고 끈다.
        os.startfile("ms-settings:startupapps")
        return None
    if STARTUP_FILE.exists():
        STARTUP_FILE.unlink()
        return False
    # 묶은 실행파일은 그 파일 하나만, 소스 실행은 파이썬 + widget.py 를 등록한다
    args = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
    if MAC:
        import plistlib
        STARTUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STARTUP_FILE.write_bytes(plistlib.dumps({
            "Label": "com.quotatank", "ProgramArguments": args,
            "RunAtLoad": True, "ProcessType": "Interactive"}))
        return True
    if not FROZEN:
        args[0] = str(Path(sys.executable).with_name("pythonw.exe"))
    cmd = " ".join(f'""{a}""' for a in args)
    STARTUP_FILE.write_text(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\r\n', encoding="utf-8")
    return True


def migrate_startup():
    """예전 이름으로 등록한 시작 시 실행을 지우고 새 이름으로 다시 등록한다."""
    if PACKAGED or not LEGACY_STARTUP_FILE.exists():
        return
    try:
        if not STARTUP_FILE.exists():
            toggle_startup()  # 새로 등록한 뒤에 지운다. 실패하면 예전 등록이 남고 다음 실행에 다시 한다
        LEGACY_STARTUP_FILE.unlink()
    except OSError:
        pass


def play_reset_sound():
    wav = str(APP_ROOT / "web" / "reset.wav")
    if MAC:
        subprocess.Popen(["afplay", wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    import winsound
    try:
        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        winsound.MessageBeep()


def hide_from_dock():
    """맥: Dock·Cmd+Tab 에서 뺀다(메뉴 막대 아이콘만 남는다). 메인 스레드에서 불러야 한다."""
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


def on_main(fn, *args):
    """맥의 AppKit 호출(트레이 제목·메뉴 갱신)은 메인 스레드에서만 안전하다."""
    if MAC:
        try:
            from PyObjCTools import AppHelper
            return AppHelper.callAfter(fn, *args)
        except ImportError:
            pass
    fn(*args)


def mac_notify(msg, title="QuotaTank"):
    def esc(t):
        return t.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.Popen(["osascript", "-e", f'display notification "{esc(msg)}" with title "{esc(title)}"'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_hwnd(title):
    """이 프로세스의 `title` 창 핸들. 창이 막 만들어지는 중일 수 있어 5초까지 기다린다."""
    u32 = ctypes.windll.user32
    u32.FindWindowW.restype = ctypes.c_void_p
    for _ in range(50):
        hwnd = u32.FindWindowW(None, title)
        pid = ctypes.c_ulong()
        if hwnd:
            u32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if pid.value == os.getpid():
                return hwnd
        time.sleep(0.1)
    return None


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", ctypes.c_long * 4),
                ("rcWork", ctypes.c_long * 4), ("dwFlags", ctypes.c_ulong)]


def win_geometry(hwnd):
    """(창 [l, t, r, b], 그 창이 있는 모니터의 작업 영역 [l, t, r, b], 배율). 전부 실제 픽셀.
    pywebview 는 배율이 다른 모니터가 섞이면 창 좌표와 화면 크기 단위가 어긋나서 직접 잰다."""
    u32 = ctypes.windll.user32
    rect = (ctypes.c_long * 4)()
    u32.GetWindowRect(ctypes.c_void_p(hwnd), rect)
    u32.MonitorFromWindow.restype = ctypes.c_void_p
    mon = u32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    u32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
    return list(rect), list(mi.rcWork), u32.GetDpiForWindow(ctypes.c_void_p(hwnd)) / 96


def cursor_monitor():
    """(마우스 x, y, 그 모니터의 작업 영역 [l, t, r, b], 배율). 실제 픽셀."""
    u32, pt = ctypes.windll.user32, wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    u32.MonitorFromPoint.restype = ctypes.c_void_p
    mon = u32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    u32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
    dx, dy = ctypes.c_uint(), ctypes.c_uint()
    ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(mon), 0, ctypes.byref(dx), ctypes.byref(dy))
    return pt.x, pt.y, list(mi.rcWork), dx.value / 96


def mouse_down():
    """왼쪽 마우스 단추가 눌려 있는지(사용자가 창을 끌고 있을 수 있다)."""
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)


DETAIL_CLICK_Y = 44  # 더블클릭으로 자세히 보기를 띄울 때, 클릭 지점이 올 창 위쪽에서의 거리(CSS px)


def beside(win, work, w, h, gap):
    """창(w×h)을 위젯 옆에 붙일 왼쪽 위 좌표. 위젯이 화면 오른쪽 절반이면 왼쪽에, 아니면 오른쪽에 둔다.
    그쪽에 자리가 없으면 반대편, 그래도 없으면 작업 영역 안으로 민다. 위쪽은 위젯과 맞춘다."""
    l, t, r, _ = win
    ml, mt, mr, mb = work
    left, right = l - gap - w, r + gap
    if (l + r) / 2 > (ml + mr) / 2:
        x = left if left >= ml or right + w > mr else right
    else:
        x = right if right + w <= mr or left < ml else left
    return max(ml, min(x, mr - w)), max(mt, min(t, mb - h))


def set_win_rect(hwnd, x, y, w, h):
    ctypes.windll.user32.SetWindowPos(ctypes.c_void_p(hwnd), None, int(x), int(y), int(w), int(h),
                                      0x0004 | 0x0010)  # SWP_NOZORDER | SWP_NOACTIVATE


def round_corners(title):
    """윈도우 11: 제목 표시줄 없는 창도 모서리를 둥글게."""
    hwnd = find_hwnd(title)
    if not hwnd:
        return
    try:
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def hide_from_taskbar(title):
    """이 프로세스의 `title` 창을 도구 창으로 바꿔 작업 표시줄·Alt+Tab 에서 뺀다."""
    u32 = ctypes.WinDLL("user32")  # 전용 핸들(keep_on_top 의 설명 참고)
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    hwnd = find_hwnd(title)
    if not hwnd:
        return
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    ex = u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    u32.ShowWindow(hwnd, 0)  # 작업 표시줄은 다시 보일 때 스타일을 읽는다
    u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
    u32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE


def sunk_below(own, top, top_topmost, top_class):
    """'항상 위' 창(own)이 가려졌는지. top 은 창 가운데 지점에서 맨 위에 있는 최상위 창.
    바탕 화면 보기(Win+D)는 바탕 화면을 항상 위 창보다 위로 올리고, 전체 화면 창도 항상 위 창을 덮을 수 있다.
    다른 항상 위 창(메뉴·툴팁·다른 앱)이 덮은 것은 그대로 둔다."""
    if not top or top == own:
        return False
    return not top_topmost or top_class in ("WorkerW", "Progman")


def keep_on_top(hwnd):
    """항상 위 창이 가려졌으면 포커스를 뺏지 않고 다시 맨 위로 올린다. 올렸으면 True."""
    # ctypes.windll.user32 는 프로세스 전체가 함께 쓴다. 거기에 인자 형식(argtypes)을 걸면 pywebview 가 창을 옮길 때 쓰는
    # SetWindowPos(…, None, None, …) 호출이 형식 오류로 실패해 드래그가 멈춘다. 이 함수 전용 핸들에만 건다
    u32 = ctypes.WinDLL("user32")
    u32.WindowFromPoint.restype = u32.GetAncestor.restype = ctypes.c_void_p
    u32.WindowFromPoint.argtypes = [wintypes.POINT]
    u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
    if not u32.IsWindowVisible(ctypes.c_void_p(hwnd)):
        return False
    rect = (ctypes.c_long * 4)()
    u32.GetWindowRect(ctypes.c_void_p(hwnd), rect)
    top = u32.GetAncestor(u32.WindowFromPoint(wintypes.POINT((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)), 2)  # GA_ROOT
    name = ctypes.create_unicode_buffer(64)
    u32.GetClassNameW(ctypes.c_void_p(top), name, 64)
    if not sunk_below(hwnd, top, bool(top and u32.GetWindowLongPtrW(top, -20) & 0x8), name.value):  # WS_EX_TOPMOST
        return False
    u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    return True


def watch_foreground(on_change):
    """앞에 나온 창이 바뀔 때마다 on_change() 를 부른다(윈도우가 알려 준다 — 주기적으로 확인하지 않는다).
    알림은 이 함수를 부른 스레드의 메시지 루프로 오므로 전용 스레드에서 부른다. 돌아오지 않는다."""
    u32 = ctypes.WinDLL("user32")  # 전용 핸들(keep_on_top 의 설명 참고)
    proc_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.LONG, wintypes.LONG,
                                   wintypes.DWORD, wintypes.DWORD)
    proc = proc_type(lambda *_: on_change())  # 루프가 도는 동안 살아 있어야 한다
    u32.SetWinEventHook.restype = ctypes.c_void_p
    u32.SetWinEventHook.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, proc_type, wintypes.DWORD, wintypes.DWORD, wintypes.UINT]
    if not u32.SetWinEventHook(3, 3, None, proc, 0, 0, 0x0002):  # EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT | SKIPOWNPROCESS
        return
    msg = wintypes.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        u32.DispatchMessageW(ctypes.byref(msg))


INSIGHT_DAYS = 30
CLAUDE_PAST_PATH = APP_DIR / "claude_past.json"   # Claude Code 대화 시각으로 추정한 지난 5시간 창
IMPORT_STATE_PATH = APP_DIR / "import_state.json"  # 이미 읽은 Codex 로그 파일


def import_past(hist, days=INSIGHT_DAYS):
    """지난 사용 기록을 채운다.
    Codex: 세션 로그의 rate_limits 이벤트(실제 사용률) → history.jsonl
    Claude: Claude Code 대화 시각으로 5시간 창을 추정, "hit your session limit" 메시지로 한도 도달 표시 → claude_past.json
    """
    since = time.time() - days * 86400
    try:
        state = json.loads(IMPORT_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    seen = state.setdefault("codex_files", {})
    pts = []
    for p in (HOME / ".codex" / "sessions").rglob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < since or seen.get(str(p)) == mtime:
            continue
        with p.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"rate_limits"' not in line:
                    continue
                try:
                    d = json.loads(line)
                    rl, t = d["payload"]["rate_limits"], iso_to_epoch(d["timestamp"])
                except Exception:
                    continue
                if not isinstance(rl, dict) or not codex_log.is_5h(rl):
                    continue
                limits = codex_log.main_limits(rl)
                if limits["r5"] and t and t >= since:
                    pts.append((t, limits["u5"], limits["r5"], limits["uw"], limits["rw"]))
        seen[str(p)] = mtime
    hist.merge("gpt", pts)
    IMPORT_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

    stamps, hits = set(), []
    for p in (HOME / ".claude" / "projects").rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < since:
                continue
        except OSError:
            continue
        with p.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"type":"user"' not in line and '"type":"assistant"' not in line:
                    continue
                try:
                    t = iso_to_epoch(json.loads(line)["timestamp"])
                except Exception:
                    continue
                if not t or t < since:
                    continue
                if '"<synthetic>"' in line:
                    if "hit your session limit" in line:
                        hits.append(t)
                    continue
                stamps.add(round(t))
    past = []
    for t in sorted(stamps):
        if not past or t >= past[-1]["start"] + FIVE_H:
            past.append({"start": t, "hit": False})
    for h in hits:
        for w in past:
            if w["start"] <= h < w["start"] + FIVE_H:
                w["hit"] = True
                w.setdefault("hit_at", h)
    CLAUDE_PAST_PATH.write_text(json.dumps(past), encoding="utf-8")


def claude_past():
    try:
        return json.loads(CLAUDE_PAST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def insights_data(hist, data, now):
    """인사이트 창용: Claude·Codex 기록을 합쳐 사이클 통계, 겹침, 넘기기 기회를 계산."""
    since = now - INSIGHT_DAYS * 86400
    rows = [r for r in hist.rows if r["t"] >= since]
    past = [w for w in claude_past() if w["start"] >= since]
    out = {"samples": len(rows), "days": INSIGHT_DAYS,
           "span_days": (now - min([r["t"] for r in rows] + [w["start"] for w in past] + [now])) / 86400,
           "estimated": len(past), "svc": {}, "weekly": {}}
    spans, hit_at = {}, {k: [] for k, _, _ in SERVICES}
    for key, _, _ in SERVICES:
        wins = [w for w in windows(hist, key) if w[0] >= since]
        seen = set()
        for r in hist.svc_rows(key):
            if r["u5"] >= 99 and r["r5"] not in seen and r["t"] >= since:
                seen.add(r["r5"])
                hit_at[key].append(r["t"])
        if key == "claude":
            # 위젯이 직접 잰 창과 겹치지 않는 추정 창만 더한다. 사용률은 한도 도달(100%)만 안다.
            for w in past:
                if not any(abs(w["start"] - s) < FIVE_H for s, _ in wins):
                    wins.append((w["start"], 100 if w["hit"] else None))
                    if w["hit"]:
                        hit_at["claude"].append(w.get("hit_at", w["start"]))
            wins.sort()
        heat = [0] * 168
        by_day = defaultdict(list)
        for start, _ in wins:
            d = datetime.fromtimestamp(start)
            heat[d.weekday() * 24 + d.hour] += 1
            by_day[d.date()].append(start)
        first = None
        if by_day:
            first = round(statistics.median(
                datetime.fromtimestamp(min(v)).hour * 60 + datetime.fromtimestamp(min(v)).minute
                for v in by_day.values()))
        peaks = [round(p) for _, p in wins if p is not None]
        out["svc"][key] = {"n": len(wins), "hits": sum(p >= 99 for p in peaks), "peaks": peaks,
                           "first": first, "heat": heat}
        spans[key] = [(s, s + FIVE_H) for s, _ in wins]

    # 서비스 5시간 창이 겹친 시간 / 둘 중 하나라도 열린 시간
    sets = {k: {m for s, e in spans[k] for m in range(int(s // 60), int(e // 60))} for k in spans}
    union = set().union(*sets.values()) if sets else set()
    all_minutes = defaultdict(int)
    for mset in sets.values():
        for m in mset:
            all_minutes[m] += 1
    overlap = sum(1 for count in all_minutes.values() if count >= 2)
    out["overlap_pct"] = overlap / len(union) * 100 if union else None

    # 한도에 닿은 순간, 다른 서비스 중 하나라도 절반 넘게 여유가 있었나.
    # 한도 도달 한 번을 한 번으로 센다. 다른 쪽 사용률 기록이 하나도 없으면 판단하지 않는다.
    ok = judged = 0
    for key, _, _ in SERVICES:
        for t in hit_at.get(key, []):
            rooms = []
            for other, _, _ in SERVICES:
                if other == key:
                    continue
                prev = [o for o in hist.svc_rows(other) if t - 5400 <= o["t"] <= t]
                if prev:
                    o = prev[-1]
                    u5 = o["u5"] if o["r5"] and o["r5"] > t else 0
                    uw = o["uw"] if o["rw"] and o["rw"] > t else 0
                    rooms.append(min(100 - u5, 100 - uw))
            if not rooms:
                continue
            judged += 1
            if max(rooms) >= 50:
                ok += 1
    out["handoff_ok"], out["handoff_n"] = ok, judged

    for key, _, _ in SERVICES:
        d = data.get(key)
        if d and d["rw"] and d["rw"] > now:
            elapsed_days = max((WEEK - (d["rw"] - now)) / 86400, 0.1)
            out["weekly"][key] = {"elapsed_days": elapsed_days, "proj": d["uw"] / elapsed_days * 7,
                                  "budget": (100 - d["uw"]) / max((d["rw"] - now) / 86400, 0.1)}
    return out


# ── 토큰·비용 집계: 로컬 대화 기록의 토큰 수 × API 단가 (구독 요금이 아닌 환산 추정치) ──
COST_CACHE_PATH = APP_DIR / "cost_cache.json"  # 파일별 집계. 바뀐 파일만 다시 읽는다
COST_DAYS = 30
# $/1M 토큰: (입력, 출력, 캐시 읽기, 캐시 쓰기 5분, 캐시 쓰기 1시간). 이름 앞부분이 가장 길게 맞는 행을 쓴다.
# Standard API, USD/1M tokens, verified 2026-10-03:
# https://developers.openai.com/api/docs/pricing
# https://developers.openai.com/api/docs/models/gpt-6-sol (previous Sol)
# https://developers.openai.com/api/docs/models/gpt-5.6-{sol,terra,luna}
# https://platform.claude.com/docs/en/about-claude/pricing
# Codex 구독 크레딧 단가가 아닌 API 환산 단가. prices 설정으로 덮어쓸 수 있다.
PRICES = {
    "claude-fable-5-1": (10, 50, 0.25, 12.5, 20), "claude-mythos-5-1": (10, 50, 0.25, 12.5, 20),
    "claude-fable-5": (10, 50, 1.0, 12.5, 20), "claude-opus-5-5": (4, 20, 0.2, 5, 8),
    "claude-opus-5": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-8": (5, 25, 0.5, 6.25, 10), "claude-opus-4-7": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-6": (5, 25, 0.5, 6.25, 10), "claude-opus-4-5": (5, 25, 0.5, 6.25, 10),
    "claude-opus-4-1": (15, 75, 1.5, 18.75, 30), "claude-opus-4": (15, 75, 1.5, 18.75, 30),
    "claude-sonnet-5-5": (2, 10, 0.2, 2.5, 4), "claude-sonnet-5": (2, 10, 0.2, 2.5, 4),
    "claude-sonnet-4-6": (3, 15, 0.3, 3.75, 6), "claude-sonnet-4-5": (3, 15, 0.3, 3.75, 6),
    "claude-sonnet-4": (3, 15, 0.3, 3.75, 6),
    "claude-haiku-4-5": (1, 5, 0.1, 1.25, 2),
    "gpt-6-astra": (10, 50, 1.0, 12.5, 12.5), "gpt-6.1-sol": (2, 10, 0.1, 2.5, 2.5),
    "gpt-6-sol": (2, 10, 0.2, 2.5, 2.5), "gpt-6-luna": (0.1, 0.5, 0.01, 0.125, 0.125),
    "gpt-5.6-sol": (4, 20, 0.4, 5, 5), "gpt-5.6": (4, 20, 0.4, 5, 5),
    "gpt-5.6-terra": (2, 12, 0.2, 2.5, 2.5), "gpt-5.6-luna": (0.2, 1.2, 0.02, 0.25, 0.25),
    "gpt-5.3-codex": (1.75, 14, 0.175, 1.75, 1.75),
}


def price_of(model, overrides=None):
    table = dict(PRICES)
    custom = set()
    for k, v in (overrides or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == 5:
            try:
                values = tuple(float(x) for x in v)
                if all(0 <= x < float("inf") for x in values):
                    table[k] = values
                    custom.add(k)
            except (TypeError, ValueError):
                pass
    # 기본 단가는 정확한 모델 이름·날짜 스냅샷에만 적용. 새 변종에 기존 단가를 추측하지 않는다.
    best = max((k for k in table if model == k or
                model.startswith(k) and (k in custom or re.fullmatch(r"-\d{4}-?\d{2}-?\d{2}", model[len(k):]))),
               key=len, default=None)
    return table[best] if best else None


def _scan_claude_file(p):
    """Claude Code 대화 기록 한 파일: {메시지키: [날짜, 모델, 입력, 출력, 캐시읽기, 캐시쓰기5분, 캐시쓰기1시간, 시각(epoch)]}.
    같은 응답이 내용 블록마다 여러 줄로 남아서 message.id + requestId 로 하나만 센다."""
    rows = {}
    with p.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
                m = d["message"]
                u, model = m["usage"], m.get("model") or ""
            except Exception:
                continue
            if model == "<synthetic>" or not isinstance(u, dict):
                continue
            t = iso_to_epoch(d.get("timestamp"))
            if not t:
                continue
            cc = u.get("cache_creation") or {}
            cw = u.get("cache_creation_input_tokens") or 0
            w1 = cc.get("ephemeral_1h_input_tokens") or 0
            rows[f"{m.get('id')}:{d.get('requestId')}"] = [
                datetime.fromtimestamp(t).strftime("%Y-%m-%d"), model, u.get("input_tokens") or 0,
                u.get("output_tokens") or 0, u.get("cache_read_input_tokens") or 0, max(cw - w1, 0), w1, round(t)]
    return rows


def _scan_codex_file(p):
    """Codex 세션 로그 한 파일: [[날짜, 모델, 입력(캐시 포함), 캐시읽기, 캐시쓰기, 출력]].
    token_count 이벤트의 누적값(total_token_usage) 차이를 그 이벤트 날짜에 더한다(같은 이벤트가 반복돼도 안전)."""
    agg, model, prev = defaultdict(lambda: [0, 0, 0, 0]), "", None
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
    with p.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"turn_context"' in line:
                try:
                    model = json.loads(line)["payload"].get("model") or model
                except Exception:
                    pass
                continue
            if '"token_count"' not in line:
                continue
            try:
                d = json.loads(line)
                info = d["payload"]["info"]
                tot = info["total_token_usage"]
                t = iso_to_epoch(d["timestamp"])
            except Exception:
                continue
            if not t or not isinstance(tot, dict):
                continue
            cur = [tot.get(k) or 0 for k in keys]
            if prev is None or any(c < q for c, q in zip(cur, prev)):  # 첫 이벤트·누적 초기화
                last = info.get("last_token_usage") or {}
                delta = [last.get(k) or 0 for k in keys] if prev is not None else cur
            else:
                delta = [c - q for c, q in zip(cur, prev)]
            prev = cur
            if any(delta):
                last = info.get("last_token_usage") or {}
                long_context = (last.get("input_tokens") or 0) > 272_000
                a = agg[(datetime.fromtimestamp(t).strftime("%Y-%m-%d"), model, long_context)]
                for i in range(4):
                    a[i] += delta[i]
    return [[day, mdl, *v, long_context] for (day, mdl, long_context), v in agg.items()]


def scan_costs(days=COST_DAYS):
    """바뀐 기록 파일만 다시 읽어 cost_cache.json 을 갱신한다. 처음엔 몇십 초 걸릴 수 있다."""
    since = time.time() - (days + 1) * 86400
    try:
        cache = json.loads(COST_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    if cache.get("version") != 3:  # 요청 시각을 더해서(공유 사용량 나누기) 기존 집계도 다시 읽는다
        cache = {"version": 3}
    for svc, root, scan in (("claude", HOME / ".claude" / "projects", _scan_claude_file),
                            ("gpt", HOME / ".codex" / "sessions", _scan_codex_file)):
        old, new = cache.get(svc, {}), {}
        for p in root.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < since:
                continue
            c = old.get(str(p))
            if c and c["m"] == st.st_mtime and c["s"] == st.st_size:
                new[str(p)] = c
                continue
            try:
                new[str(p)] = {"m": st.st_mtime, "s": st.st_size, "rows": scan(p)}
            except OSError:
                continue
        cache[svc] = new
    tmp = COST_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, COST_CACHE_PATH)
    return cache


def cost_summary(cache, overrides=None, days=COST_DAYS, now=None):
    """인사이트 '비용' 탭용: 서비스별 합계, 날짜별 금액, 모델별 토큰·금액."""
    now = now or time.time()
    dates = [(datetime.fromtimestamp(now) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    first = dates[0]
    out = {"dates": dates, "svc": {}, "pricing_date": "2026-10-03", "basis": "Standard API"}
    for svc in ("claude", "gpt"):
        daily, models = defaultdict(float), defaultdict(lambda: {"tokens": 0, "cost": 0.0, "known": True})
        if svc == "claude":  # 이어 하기·분기한 세션은 같은 응답을 다시 담고 있어 파일을 넘어 한 번만 센다
            merged = {}
            for c in (cache.get(svc) or {}).values():
                merged.update(c["rows"])
            items = merged.values()
        else:
            items = [r for c in (cache.get(svc) or {}).values() for r in c["rows"]]
        for r in items:
            day, model = r[0], r[1]
            if day < first or day > dates[-1]:
                continue
            p = price_of(model, overrides)
            if svc == "claude":
                i, o, cr, w5, w1 = r[2:7]
                tokens = i + o + cr + w5 + w1
                cost = (i * p[0] + o * p[1] + cr * p[2] + w5 * p[3] + w1 * p[4]) / 1e6 if p else 0.0
            else:
                i, cr, cw, o = r[2:6]
                tokens = i + o
                long_context = len(r) > 6 and r[6] and model.startswith(("gpt-6", "gpt-5.6"))
                input_factor, output_factor = (2, 1.5) if long_context else (1, 1)
                cost = ((max(i - cr - cw, 0) * p[0] + cr * p[2] + cw * p[3]) * input_factor
                        + o * p[1] * output_factor) / 1e6 if p else 0.0
            m = models[model or "알 수 없음"]
            m["tokens"] += tokens
            m["cost"] += cost
            m["known"] = m["known"] and p is not None
            daily[day] += cost
        ms = sorted(({"model": k, **v} for k, v in models.items()), key=lambda m: (-m["cost"], -m["tokens"]))
        out["svc"][svc] = {"total": sum(daily.values()), "tokens": sum(m["tokens"] for m in ms),
                           "daily": [round(daily.get(d, 0.0), 4) for d in dates], "models": ms[:8],
                           "unpriced": sum(m["tokens"] for m in ms if not m["known"])}
    return out


# ── 공유 사용량 나누기(share.py): 이 PC 의 Claude Code 기록 × 환산 계수 ──
SHARE_STATE_PATH = APP_DIR / "share_state.json"  # 직접 맞춘·배운 환산 계수. 깨지면 버리고 기본값으로 간다
SHARE_RECENT_HOURS = 6


def load_share_state():
    try:
        return share.clean_state(json.loads(SHARE_STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return {}


def save_share_state(state):
    try:
        atomic_json(SHARE_STATE_PATH, state)
    except Exception:
        pass


def claude_rows(cache):
    """비용 집계 캐시의 Claude 응답 행을 파일을 넘어 한 번씩만(이어 하기 세션이 같은 응답을 다시 담고 있다)."""
    merged = {}
    for c in (cache.get("claude") or {}).values():
        merged.update(c["rows"])
    return merged


def scan_recent_claude(memo, hours=SHARE_RECENT_HOURS, root=None):
    """최근 hours 시간 안에 바뀐 Claude Code 기록 파일만 다시 읽는다. memo {경로: (수정 시각, 크기, 행)} 를 새로 만들어 돌려준다.
    비용 집계는 1시간마다라 나누기 값이 늦어지므로, 지금 쓰고 있는 파일은 따로 따라간다."""
    root = root or HOME / ".claude" / "projects"
    since = time.time() - hours * 3600
    new = {}
    for p in root.rglob("*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < since:
            continue
        old = memo.get(str(p))
        if old and old[0] == st.st_mtime and old[1] == st.st_size:
            new[str(p)] = old
            continue
        try:
            new[str(p)] = (st.st_mtime, st.st_size, _scan_claude_file(p))
        except OSError:
            continue
    return new


# ── 알림 영역 아이콘: 서비스별 5시간 사용률 막대 ──────────────────────────
TRAY_ORDER = ("claude", "gpt", "gemini")


def tray_image(levels):
    """levels: [5시간 사용률 또는 None] × 3 → 64×64 아이콘. 막대 높이 = 사용률, 색 = 여유/주의/위험."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    g = ImageDraw.Draw(img)
    for i, u in enumerate(levels):
        x0 = 2 + i * 21
        g.rounded_rectangle((x0, 4, x0 + 17, 60), radius=5, fill=(120, 126, 138, 150))
        if u is None:
            continue
        h = max(6, round(56 * min(max(u, 0), 100) / 100))
        color = (74, 222, 128, 255) if u < 60 else (251, 191, 36, 255) if u < 85 else (248, 113, 113, 255)
        g.rounded_rectangle((x0, 60 - h, x0 + 17, 60), radius=5, fill=color)
    return img


# ── 리밋에 걸려 멈춘 Claude Code 터미널 세션 이어 가기 (윈도우) ──────────
CLAUDE_DIR = HOME / ".claude"


def proc_start(pid):
    """살아 있는 프로세스의 생성 시각(FILETIME 정수). 없거나 끝났으면 None."""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    h = ctypes.c_void_p(h)
    try:
        code, t = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        if not k32.GetProcessTimes(h, *map(ctypes.byref, t)):
            return None
        return t[0].dwHighDateTime << 32 | t[0].dwLowDateTime
    finally:
        k32.CloseHandle(h)


def last_turn(path):
    """대화 기록(jsonl)의 마지막 user/assistant 항목. 파일 끝 256KB 만 읽는다."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 262144))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("type") in ("user", "assistant") and not d.get("isSidechain"):
            return d
    return None


PANEL_ENTRY = ("claude-vscode",)  # VS Code(계열) 확장 패널에서 띄운 Claude Code


def limited_claude_sessions(entrypoints=("cli",)):
    """리밋 에러로 끝난 채 떠 있는 Claude Code 세션: [(pid, sessionId, cwd, 에러 문구, 에러 시각)].
    ~/.claude/sessions/<pid>.json 이 실행 중인 프로세스와 세션을 잇는다. 기본은 터미널(cli) 세션만,
    확장 패널 세션은 PANEL_ENTRY 로 따로 찾는다(콘솔이 없어 입력 방법이 다르다)."""
    out = []
    for f in (CLAUDE_DIR / "sessions").glob("*.json"):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
            if s.get("entrypoint") not in entrypoints or s.get("kind") != "interactive":
                continue
            start = proc_start(s["pid"])
            if start is None or abs(start - int(s.get("procStart") or start)) > 10 ** 7:  # pid 재사용
                continue
            tr = next((CLAUDE_DIR / "projects").glob(f"*/{s['sessionId']}.jsonl"), None)
            d = tr and last_turn(tr)
            if d and d.get("type") == "assistant" and d.get("error") == "rate_limit":
                text = "".join(c.get("text", "") for c in d["message"]["content"] if isinstance(c, dict))
                out.append((s["pid"], s["sessionId"], s.get("cwd", ""), text, iso_to_epoch(d["timestamp"])))
        except Exception:
            continue
    return out


RESUME_DELAY = 1  # 리셋 시각에서 이만큼 지나 입력한다
RESUME_LEAD = 8   # 보조 프로세스를 이만큼 먼저 띄워 기다리게 한다(프로세스·브로커가 뜨는 시간). 입력은 리셋 뒤에만 한다


def resume_at(text, err_ts, d):
    """리밋이 풀리는 시각. 에러가 난 창의 리셋 시각(사용량 API)을 쓰고, 이미 다음 창으로 넘어갔으면
    에러 문구("resets 2:10pm (Asia/Seoul)")를 읽는다."""
    weekly = "weekly" in text.lower()
    r, span = (d.get("rw"), WEEK) if weekly else (d.get("r5"), FIVE_H)
    if r and r - span <= err_ts < r:
        return r
    m = re.search(r"resets\s+(\d{1,2})(?::(\d\d))?\s*([ap]m)", text, re.I)
    if not m or weekly:
        return None
    t = datetime.fromtimestamp(err_ts).replace(
        hour=int(m[1]) % 12 + (12 if m[3].lower() == "pm" else 0), minute=int(m[2] or 0), second=0, microsecond=0)
    if t.timestamp() <= err_ts:
        t += timedelta(days=1)
    return t.timestamp()


def console_type(pid, text):
    """pid 가 붙어 있는 콘솔(터미널)의 입력 버퍼에 text 와 Enter 를 넣는다. 창 포커스는 건드리지 않는다.
    AttachConsole 은 콘솔이 없는 프로세스에서만 되므로 type_into_console 이 별도 프로세스로 부른다."""
    if not isinstance(text, str) or not 0 < len(text) <= 200 or any(ord(c) < 32 for c in text):
        return False
    class KEY(ctypes.Structure):
        _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                    ("wVirtualScanCode", wintypes.WORD), ("UnicodeChar", wintypes.WCHAR),
                    ("dwControlKeyState", wintypes.DWORD)]

    class EV(ctypes.Union):
        _fields_ = [("KeyEvent", KEY), ("_pad", ctypes.c_byte * 16)]

    class REC(ctypes.Structure):
        _fields_ = [("EventType", wintypes.WORD), ("Event", EV)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        return False
    try:
        h = k32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)  # 읽기/쓰기, 공유, OPEN_EXISTING
        if not h or h == ctypes.c_void_p(-1).value:
            return False
        h = ctypes.c_void_p(h)

        def send(units, vk=0, scan=0):
            recs = (REC * (2 * len(units)))()
            for i, ch in enumerate(units):
                for j, down in enumerate((True, False)):
                    r = recs[2 * i + j]
                    r.EventType = 1  # KEY_EVENT
                    r.Event.KeyEvent = KEY(down, 1, vk, scan, ch, 0)
            n = wintypes.DWORD()
            return k32.WriteConsoleInputW(h, recs, len(recs), ctypes.byref(n)) and n.value == len(recs)

        raw = text.encode("utf-16-le")  # 이모지 같은 서로게이트 쌍도 UTF-16 단위로 보낸다
        units = [raw[i:i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(raw), 2)]
        ok = send(units)
        time.sleep(0.6)  # 한꺼번에 들어온 글자와 붙으면 붙여넣기로 보고 Enter 를 줄바꿈으로 받는다
        ok = send(["\r"], 0x0D, 0x1C) and ok
        k32.CloseHandle(h)
        return bool(ok)
    finally:
        k32.FreeConsole()


def type_into_console(pid, text, not_before=0):
    """console_type 을 콘솔 없는 별도 프로세스에서 돌린다(위젯이 콘솔에서 실행됐어도 그 콘솔을 놓지 않게).
    not_before: 보조 프로세스가 이 시각까지 기다렸다가 확인·입력한다."""
    try:
        me = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
        r = subprocess.run(me + ["--type-into", str(pid), text, f"{not_before:.2f}"],
                           creationflags=0x00000008, timeout=90)  # DETACHED_PROCESS
        # 보조 프로세스는 리셋 시각까지 기다리고, 사용량을 새로 조회한 뒤에야 입력한다(브로커 대기 25초 + 시작 시간)
        return r.returncode == 0
    except Exception:
        return False


def authorized_console_resume(pid, text, not_before=0):
    """The helper cannot be used as a general purpose terminal input executable."""
    cfg = load_config()
    if not cfg["auto_resume"] or text != cfg["resume_text"]:
        return False
    for target, sid, cwd, message, stamp in limited_claude_sessions():
        if target != pid:
            continue
        try:
            # not_before can only delay the checks below (bounded); it never skips them.
            try:
                BROKER.warm()
            except Exception:
                pass
            wait = min(not_before - time.time(), RESUME_LEAD + 5)
            if wait > 0:
                time.sleep(wait)
            # A fresh query is required before the helper sends any input.
            d = provider_call("fetch", "claude")
            at = resume_at(message, stamp, d)
            if not at or time.time() < at + RESUME_DELAY or d["u5"] >= 100 or d["uw"] >= 100:
                return False
            # Recheck the option, text and process/session immediately before writing (the wait above can be long).
            cfg = load_config()
            if not cfg["auto_resume"] or text != cfg["resume_text"]:
                return False
            if (target, sid, cwd, message, stamp) not in limited_claude_sessions():
                return False
            return console_type(pid, text)
        except Exception:
            return False
    return False


# ── 사이클 예약: 정한 시각에 공식 CLI 로 한 마디를 보내 5시간 창을 그때 시작한다 ──
CYCLE_GRACE = 600  # 예약 시각에서 이만큼 지났으면(PC 가 꺼져 있었으면) 건너뛴다: 엉뚱한 시각에 창을 열지 않는다
# 가장 작은 모델에 한 마디. 도구·사용자 설정(훅·플러그인·CLAUDE.md)·세션 저장 없이. --tools 는 뒤의 값을 모두 가져가므로 맨 끝에 둔다
CYCLE_ARGS = ("-p", "hi", "--safe-mode", "--model", "haiku", "--no-session-persistence", "--tools", "")


def cycle_due(times, now, fired):
    """지금 보낼 예약 [(날짜, "HH:MM")]: 예약 시각부터 CYCLE_GRACE 초 안이고 오늘 아직 다루지 않은 것."""
    out = []
    for hhmm in times:
        at = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
        key = (now.strftime("%Y-%m-%d"), hhmm)
        if 0 <= (now - at).total_seconds() < CYCLE_GRACE and key not in fired:
            out.append(key)
    return out


def cycle_missed(times, since, now):
    """since(위젯이 마지막으로 깨어 있던 때)부터 지금 사이에 CYCLE_GRACE 넘게 지나가 버린 오늘의 예약 [(날짜, "HH:MM")]:
    PC 가 꺼져 있거나 잠들어 놓친 것."""
    out = []
    for hhmm in times:
        at = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
        if since < at and (now - at).total_seconds() >= CYCLE_GRACE:
            out.append((now.strftime("%Y-%m-%d"), hhmm))
    return out


# 켤 때 Claude 시작: PC 를 켜거나 깨운 뒤 이만큼 안에 Claude 토큰이 만료돼 있으면 한 마디를 보낸다
BOOT_WINDOW = 600
# PC 가 꺼져 있어 예약을 놓쳤을 때 권하는 클라우드 루틴(공식 문서: 루틴도 구독 사용량에 잡힌다. 정각은 몇 분 늦게 돌 수 있다)
CLOUD_TIP = "PC 를 켜지 않아도 열려면 Claude Code 에서 /schedule 로 매일 {at} 쯤 \"hi\" 를 보내는 클라우드 루틴을 만들어 보세요"


def start_claude_window():
    """이미 로그인된 공식 Claude Code CLI 로 한 마디를 보낸다(빈 임시 폴더에서, 창 없이). 보냈으면 True.
    위젯은 로그인 정보를 읽거나 넘기지 않는다: CLI 가 평소처럼 자기 로그인으로 요청한다."""
    cli = find_cli("claude")
    # 절대 경로만 실행한다: 윈도우의 which 는 위젯을 띄운 폴더의 claude.cmd 를 먼저 찾는다.
    # .cmd 로 깔린 CLI 는 cmd.exe 를 거쳐 실행되므로 경로에 셸이 달리 읽는 글자가 있으면 실행하지 않는다
    if not cli or not os.path.isabs(cli) or any(c in cli for c in "&|<>^%!,;=\r\n"):
        return False
    folder = tempfile.mkdtemp(prefix="quotatank-cycle-")
    proc = None
    try:
        # 구독 로그인(기본 프로필)으로 보내야 창이 시작된다: API 키·다른 인증·다른 프로필을 가리키는 환경변수는 넘기지 않는다
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")) and k.upper() != "CLAUDE_CONFIG_DIR"}
        if MAC:  # npm 으로 깐 CLI 는 node 를 PATH 에서 찾는다
            env["PATH"] = os.pathsep.join([os.path.dirname(cli), *MAC_CLI_DIRS, env.get("PATH", "")])
        proc = subprocess.Popen([cli, *CYCLE_ARGS], cwd=folder, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=MAC,
                                creationflags=0 if MAC else subprocess.CREATE_NO_WINDOW)
        return proc.wait(timeout=120) == 0
    except Exception:
        stop_login(proc)  # 시간 안에 끝나지 않았으면 CLI 가 띄운 자식(node 등)까지 끝낸다
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)
# ── VS Code 확장 패널 세션 이어 가기 (윈도우, 자리 비웠을 때만) ─────────
# 확장은 이미 열린 세션에 바깥에서 문구를 넣지 않는다(vscode://…/open 의 prompt 는 무시된다).
# 그래서 사용자가 자리를 비웠을 때만 그 세션의 편집기 창과 패널을 앞으로 띄우고, 포커스가 Claude 입력창이고
# 그 패널에 이 세션의 한도 안내가 보이는지 UI Automation 으로 확인한 뒤 키 입력을 보낸다.
# 편집기 창이나 패널 세션이 여럿이어도 된다. 조건이 하나라도 어긋나면 아무것도 입력하지 않는다.
PANEL_IDLE_SEC = 240  # 윈도우는 입력이 200초쯤 없어야 다른 프로그램이 창을 앞으로 가져오게 둔다
PANEL_FRAME_ID = "active-frame"  # 확장 패널(webview) 문서의 접근성 id
PANEL_MAX_AGE = 6 * 3600  # 한도가 풀린 지 이만큼 지난 세션에는 넣지 않는다(며칠 전 멈춘 채 열려 있는 패널)
PANEL_INPUT_NAME = "Message input"  # 확장 입력창의 aria-label
EDITORS = {"code.exe": ("vscode", "Code"), "code - insiders.exe": ("vscode-insiders", "Code - Insiders"),
           "cursor.exe": ("cursor", "Cursor"), "windsurf.exe": ("windsurf", "Windsurf")}  # 실행파일: (URI 스킴, 설정 폴더)
POWERSHELL = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def process_exe(pid):
    """프로세스 실행파일 전체 경로. 못 읽으면 None."""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    h = ctypes.c_void_p(h)
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k32.CloseHandle(h)


def parent_pid(pid):
    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return None
    snap = ctypes.c_void_p(snap)
    try:
        e = ENTRY(dwSize=ctypes.sizeof(ENTRY))
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ProcessID == pid:
                return e.th32ParentProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        return None
    finally:
        k32.CloseHandle(snap)


def panel_editor(pid):
    """확장 패널 세션(claude.exe)을 띄운 편집기의 실행파일 경로. 아는 편집기가 아니면 None."""
    parent = parent_pid(pid)
    started, parent_started = proc_start(pid), parent and proc_start(parent)
    if not parent_started or not started or parent_started > started:  # 부모가 끝나고 pid 가 재사용됐다
        return None
    exe = process_exe(parent)
    return exe if exe and Path(exe).name.lower() in EDITORS else None


def editor_windows(exe):
    """exe 로 떠 있는 보이는 편집기 창(제목 있는 최상위 Chromium 창): [(핸들, 프로세스 id, 제목)]. 세다가 실패하면 None."""
    u32, out, failed = ctypes.windll.user32, [], []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)
    def each(hwnd, _):
        try:
            h, cls = ctypes.c_void_p(hwnd), ctypes.create_unicode_buffer(64)
            if u32.IsWindowVisible(h) and not u32.GetWindow(h, 4) and u32.GetWindowTextLengthW(h) > 0:  # GW_OWNER
                u32.GetClassNameW(h, cls, 64)
                pid = wintypes.DWORD()
                u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if cls.value == "Chrome_WidgetWin_1" and (process_exe(pid.value) or "").lower() == exe.lower():
                    title = ctypes.create_unicode_buffer(512)
                    u32.GetWindowTextW(h, title, 512)
                    out.append((hwnd, pid.value, title.value))
            return True
        except Exception:  # 하나라도 못 세면 창 수를 믿을 수 없다
            failed.append(1)
            return False
    u32.EnumWindows(each, None)
    return None if failed else out


def foreground_window():
    """앞에 나와 있는 창: (핸들, 실행파일 경로). 없으면 (None, None)."""
    u32 = ctypes.windll.user32
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    hwnd, pid = u32.GetForegroundWindow(), wintypes.DWORD()
    if not hwnd:
        return None, None
    u32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return hwnd, process_exe(pid.value)


def panel_window(exe, cwd):
    """세션 폴더(cwd)를 연 편집기 창: (핸들, 프로세스 id). 제목의 폴더 칸이 그 폴더 이름인 창이 정확히 하나일 때만 돌려준다.
    편집기 제목은 "파일 - 폴더 - Visual Studio Code" 꼴이라 끝에서 둘째 칸만 본다(파일 이름이 폴더 이름과 같은 창을 고르지 않게).
    같은 이름의 폴더를 두 창에서 열었거나, 폴더 칸을 알 수 없는 창(여러 폴더 작업 영역)이 하나라도 있으면 구분할 수 없어 None."""
    folder = Path(cwd).name if cwd else ""
    wins = editor_windows(exe) if folder else None
    if not wins or any("(Workspace)" in w[2] for w in wins):
        return None
    hits = [w for w in wins if len(w[2].split(" - ")) >= 2 and w[2].split(" - ")[-2] == folder]
    return hits[0][:2] if len(hits) == 1 else None


def limit_marker(message):
    """한도 안내 문구에서 패널 화면과 맞춰 볼 조각("resets3:45pm"). 리셋 시각이 없으면 None(확인할 수 없어 입력하지 않는다)."""
    m = re.search(r"resets\s+\d{1,2}(?::\d\d)?\s*[ap]m", message or "", re.I)
    return re.sub(r"\s+", "", m[0]).lower() if m else None


def last_input_tick():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return info.dwTime


def idle_seconds():
    """마지막 키보드·마우스 입력 뒤 지난 초."""
    return ((ctypes.windll.kernel32.GetTickCount() - last_input_tick()) & 0xFFFFFFFF) / 1000


def screen_locked():
    """잠금 화면이면 입력 데스크톱을 열 수 없다."""
    u32 = ctypes.windll.user32
    u32.OpenInputDesktop.restype = ctypes.c_void_p
    h = u32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    u32.CloseDesktop(ctypes.c_void_p(h))
    return False


def user_busy():
    """발표·전체 화면(화면 공유·영상 포함)·방해 금지 중인지. 입력이 없어도 자리를 비운 게 아닐 수 있다."""
    state = ctypes.c_int()
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
        return True
    # NOT_PRESENT(화면 보호기), BUSY, RUNNING_D3D_FULL_SCREEN, PRESENTATION_MODE, QUIET_TIME, APP(전체 화면 앱)
    return state.value in (1, 2, 3, 4, 6, 7)


def user_away():
    return idle_seconds() >= PANEL_IDLE_SEC and not screen_locked() and not user_busy()


# 포커스가 있는 요소의 프로세스·이름·글자, 그리고 그 요소가 든 패널 문서의 마지막 글 120조각에 QT_MARKER 가 보이는지(1/0)
UIA_FOCUS_SCRIPT = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
    "$A=[System.Windows.Automation.AutomationElement];$e=$A::FocusedElement;if(-not $e){exit 1};"
    "$t=[char]0;try{$t=$e.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(400)}catch{};"
    "$shown=0;if($env:QT_MARKER){$w=[System.Windows.Automation.TreeWalker]::RawViewWalker;$f=$e;$i=0;"
    "while($f -ne $null -and $f.Current.AutomationId -ne $env:QT_FRAME -and $i -lt 20){$f=$w.GetParent($f);$i++};"
    "if($f -ne $null -and $f.Current.AutomationId -eq $env:QT_FRAME){"
    "$c=New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty,[System.Windows.Automation.ControlType]::Text);"
    "$n=@($f.FindAll([System.Windows.Automation.TreeScope]::Descendants,$c)|ForEach-Object{$_.Current.Name});"
    "$tail=(($n|Select-Object -Last 120) -join '') -replace '\\s','';"
    "if($tail.ToLower().Contains($env:QT_MARKER)){$shown=1}}};"
    "[Console]::Out.Write(\"$($e.Current.ProcessId)`t$($e.Current.Name)`t$shown`t$t\")")


def focused_ui(marker=None):
    """키보드 포커스가 있는 UI 요소: (프로세스 id, 접근성 이름, 입력된 글자, 그 패널 끝에 marker 가 보이는지). 못 읽으면 None.
    입력된 글자를 못 읽으면 "\\0" 이 온다(비어 있다고 보지 않는다). marker 는 limit_marker 가 만든 영숫자·콜론 조각만 받는다.
    파이썬 의존성을 늘리지 않으려고 윈도우에 기본으로 있는 UI Automation(.NET)을 PowerShell 로 부른다."""
    if marker is not None and not re.fullmatch(r"[a-z0-9:]{6,20}", marker):
        return None
    try:
        r = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", UIA_FOCUS_SCRIPT],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                           env=dict(os.environ, QT_MARKER=marker or "", QT_FRAME=PANEL_FRAME_ID),
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        pid, name, shown, text = r.stdout.split("\t", 3)
        return int(pid), name, text, shown == "1"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def ctrl_enter_to_send(exe):
    """편집기 설정에서 claudeCode.useCtrlEnterToSend 를 켰는지(그러면 Enter 대신 Ctrl+Enter 로 보낸다)."""
    folder = EDITORS[Path(exe).name.lower()][1]
    try:
        raw = (Path(os.environ.get("APPDATA", HOME)) / folder / "User" / "settings.json").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return bool(re.search(r'"claudeCode\.useCtrlEnterToSend"\s*:\s*true', raw))


def send_input(events):
    """키 이벤트 [(가상 키, 스캔 코드, 플래그)] 를 SendInput 한 번으로 보낸다(다른 입력이 끼어들지 않는다)."""
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", UNION)]
    arr = (INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)) == len(events)


def text_events(text):
    raw = text.encode("utf-16-le")  # 이모지 같은 서로게이트 쌍도 UTF-16 단위로 보낸다
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    return [(0, u, flags) for u in units for flags in (0x0004, 0x0004 | 0x0002)]  # KEYEVENTF_UNICODE, KEYUP


def enter_events(ctrl):
    enter = [(0x0D, 0x1C, 0), (0x0D, 0x1C, 0x0002)]
    return [(0x11, 0x1D, 0)] + enter + [(0x11, 0x1D, 0x0002)] if ctrl else enter


def panel_precheck(pid, cwd):
    """조회·창 조작 전에 하는 로컬 검사. 통과하면 (편집기 실행파일, 그 세션 폴더를 연 창 핸들, 창 프로세스 id), 아니면 None."""
    exe = panel_editor(pid)
    if not exe or not user_away():
        return None
    win = panel_window(exe, cwd)
    return (exe, *win) if win else None


def panel_type(pid, sid, text, cwd, message):
    """확장 패널 세션 sid 의 입력창에 text 를 넣고 보낸다. message 는 그 세션을 멈춘 한도 안내 문구다.
    "skip": 아무것도 입력하지 않았다(다시 시도해도 된다). "typed": 글자는 넣었지만 Enter 직전 확인에 실패해 보내지 않았다.
    "sent": Enter 까지 보냈다. 글자를 넣은 뒤에는 결과와 상관없이 다시 시도하지 않는다."""
    marker = limit_marker(message)
    if (not marker or not re.fullmatch(r"[0-9A-Za-z_-]{1,128}", sid or "")
            or not 0 < len(text) <= 200 or any(ord(c) < 32 for c in text)):
        return "skip"
    ready = panel_precheck(pid, cwd)
    if not ready:
        return "skip"
    exe, hwnd, editor_pid = ready
    same = lambda a: (a or "").lower() == exe.lower()
    front = lambda: foreground_window() == (hwnd, exe) or (foreground_window()[0] == hwnd and same(foreground_window()[1]))
    tick = last_input_tick()
    ctrl = ctrl_enter_to_send(exe)  # 글자를 넣기 전에 읽어 둔다(넣은 뒤에는 실패할 수 있는 일을 하지 않는다)
    u32 = ctypes.windll.user32
    if not front():  # 그 폴더의 창을 먼저 앞으로: 편집기는 주소(URI)를 앞에 있는 창으로 넘긴다
        u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        for _ in range(12):
            time.sleep(0.25)
            if front():
                break
        else:
            return "skip"  # 윈도우가 앞으로 가져오게 두지 않았다. 다른 창에 세션을 열지 않게 여기서 그만둔다
    u32.AllowSetForegroundWindow(editor_pid)
    os.startfile(f"{EDITORS[Path(exe).name.lower()][0]}://anthropic.claude-code/open?session={sid}")  # 이미 열린 패널을 보여 준다
    time.sleep(2.5)  # 패널이 그려지고 입력창 포커스가 돌아올 때까지
    ui = focused_ui(marker)
    if (not ui or ui[1] != PANEL_INPUT_NAME or ui[2].strip() or not ui[3] or not same(process_exe(ui[0]))
            or last_input_tick() != tick or not front()):
        return "skip"  # 입력창이 아니거나, 쓰던 글이 있거나(못 읽으면 "\0"), 한도 안내가 보이지 않거나, 사용자가 돌아왔다
    try:  # 여기부터는 글자가 들어갔을 수 있다: 무슨 일이 있어도 "skip" 으로 돌아가 다시 시도하게 두지 않는다
        if not send_input(text_events(text)):
            return "typed"
        tick = last_input_tick()  # 방금 보낸 입력으로 바뀐 값
        time.sleep(0.4)
        ui = focused_ui()  # Enter 직전에 다시: 같은 입력창에 정확히 text 만 있어야 보낸다
        if (not ui or ui[1] != PANEL_INPUT_NAME or ui[2].strip() != text.strip() or not same(process_exe(ui[0]))
                or last_input_tick() != tick or not front()):
            return "typed"
        return "sent" if send_input(enter_events(ctrl)) else "typed"
    except Exception:
        return "typed"


def authorized_panel_resume(pid, text):
    """authorized_console_resume 의 확장 패널판: "skip" | "sent"(전송 확인) | "unverified"(입력했지만 확인 못 함).
    설정과 로컬 조건을 먼저 보고, 최신 한도를 다시 조회한 뒤, 보낸 뒤에는 대화 기록에서 리밋 에러가
    마지막 항목이 아니게 됐는지(실제로 전송됐는지) 확인한다."""
    cfg = load_config()
    if not (cfg["auto_resume"] and cfg["panel_resume"]) or text != cfg["resume_text"]:
        return "skip"
    for item in limited_claude_sessions(PANEL_ENTRY):
        if item[0] != pid:
            continue
        try:
            if not panel_precheck(pid, item[2]):  # 조회보다 로컬 검사를 먼저(조건이 안 되면 서버에 묻지 않는다)
                return "skip"
            d = provider_call("fetch", "claude")
            at = resume_at(item[3], item[4], d)
            if not at or time.time() < at + 30 or d["u5"] >= 100 or d["uw"] >= 100:
                return "skip"
            if item not in limited_claude_sessions(PANEL_ENTRY):
                return "skip"
            result = panel_type(pid, item[1], text, item[2], item[3])
        except Exception:
            return "skip"
        if result != "sent":
            return "unverified" if result == "typed" else "skip"
        for _ in range(15):
            time.sleep(2)
            if item not in limited_claude_sessions(PANEL_ENTRY):
                return "sent"
        return "unverified"
    return "skip"


# ── 조회 간격 ──────────────────────────────────────────────────────────
def next_poll(poll_sec, level, d, retry_after, now, idle_sec=300, steps=(120, 240, 480), hot_sec=120, hot_pct=80):
    """(다음 조회 시각, 429 단계). retry_after 가 None 이 아니면 429(요청 많음)를 받은 것이다.
    429 → 2·4·8분 뒤(서버가 더 기다리라면 그만큼, 최대 1시간). 성공하면 단계를 하나씩 내리며 그 단계 간격 아래로는
    당기지 않는다(8 → 4 → 2분 → 기본): 한 번 성공했다고 바로 짧은 간격으로 돌아가면 다시 막힌다.
    기본은 poll_sec(5분). 5시간·주간 사용률이 hot_pct 이상이면 숫자가 중요한 때라 hot_sec(2분)마다 본다.
    5시간 창이 시작 전('대기')이면 값이 바뀔 일이 없어 idle_sec 보다 자주 보지 않는다. 리셋 시각이 그 전에 오면 리셋 직후에 조회한다."""
    if retry_after is not None:
        return now + max(steps[min(level, len(steps) - 1)], min(retry_after, 3600)), level + 1
    wait = poll_sec
    live = bool(d) and d.get("src") == "live"
    if live and d.get("has5") is not False and not (d.get("r5") and d["r5"] > now):
        wait = max(poll_sec, idle_sec)
    if live and ((d.get("r5") or 0) > now and (d.get("u5") or 0) >= hot_pct or (d.get("rw") or 0) > now and (d.get("uw") or 0) >= hot_pct):
        wait = min(wait, hot_sec)
    if level > 0:
        wait = max(wait, steps[min(level - 1, len(steps) - 1)])
    due = now + wait
    for r in ((d or {}).get("r5"), (d or {}).get("rw")):
        if r and now < r + 5 < due:
            due = r + 5
    return due, max(0, level - 1)


def credit_daily(rows):
    """브로커가 준 날짜별 API 금액에서 ["YYYY-MM-DD", 달러] 모양만 남긴다(인사이트 비용 쪽에 쓴다)."""
    out = []
    for r in rows if isinstance(rows, list) else []:
        if (isinstance(r, list) and len(r) == 2 and isinstance(r[0], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0])
                and finite(r[1], 0, 1000000)):
            out.append([r[0], float(r[1])])
    return out[-62:]


def credit_cycle(day, now):
    """API 크레딧 결제 주기 (시작, 끝) epoch. 매달 day 일 0시(이 PC 시각)에 새 크레딧이 들어오고 남은 크레딧은 사라진다.
    그달에 그날이 없으면(31일 등) 그달 마지막 날로 본다. day 가 0 이면 None."""
    if not day:
        return None
    t = datetime.fromtimestamp(now)

    def on(year, month):
        last = calendar.monthrange(year, month)[1]
        return datetime(year, month, min(day, last))
    start = on(t.year, t.month)
    if start > t:
        start = on(t.year - (t.month == 1), 12 if t.month == 1 else t.month - 1)
    end = on(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1)
    return start.timestamp(), end.timestamp()


# ── shared state ────────────────────────────────────────────────────────
class Core:
    def __init__(self):
        self.cfg = load_config()
        self.hist = History()
        enabled = {s[0] for s in SERVICES if s[0] != "gemini" or self.cfg.get("gemini_enabled", False)} | {a["id"] for a in self.cfg.get("accounts", [])}
        # 첫 응답 전(또는 API가 막혔을 때)에는 마지막 기록 값을 보여 준다
        self.data = {k: {"u5": r["u5"], "r5": r["r5"], "uw": r["uw"], "rw": r["rw"], "plan": "", "src": "log", "observed_at": r["t"],
                         **({"has5": False} if r.get("has5") is False else {})}
                     for k, r in self.hist.last.items() if k in enabled}
        self.err = {}
        self.fetched_at = 0
        self.lock = threading.Lock()
        self.status = {}  # 서비스 장애 상태
        self.login = None  # 진행 중인 계정 추가 로그인 {"provider", "name", "dir", "started", "state", "msg"}
        self.cost = None  # 토큰·비용 집계 (인사이트 비용 탭)
        self.next_at = {}  # 서비스·계정별 다음 조회 시각 (next_poll)
        self.limited = {}  # 서비스·계정별 연속 429 횟수
        self.share_state = load_share_state()  # 환산 계수: 직접 맞춘 값·혼자 쓸 때 배운 값
        self.share_rows = {}  # 비용 집계(1시간마다)에서 온 Claude 응답 행
        self.share_recent = {}  # 최근 바뀐 Claude Code 기록 파일
        self.share_ready = False  # 비용 집계가 끝나기 전에는 주간 합계가 모자라 나누지 않는다
        self.share = {}  # share.summary 결과
        self.share_lock = threading.RLock()  # share_state·share·share_recent 를 바꿀 때만 잡는다(파일 읽기는 락 밖)
        self.share_scan_lock = threading.Lock()  # 기록 파일 읽기는 한 번에 하나
        self.work = {}  # 작업 계정: 서비스 키 → 기본 로그인 자리에 들어가 있는 추가 계정 id (없으면 원래 기본 계정)
        self.work_gen = 0  # 작업 계정을 바꿀 때마다 늘린다. 바꾸기 전에 시작한 조회 결과는 버린다
        self.switching = False
        # API 크레딧: 관리자 키가 저장돼 있는지(None = 아직 모름), 이번 주기에 쓴 금액·확인 시각·그 주기 시작, 오류
        self.update = None  # 새 버전("1.2.3")이 있으면
        self.credit = {"has_key": None, "spent": None, "daily": [], "observed_at": None, "since": None, "err": ""}

    # ── 작업 계정 ──
    def sync_work(self):
        """기본 로그인 자리에 어느 계정이 들어 있는지 브로커에 묻는다. 묻지 못한 서비스는 전에 알던 대로 둔다."""
        if MAC:
            return
        with self.lock:
            accounts = list(self.cfg.get("accounts", []))
            work = dict(self.work)
        for provider in ("claude", "gpt"):
            mine = [x for x in accounts if x["provider"] == provider]
            if not mine:
                work.pop(provider, None)
                continue
            try:
                path = provider_call("active", provider)
            except Exception:
                continue
            work[provider] = next((x["id"] for x in mine if path and Path(x["path"]) == Path(path)), None)
        work = {k: v for k, v in work.items() if v}
        with self.lock:
            if work != self.work:
                self.work = work
                self.work_gen += 1

    # ── 공유 사용량 나누기 ──
    def share_events(self, refresh=False):
        """이 PC 의 Claude Code 요청 [(시각, API 환산 USD, 모델 계열)]. refresh 면 지금 쓰는 기록 파일을 먼저 다시 읽는다."""
        if refresh:  # 파일을 읽는 동안 화면 상태 조회(state)가 기다리지 않게 락 밖에서 읽고 결과만 바꿔 끼운다
            with self.share_scan_lock:
                memo = scan_recent_claude(dict(self.share_recent))
                with self.share_lock:
                    self.share_recent = memo
        with self.share_lock:
            rows = dict(self.share_rows)
            for _, _, recent in self.share_recent.values():
                rows.update(recent)
        prices = self.cfg.get("prices")
        return share.weigh(rows.values(), lambda model: price_of(model, prices))

    def update_share(self):
        """켜져 있으면 Claude 사용률을 내 몫·다른 곳으로 다시 나눈다. Claude 조회가 성공할 때와 설정이 바뀔 때 부른다."""
        with self.lock:
            d = self.data.get("claude")
        setting = (self.cfg.get("shared_split"), self.cfg.get("share_plan"))
        if not setting[0] or not self.share_ready:
            summary = {}
        else:
            try:
                summary = share.summary(d, self.share_events(refresh=True), self.share_state, setting[1], time.time(),
                                        self.hist.svc_rows("claude"))
            except Exception:
                summary = {}  # 오래된 값을 계속 보이지 않는다
        with self.share_lock:
            if setting == (self.cfg.get("shared_split"), self.cfg.get("share_plan")):  # 계산하는 사이 설정이 바뀌었으면 버린다(바꾼 쪽이 다시 계산한다)
                self.share = summary

    def learn_share(self, old, new):
        """Claude 의 5시간·주간 창이 끝났다. 기능이 꺼져 있는 동안은 혼자 쓴다고 보고, 끝난 창의 마지막 사용률과
        그 창에서 내가 쓴 금액으로 환산 계수를 배운다."""
        now = time.time()
        if (self.cfg.get("shared_split") or not self.share_ready  # 비용 집계 전에는 주간 합계가 모자라 잘못 배운다
                or not old or old.get("src") != "live" or not new or new.get("src") != "live"):
            return
        plan = share.resolve_plan(self.cfg.get("share_plan"), old.get("plan"))
        ended = [(span, rk, uk) for span, rk, uk in (("h5", "r5", "u5"), ("week", "rw", "uw"))
                 if old.get(rk) and wkey(old[rk]) != wkey(new.get(rk)) and old[rk] <= now + 60]  # 리셋 시각이 흔들린 것은 같은 창
        if not ended:
            return
        events = self.share_events(refresh=True)
        with self.share_lock:
            state = self.share_state
            for span, rk, uk in ended:
                used, _ = share.spent(events, old[rk] - share.SPAN_SEC[span], min(old[rk], old.get("observed_at") or old[rk]))
                state = share.learn(state, plan, span, old[uk], used)
            if state is not self.share_state:
                self.share_state = state
                save_share_state(state)

    def calibrate_share(self, span):
        """'나만 썼어요': 지금 창(5시간 또는 주간)의 사용률과 내가 쓴 금액의 비율을 이 요금제의 환산 계수로 맞춘다. (받았는지, 안내 문구)"""
        now = time.time()
        with self.lock:
            d = self.data.get("claude")
        if not self.cfg.get("shared_split"):
            return False, "공유 사용량 나눠 보기를 켠 뒤에 맞출 수 있어요"
        if not self.share_ready:
            return False, "사용 기록을 읽는 중이에요 · 잠시 뒤 다시 눌러 주세요"
        rk, uk = {"h5": ("r5", "u5"), "week": ("rw", "uw")}.get(span, (None, None))
        if not rk or not d or d.get("src") != "live" or not d.get(rk) or d[rk] <= now:
            return False, "최신 사용량을 확인한 뒤 다시 눌러 주세요"
        plan = share.resolve_plan(self.cfg.get("share_plan"), d.get("plan"))
        used, _ = share.spent(self.share_events(refresh=True), d[rk] - share.SPAN_SEC[span], min(now, d.get("observed_at") or now))
        with self.share_lock:
            state, ok = share.calibrate(self.share_state, plan, span, d[uk], used)
            if ok:
                self.share_state = state
                save_share_state(state)
        if not ok:
            return False, "쓴 양이 적어서 맞출 수 없어요 · 더 쓴 뒤 눌러 주세요"
        self.update_share()
        return True, "내 사용량에 맞춰 계산을 고쳤어요"

    def reset_share_calibration(self):
        with self.share_lock:
            self.share_state = share.reset_manual(self.share_state)
            save_share_state(self.share_state)
        self.update_share()

    def status_loop(self):
        """장애 상태는 5분마다. 바뀌면 트레이도 갱신."""
        while True:
            if self.cfg.get("status_check", True):
                st = fetch_status()
                with self.lock:
                    self.status = st
                self.on_status()
            time.sleep(300)

    def on_status(self):
        pass

    def update_loop(self):
        """하루 한 번 새 버전이 있는지 본다(update_check 를 끄면 묻지 않는다). 실패하면 다음 날 다시 본다."""
        time.sleep(60)
        while True:
            if self.cfg.get("update_check", True):
                try:
                    found = fetch_update()
                except Exception:
                    found = None
                if found and found != self.update:
                    self.update = found
                    self.on_update()
            time.sleep(UPDATE_POLL)

    def on_update(self):
        pass

    def _fetch_credit(self, force=False):
        """API 크레딧: 결제일을 정했고 관리자 키가 저장돼 있으면 CREDIT_POLL 마다 이번 주기에 쓴 금액을 브로커에 묻는다.
        429 는 next_poll 로 늦춘다. 키가 있는지는 처음 한 번(그리고 키를 바꿀 때) 묻는다."""
        now = time.time()
        with self.lock:
            cycle = credit_cycle(self.cfg.get("credit_day", 0), now)
            has_key = self.credit["has_key"]
        if has_key is None:  # 저장 여부는 결제일과 상관없이 처음 한 번 묻는다(설정 칸에 보인다)
            try:
                has_key = provider_call("credit_key", "claude")["saved"] is True
            except Exception:
                has_key = None  # 다음 조회 때 다시 묻는다
            with self.lock:
                self.credit["has_key"] = has_key
        with self.lock:
            if not cycle or not has_key:
                self.next_at.pop("api_credit", None)
                return
            if not (now >= self.next_at.get("api_credit", 0) or force and not self.limited.get("api_credit")):
                return
        value = retry = error = None
        try:
            value = provider_call("credit", "claude", value=int(cycle[0]))
        except RateLimited as ex:
            retry = ex.retry_after or 0
        except Exception as ex:
            error = str(ex)[:120] if type(ex) is RuntimeError else "API 크레딧 조회 실패"
        spent = value.get("spent") if isinstance(value, dict) else None
        daily = credit_daily(value.get("daily") if isinstance(value, dict) else None)
        with self.lock:
            self.next_at["api_credit"], self.limited["api_credit"] = next_poll(
                CREDIT_POLL, self.limited.get("api_credit", 0), None, retry, time.time())
            if finite(spent, 0, 1000000):
                self.credit.update(spent=float(spent), daily=daily, observed_at=time.time(), since=int(cycle[0]), err="")
            elif retry is not None:
                self.credit["err"] = f"요청 많음 · {hm(self.next_at['api_credit'])}에 다시 조회"
            else:
                self.credit["err"] = error or "API 크레딧 조회 실패"

    def credit_view(self, now=None):
        """화면에 보낼 API 크레딧. 결제일을 정하지 않았으면 None. 쓴 금액은 지금 주기의 값일 때만 보낸다."""
        now = time.time() if now is None else now
        with self.lock:
            day, amount, c = self.cfg.get("credit_day", 0), self.cfg.get("credit_amount", 100), dict(self.credit)
        cycle = credit_cycle(day, now)
        if not cycle:
            return None
        current = c["since"] == int(cycle[0])
        return {"day": day, "amount": amount, "start": cycle[0], "end": cycle[1], "has_key": c["has_key"],
                "spent": c["spent"] if current else None, "observed_at": c["observed_at"] if current else None,
                "daily": c.get("daily") or [] if current else [],
                "err": c["err"] if c["has_key"] else ""}

    def _credit_alert(self, now):
        """API 크레딧이 사라지기 3일 전·하루 전에 한 번씩 알릴 문구(없으면 None). 남은 금액을 알면 함께, 5달러 미만이면 알리지 않는다."""
        v = self.credit_view(now)
        if not v:
            return None
        left = v["end"] - now
        step = "1d" if left <= 86400 else "3d" if left <= 3 * 86400 else None
        if not step or ("credit", v["end"], step) in self.alerted:
            return None
        self.alerted.add(("credit", v["end"], step))
        when = "내일" if step == "1d" else f"{max(1, round(left / 86400))}일 뒤"
        if v["spent"] is None:
            return f"API 크레딧이 {when} 새로 들어와요 · 남은 크레딧은 사라져요"
        rest = max(0.0, v["amount"] - v["spent"])
        return f"API 크레딧 ${rest:.0f} 남음 · {when} 사라져요" if rest >= 5 else None

    def _fetch_all(self, force=False):
        """조회할 때가 된 서비스·계정만 조회한다. force(새로고침)는 '대기'로 늦춘 조회도 바로 하지만,
        429 로 기다리는 중인 곳은 기다린다(다시 요청하면 더 오래 막힌다)."""
        now = time.time()
        with self.lock:
            accounts = list(self.cfg.get("accounts", []))
            due = lambda key: now >= self.next_at.get(key, 0) or force and not self.limited.get(key)
            work, gen = dict(getattr(self, "work", {})), getattr(self, "work_gen", 0)
        # 작업 계정을 바꾸면 그 계정의 로그인은 기본 자리에, 원래 기본 계정의 로그인은 그 계정 폴더에 있다
        paths = {a["id"]: a["path"] for a in accounts}
        services = [(key, name, (lambda key=key: provider_call("fetch", key, paths[work[key]])) if work.get(key) in paths else fn)
                    for key, name, fn in SERVICES]
        extra = [(a["id"], a["name"], lambda a=a: provider_call(
                     "fetch", a["provider"], None if work.get(a["provider"]) == a["id"] else a["path"]))
                 for a in accounts if a.get("provider") in provider_meta.BY_KEY]
        jobs = [(key, fn) for key, _, fn in services + extra
                if (key != "gemini" or self.cfg.get("gemini_enabled", False)) and due(key)]
        if not jobs:
            return
        claude_change = None  # (이전 값, 새 값): 창이 끝났는지 보고 환산 계수를 배운다
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {pool.submit(fn): key for key, fn in jobs}
            for task in as_completed(pending):
                key = pending[task]
                d = retry = error = None
                try:
                    raw = task.result()
                    retry = raw.get("retry_after") if isinstance(raw, dict) else None
                    d = usage_values(raw)
                    d["observed_at"] = d.get("observed_at", time.time() if d.get("src") == "live" else None)
                except RateLimited as ex:
                    retry = ex.retry_after or 0
                except Exception as ex:
                    error = str(ex)[:120] if type(ex) is RuntimeError else "조회 실패 · 로그인 또는 네트워크를 확인하세요"
                done = time.time()
                with self.lock:
                    if key == "gemini" and not self.cfg.get("gemini_enabled", False):
                        continue  # 조회 중 꺼진 서비스의 응답을 되살리지 않는다.
                    if key not in {s[0] for s in SERVICES} and not any(a["id"] == key for a in self.cfg.get("accounts", [])):
                        continue  # 조회하는 사이에 연결 해제한 계정
                    self.next_at[key], self.limited[key] = next_poll(
                        self.cfg.get("poll_sec", 300), self.limited.get(key, 0), d, retry, done)
                    if getattr(self, "work_gen", 0) != gen or getattr(self, "switching", False):
                        continue  # 조회하는 사이에 작업 계정을 바꿨다(바꾸는 중이다): 다른 계정의 값이 이 카드에 들어갈 수 있다
                    if d is not None:
                        before = self.data.get(key)
                        self.data[key] = d
                        self.err.pop(key, None)
                        if d.get("src") == "live":
                            self.fetched_at = done
                            self.hist.add(key, d)
                            if key == "claude":
                                claude_change = (before, d)
                    if retry is not None:
                        self.err[key] = f"요청 많음 · {hm(self.next_at[key])}에 다시 조회"
                    elif error is not None:
                        self.err[key] = error
        if claude_change:
            try:
                self.learn_share(*claude_change)
                self.update_share()
            except Exception:
                pass


# ── web widget (pywebview + WebView2) ───────────────────────────────────
WEB_DIR = APP_ROOT / "web"
WEB_BG = {"dark": "#0d1117", "light": "#ffffff"}  # 페이지가 뜨기 전 창 배경 (GitHub canvas)


_PAGE_URLS = {}


def web_page(name):
    """페이지 주소. 페이지마다 로컬 서버를 한 번만 띄워 다시 쓴다(인사이트 창을 열 때마다 늘지 않게)."""
    if name not in _PAGE_URLS:
        _PAGE_URLS[name] = serve_local(page_app(WEB_DIR, name, allow_eval=MAC))
    return _PAGE_URLS[name]


def system_light():
    if MAC:  # 다크 모드면 AppleInterfaceStyle=Dark, 라이트면 키가 없다
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=5)
            return "dark" not in out.stdout.lower()
        except Exception:
            return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return False


def web_bg(theme):
    if theme == "auto":
        theme = "light" if system_light() else "dark"
    return WEB_BG[theme]


class Api:
    """web/*.html 에서 window.pywebview.api 로 부르는 함수들."""

    def __init__(self, app):
        self._app = app
        self._selected_files = set()

    def _expose(self, win, insights=False):
        # Do not hand the object graph to the bridge: dispatch only these functions.
        names = ("insights", "theme", "detail_size", "fit_insights", "ins_minimize", "ins_close") if insights else (
            "state", "refresh", "choose_account_file", "add_account", "login_options", "start_login",
            "cancel_login", "remove_account", "rename_account", "move_account", "switch_account", "set_gemini_enabled", "fit", "set_compact", "set_compact_hidden", "toggle_topmost",
            "toggle_startup", "theme", "set_theme", "open_insights", "toggle_shared_split", "set_share_plan",
            "calibrate_share", "reset_share_calibration", "toggle_remote_view", "toggle_cycle_start", "set_cycle_times", "toggle_boot_start",
            "set_credit", "set_credit_key", "welcome", "finish_welcome", "open_update", "quit")
        win.expose(*(getattr(self, n) for n in names))

    def state(self):
        return self._app.state()

    def insights(self):
        a = self._app
        with a.lock:
            data = dict(a.data)
        out = insights_data(a.hist, data, time.time())
        out["cost"] = a.cost
        out["api_credit"] = a.credit_view()  # 실제 API 크레딧 사용(관리자 키가 있을 때 날짜별 금액)
        if a.cfg.get("shared_split") and a.share_ready:  # Claude 5시간 창별 내 몫·다른 곳
            out["share"] = share.past_windows(a.hist.svc_rows("claude"), a.share_events(), a.share_state,
                                              a.cfg.get("share_plan"), (data.get("claude") or {}).get("plan"), time.time())
        return out

    def refresh(self):
        now = time.monotonic()
        if now - getattr(self, "_refresh_at", 0) < 10:
            return
        self._refresh_at = now
        self._app.wake.set()

    def choose_account_file(self):
        import webview
        paths = self._app.win.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                                 file_types=("JSON (*.json)",))
        if not paths:
            return None
        path = str(Path(paths[0]).resolve())
        self._selected_files.add(path)
        return path

    def add_account(self, provider, name, path):
        if not isinstance(path, str) or len(path) > 4096:
            return {"error": "로그인 파일을 선택하세요"}
        try:
            selected = str(Path(path).expanduser().resolve(strict=True))
        except (OSError, ValueError):
            return {"error": "로그인 파일을 선택하세요"}
        if selected not in self._selected_files:
            return {"error": "파일 선택 창이나 찾은 계정 목록에서 선택하세요"}
        return self._add_account(provider, name, selected)

    def _add_account(self, provider, name, path):
        """로그인 파일을 계정으로 연결한다. 토큰은 저장하지 않고 파일 경로·이름·이메일만 저장한다."""
        if not isinstance(provider, str) or not isinstance(name, str) or len(name) > 254:
            return {"error": "계정 정보를 확인하세요"}
        p = provider_meta.BY_KEY.get(provider)
        if not p or not getattr(p, "LOGIN", None):
            return {"error": "Claude 또는 Codex를 선택하세요"}
        try:
            file = Path(path).expanduser().resolve(strict=True)
            info = provider_call("inspect", provider, str(file))
            if not info["valid"]:
                raise ValueError()
        except Exception:
            return {"error": "이 서비스의 로그인 파일이 아니에요"}
        email = info["email"]
        name = str(name).strip()[:40] or email or f"{p.NAME} 계정"
        try:  # 브로커에 묻는 동안 공용 잠금을 쥐지 않는다(조회가 밀려 있으면 화면이 멈춘다)
            default_email = provider_call("default_email", provider) if email else None
        except RuntimeError:
            default_email = None
        a = self._app
        with a.lock:
            accounts = list(a.cfg.get("accounts", []))
            if len(accounts) >= 20:
                return {"error": "추가 계정은 최대 20개까지 연결할 수 있어요"}
            same = [x for x in accounts if x["provider"] == provider]
            if (file == p.DEFAULT_FILE.resolve() or any(Path(x["path"]) == file for x in same)
                    or email and (email == default_email or any(x.get("email") == email for x in same))):
                return {"error": f"이미 연결된 계정이에요{f' ({email})' if email else ''}. 다른 계정으로 로그인하세요"}
            accounts.append({"id": "account_" + uuid.uuid4().hex, "provider": provider,
                             "name": name, "path": str(file), **({"email": email} if email else {})})
            a.cfg["accounts"] = accounts
            if not save_config(a.cfg):
                a.cfg["accounts"] = accounts[:-1]
                return {"error": "설정을 저장하지 못했습니다. 저장 폴더 권한을 확인하세요"}
        a.wake.set()
        return {"ok": True}

    def login_options(self):
        """계정 추가 화면: 로그인으로 추가할 수 있는 서비스와, 이 PC에서 찾았지만 아직 연결 안 된 로그인."""
        with self._app.lock:
            linked = {str(Path(x["path"]).resolve()) for x in self._app.cfg.get("accounts", [])}
        opts, found = [], []
        for p in provider_meta.ALL:
            if not getattr(p, "LOGIN", None):
                continue
            ok, why = login_supported(p)
            opts.append({"key": p.KEY, "name": p.NAME, "ok": ok, "why": why})
            try:
                candidates = provider_call("profiles", p.KEY)
                default_email = provider_call("default_email", p.KEY)
            except RuntimeError:
                candidates, default_email = [], None
            for item in candidates:
                f = Path(item["path"])
                if str(f) not in linked and item.get("email") != default_email:
                    self._selected_files.add(str(f))
                    found.append({"provider": p.KEY, "name": p.NAME, "path": str(f),
                                  "email": item.get("email") or "", "folder": "~/" + f.parent.name})
        return {"providers": opts, "found": found}

    def open_update(self):
        """새 버전 받는 곳(GitHub 릴리스 페이지, 고정 주소)을 브라우저로 연다."""
        import webbrowser
        webbrowser.open(RELEASES_URL)

    def welcome(self):
        """처음 실행 안내: 이 PC의 기본 로그인(서비스별 이메일·있는지)과 더 찾은 로그인, 시작 시 실행 상태."""
        base = []
        for p in provider_meta.ALL:
            if not getattr(p, "LOGIN", None):
                continue
            try:
                email = provider_call("default_email", p.KEY)
            except RuntimeError:
                email = None
            found = bool(email) or bool(p.DEFAULT_FILE and p.DEFAULT_FILE.exists())
            base.append({"key": p.KEY, "name": p.NAME, "email": email or "", "found": found})
        return {"base": base, "found": self.login_options()["found"],
                "startup": startup_enabled(), "packaged": PACKAGED, "mac": MAC}

    def finish_welcome(self, choice):
        """처음 실행 안내를 마친다. choice 가 None 이면 건너뛰기(아무것도 바꾸지 않는다).
        {"accounts": [{"provider", "path"}], "gemini": bool, "startup": bool, "alerts": bool} 이면 고른 대로 설정한다."""
        a = self._app
        failed = []
        with a.lock:
            if a.cfg.get("onboarded", True):  # 이미 마쳤다: 다시 적용하지 않는다
                return {"ok": True, "failed": []}
        if choice is not None:
            if (not isinstance(choice, dict) or not isinstance(choice.get("accounts"), list) or len(choice["accounts"]) > 20
                    or any(type(choice.get(k)) is not bool for k in ("gemini", "startup", "alerts"))):
                return {"error": "설정 값을 확인하세요"}
            for item in choice["accounts"]:
                ok = isinstance(item, dict) and isinstance(item.get("path"), str)
                r = self.add_account(item.get("provider"), "", item["path"]) if ok else {"error": "계정"}
                if r.get("error"):
                    failed.append(r["error"])
            if choice["gemini"]:
                r = self.set_gemini_enabled(True)
                if r and r.get("error"):
                    failed.append(r["error"])
            if choice["startup"] and not PACKAGED and not startup_enabled():
                try:
                    toggle_startup()
                except OSError:
                    failed.append("시작 시 실행을 켜지 못했어요")
            with a.lock:
                a.cfg["reset_alert"] = choice["alerts"]
        with a.lock:
            a.cfg["onboarded"] = True
            saved = save_config(a.cfg)
        if a.tray:  # 계정 연결·Gemini 켜기는 각자 조회를 깨운다. 나머지는 조회가 필요 없다
            on_main(a.tray.update_menu)
        if not saved:
            failed.append("설정을 저장하지 못했어요")
        return {"ok": True, "failed": failed[:5]}

    def start_login(self, provider, name):
        """새 설정 폴더로 공식 로그인을 띄운다. 로그인 파일이 생기면 WebWidget.login_loop 가 연결한다."""
        if not isinstance(provider, str) or not isinstance(name, str) or len(name) > 80:
            return {"error": "계정 정보를 확인하세요"}
        p = provider_meta.BY_KEY.get(provider)
        ok, why = login_supported(p) if p else (False, "서비스를 선택하세요")
        if not ok:
            return {"error": why}
        a = self._app
        with a.lock:  # 확인과 자리 잡기를 한 번에: 버튼을 연달아 눌러도 숨긴 로그인이 두 개 뜨지 않는다
            if a.login and a.login["state"] == "waiting":
                return {"error": "진행 중인 로그인을 완료하거나 취소하세요"}
            folder = new_profile_dir(p, str(name).strip())
            entry = a.login = {"provider": provider, "name": str(name).strip()[:40], "dir": str(folder),
                               "started": time.time(), "state": "waiting", "proc": None,
                               "msg": "브라우저 로그인 대기 중"}
        try:
            folder.mkdir(parents=True)
            proc = launch_login(p, folder)
        except Exception as ex:
            with a.lock:
                if a.login is entry:
                    a.login = None
            return {"error": f"로그인 창을 열지 못했어요: {str(ex)[:60]}"}
        with a.lock:
            mine = a.login is entry
            if mine:
                entry["proc"] = proc
        if not mine:  # 띄우는 사이에 취소했다
            stop_login(proc)
        return {"ok": True}

    def cancel_login(self):
        with self._app.lock:
            lg, self._app.login = self._app.login, None
        stop_login(lg and lg.get("proc"))
        return {"ok": True}

    def rename_account(self, account_id, name):
        """표시 이름만 바꾼다. 연결 경로·이메일·사용량 기록은 그대로 둔다. 기본 서비스 카드의 이름은 names 에 둔다."""
        if not isinstance(account_id, str) or not isinstance(name, str):
            return {"error": "계정 정보를 확인하세요"}
        name = name.strip()
        if not name or len(name) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            return {"error": "이름은 1~40자로 입력하세요. 줄바꿈은 사용할 수 없어요"}
        a = self._app
        with a.lock:
            if account_id in provider_meta.BY_KEY and account_id in card_order(a.cfg):
                previous = a.cfg.get("names", {})
                a.cfg["names"] = dict(previous, **{account_id: name})
                if not save_config(a.cfg):
                    a.cfg["names"] = previous
                    return {"error": "이름 변경을 저장하지 못했습니다"}
                return {"ok": True}
            previous = a.cfg.get("accounts", [])
            if not any(x["id"] == account_id for x in previous):
                return {"error": "연결된 계정을 찾지 못했어요"}
            a.cfg["accounts"] = [dict(x, name=name) if x["id"] == account_id else x for x in previous]
            if not save_config(a.cfg):
                a.cfg["accounts"] = previous
                return {"error": "이름 변경을 저장하지 못했습니다"}
        return {"ok": True}

    def move_account(self, key, step):
        """카드 순서를 한 칸 옮긴다(step -1 앞, 1 뒤). 순서만 저장하고 계정 정보·사용량 기록은 그대로 둔다."""
        if not isinstance(key, str) or type(step) is not int or step not in (-1, 1):
            return {"error": "계정 정보를 확인하세요"}
        a = self._app
        with a.lock:
            order = card_order(a.cfg)
            if key not in order:
                return {"error": "연결된 계정을 찾지 못했어요"}
            i = order.index(key)
            if not 0 <= i + step < len(order):
                return {"ok": True}
            order[i], order[i + step] = order[i + step], order[i]
            previous = a.cfg.get("order", [])
            a.cfg["order"] = order
            if not save_config(a.cfg):
                a.cfg["order"] = previous
                return {"error": "순서 변경을 저장하지 못했습니다"}
        return {"ok": True}

    def set_gemini_enabled(self, enabled):
        """Gemini는 사용자가 켠 뒤에만 브로커로 조회한다."""
        if type(enabled) is not bool:
            return {"error": "활성화 여부를 확인하세요"}
        a = self._app
        with a.lock:
            previous = a.cfg.get("gemini_enabled", False)
            if previous == enabled:
                return {"ok": True}
            hidden = a.cfg.get("compact_hidden", [])
            a.cfg["gemini_enabled"] = enabled
            shown = [k for k in hidden if k != "gemini"] if enabled else list(hidden)
            # Gemini만 표시하던 사용자가 꺼도 빈 위젯이 되지 않게 한다.
            keys = ["claude", "gpt"] + [x["id"] for x in a.cfg.get("accounts", [])]
            if not enabled and all(k in shown for k in keys):
                shown.remove("claude")
            a.cfg["compact_hidden"] = shown
            if not save_config(a.cfg):
                a.cfg["gemini_enabled"] = previous
                a.cfg["compact_hidden"] = hidden
                return {"error": "서비스 설정을 저장하지 못했습니다"}
            if enabled and not a.limited.get("gemini"):
                a.next_at.pop("gemini", None)
            if not enabled:
                a.data.pop("gemini", None)
                a.err.pop("gemini", None)
        a.wake.set()
        if callable(getattr(a, "on_status", None)):
            a.on_status()
        return {"ok": True}

    def switch_account(self, key):
        """작업 계정 바꾸기: 그냥 실행한 claude·codex 가 쓸 계정을 고른다. 브로커가 기본 로그인 자리와 그 계정 폴더의
        로그인을 맞바꾼다(토큰은 화면 프로세스로 오지 않는다). 카드·기록은 계정을 따라간다."""
        if not isinstance(key, str):
            return {"error": "계정 정보를 확인하세요"}
        if MAC:
            return {"error": "작업 계정 바꾸기는 아직 윈도우만 지원해요"}
        a = self._app
        with a.lock:
            account = next((x for x in a.cfg.get("accounts", []) if x["id"] == key), None)
            provider = account["provider"] if account else key
            if provider not in ("claude", "gpt") or not account and key not in card_order(a.cfg):
                return {"error": "Claude·Codex 계정만 작업 계정으로 고를 수 있어요"}
            if (a.work.get(provider) or provider) == key:
                return {"ok": True}
            if a.switching:
                return {"error": "계정을 바꾸는 중이에요"}
            if time.monotonic() - getattr(self, "_switch_at", -60) < 10:  # 연달아 바꿔 조회가 몰리지 않게
                return {"error": "잠시 뒤 다시 바꿔 주세요"}
            self._switch_at = time.monotonic()
            a.switching = True
            a.work_gen += 1  # 지금 진행 중인 조회 결과는 버린다
        error = None
        try:  # 브로커가 잠금을 기다리는 동안 공용 잠금을 쥐지 않는다
            provider_call("switch", provider, account["path"] if account else None)
        except RuntimeError as ex:
            error = str(ex)
        finally:
            a.sync_work()
            with a.lock:
                a.switching = False
                a.work_gen += 1
                for k in [provider] + [x["id"] for x in a.cfg.get("accounts", []) if x["provider"] == provider]:
                    if not a.limited.get(k):  # 429 로 기다리는 곳은 기다린다
                        a.next_at.pop(k, None)
            a.wake.set()
        if error:
            return {"error": error}
        name = provider_meta.BY_KEY[provider].NAME
        return {"ok": True, "msg": "열려 있는 Claude Code도 다음 메시지부터 이 계정을 써요" if provider == "claude"
                else f"{name}를 다시 실행하면 이 계정을 써요"}

    def remove_account(self, account_id):
        if not isinstance(account_id, str):
            return {"error": "계정 정보를 확인하세요"}
        a = self._app
        with a.lock:
            previous = a.cfg.get("accounts", [])
            if not any(x["id"] == account_id for x in previous):
                return {"error": "연결된 계정을 찾지 못했어요"}
            if getattr(a, "switching", False):
                return {"error": "계정을 바꾸는 중이에요 · 끝난 뒤 삭제하세요"}
            if account_id in getattr(a, "work", {}).values():  # 이 계정 폴더에 원래 기본 계정의 로그인이 맡겨져 있다
                return {"error": "작업 계정은 삭제할 수 없어요 · 다른 계정을 작업 계정으로 고른 뒤 삭제하세요"}
            hidden = a.cfg.get("compact_hidden", [])
            a.cfg["accounts"] = [x for x in previous if x["id"] != account_id]
            a.cfg["compact_hidden"] = [k for k in hidden if k != account_id]
            if not save_config(a.cfg):
                a.cfg["accounts"] = previous
                a.cfg["compact_hidden"] = hidden
                return {"error": "계정 삭제를 저장하지 못했습니다"}
            a.data.pop(account_id, None)
            a.err.pop(account_id, None)
            a.next_at.pop(account_id, None)
            a.limited.pop(account_id, None)
        return {"ok": True}

    def fit(self, w, h):
        """창 크기를 내용에 맞춘다. 간단히 보기 ↔ 자세히 보기를 바꿔도 위쪽 가장자리는 그대로 두고,
        화면 오른쪽 절반에 있으면 오른쪽 가장자리를 기준으로 늘고 준다."""
        if not finite(w, 60, 2000) or not finite(h, 40, 4000):
            return
        from webview.window import FixPoint
        win = self._app.win
        if not self._app.cfg["compact"] and w >= 280:  # 자세히 보기 크기 — 인사이트 창이 따라 한다
            self._app.detail_size = (int(w), int(h))
        if not MAC:
            return self._app.place(w, h)
        x, y, cw, ch = win.x, win.y, win.width, win.height
        s = next((s for s in self._app.webview.screens
                  if s.x <= x + cw / 2 < s.x + s.width and s.y <= y + ch / 2 < s.y + s.height),
                 self._app.webview.screens[0])
        fp = (FixPoint.EAST if x + cw / 2 > s.x + s.width / 2 else FixPoint.WEST) | FixPoint.NORTH
        win.resize(int(w), int(h), fix_point=fp)

    def set_compact(self, v, at_cursor=False):
        """at_cursor: 더블클릭으로 바꿀 때 그 자리(마우스)에 창을 띄운다(윈도우).
        간단히 보기는 창의 가운데, 자세히 보기는 가로 가운데·첫 카드 사이클 줄 바로 아래가 그 자리에 온다."""
        a = self._app
        a.cfg["compact"] = bool(v)
        save_config(a.cfg)
        if at_cursor and not MAC:
            a._center_at = (*cursor_monitor(), time.time())
            # 창 크기 요청(fit)이 이 호출보다 먼저 왔을 수도 있어 잠시 뒤 한 번 더 맞춘다
            if a._last_fit:
                threading.Timer(0.3, lambda: a.place(*a._last_fit)).start()

    def set_compact_hidden(self, keys):
        """간단히 보기에서 숨길 서비스·계정 (자세히 보기 이름표 토글)."""
        if not isinstance(keys, list):
            return
        self._app.cfg["compact_hidden"] = [k for k in keys[:20] if isinstance(k, str) and len(k) <= 80]
        save_config(self._app.cfg)

    def toggle_topmost(self):
        a = self._app
        a.cfg["topmost"] = not a.cfg["topmost"]
        a.win.on_top = a.cfg["topmost"]
        save_config(a.cfg)
        return a.cfg["topmost"]

    def toggle_startup(self):
        return toggle_startup()

    def toggle_shared_split(self):
        """공유 사용량 나눠 보기 켜기/끄기. 켜면 기록을 읽어 나누느라 시간이 걸릴 수 있어 뒤에서 계산한다."""
        a = self._app
        a.cfg["shared_split"] = not a.cfg.get("shared_split", False)
        save_config(a.cfg)
        threading.Thread(target=a.update_share, daemon=True).start()
        if a.tray:
            on_main(a.tray.update_menu)
        return a.cfg["shared_split"]

    def set_share_plan(self, plan):
        """요금제 단계. 서버는 Max 5배와 20배를 구분해 알려 주지 않는다."""
        if plan not in ("auto", "pro", "max5", "max20"):
            return
        self._app.cfg["share_plan"] = plan
        save_config(self._app.cfg)
        threading.Thread(target=self._app.update_share, daemon=True).start()

    def calibrate_share(self, span):
        """'나만 썼어요': 지금 창을 나 혼자 쓴 것으로 보고 환산 계수를 맞춘다. {"ok": 받았는지, "msg": 안내 문구}"""
        ok, msg = self._app.calibrate_share(span if span in ("h5", "week") else None)
        return {"ok": ok, "msg": msg}

    def reset_share_calibration(self):
        self._app.reset_share_calibration()

    def toggle_remote_view(self):
        """폰에서 보기 켜기/끄기. {"on": 켜졌는지, "url": 폰에서 열 주소, "qr": 그 주소의 QR 코드(행마다 0·1 문자열), "msg": 주소가 없을 때 안내 문구}"""
        a = self._app
        a.cfg["remote_view"] = not a.cfg.get("remote_view", False)
        save_config(a.cfg)
        if a.tray:
            on_main(a.tray.update_menu)
        return a.sync_remote()

    def toggle_cycle_start(self):
        """사이클 예약 켜기/끄기."""
        a = self._app
        a.cfg["cycle_start"] = not a.cfg.get("cycle_start", False)
        save_config(a.cfg)
        if a.tray:
            on_main(a.tray.update_menu)
        return a.cfg["cycle_start"]

    def toggle_boot_start(self):
        """켤 때 Claude 시작 켜기/끄기."""
        a = self._app
        a.cfg["boot_start"] = not a.cfg.get("boot_start", False)
        save_config(a.cfg)
        if a.tray:
            on_main(a.tray.update_menu)
        return a.cfg["boot_start"]

    def set_credit(self, day, amount):
        """API 크레딧 결제일(1~31, 0 = 끔)과 한 달 크레딧(100 · 200달러). 바뀐 주기는 바로 다시 조회한다."""
        a = self._app
        if type(day) is not int or not 0 <= day <= 31 or amount not in (100, 200) or type(amount) is not int:
            return {"error": "결제일(1~31)과 크레딧 금액을 확인하세요"}
        with a.lock:
            changed = a.cfg.get("credit_day") != day  # 금액만 바꾸면 조회할 것이 같다
            a.cfg["credit_day"], a.cfg["credit_amount"] = day, amount
            if changed and not a.limited.get("api_credit"):  # 429 로 기다리는 중이면 기다린다
                a.next_at.pop("api_credit", None)
        save_config(a.cfg)
        if changed:
            a.wake.set()
        return a.credit_view()

    def set_credit_key(self, key):
        """관리자 키 저장("" 이면 지우기). 키는 브로커가 자격 증명 저장소에 두고, 이 프로세스에는 남기지 않는다."""
        a = self._app
        if not isinstance(key, str) or len(key) > 256:
            return {"error": "관리자 키를 확인하세요"}
        key = re.sub(r"[\s\"'`]", "", key)  # 붙여 넣을 때 섞인 공백·줄바꿈·따옴표
        try:
            saved = provider_call("credit_key", "claude", value=key)["saved"] is True
        except Exception as ex:
            return {"error": str(ex)[:120] if type(ex) in (RuntimeError, ValueError) else "관리자 키를 저장하지 못했어요"}
        with a.lock:
            a.credit.update(has_key=saved, spent=None, observed_at=None, since=None, err="")
            if not a.limited.get("api_credit"):  # 새 키는 바로 확인하되, 429 로 기다리는 중이면 기다린다
                a.next_at.pop("api_credit", None)
        a.wake.set()
        return a.credit_view()

    def set_cycle_times(self, text):
        """예약 시각("8:00, 13:30" 같은 글이나 목록). 올바른 시각만 4개까지 받고, 저장한 목록을 돌려준다."""
        self._app.cfg["cycle_times"] = cycle_times(text)
        save_config(self._app.cfg)
        return self._app.cfg["cycle_times"]

    def theme(self):
        return self._app.cfg["theme"]

    def set_theme(self, t):
        self._app.set_theme(t)

    def open_insights(self):
        self._app.open_insights()

    def detail_size(self):
        return self._app.detail_size

    def fit_insights(self, w, h):
        """인사이트 창 크기. 제목 표시줄이 없어 창 크기 = 내용 크기."""
        if self._app.ins_win and finite(w, 60, 2000) and finite(h, 40, 4000):
            self._app.place_insights(w, h)  # 크기가 바뀌어도 위젯 옆·화면 안에 머물게

    def ins_minimize(self):
        if self._app.ins_win:
            self._app.ins_win.minimize()

    def ins_close(self):
        if self._app.ins_win:
            self._app.ins_win.destroy()

    def quit(self):
        self.cancel_login()  # 숨겨 둔 로그인 프로세스를 남기지 않는다
        if self._app.tray:
            on_main(self._app.tray.stop)
        for w in list(self._app.webview.windows):
            w.destroy()


class WebWidget(Core):
    def __init__(self, webview):
        super().__init__()
        self.webview = webview
        self.wake = threading.Event()
        self.boot_until = time.time() + BOOT_WINDOW  # 켤 때 Claude 시작을 할 수 있는 마지막 시각(깨어날 때 다시 연다)
        self.boot_done = False
        self.cycle_fired = set()  # 다룬 사이클 예약 (날짜, "HH:MM"): 보냈거나, 창이 돌고 있었거나, 놓쳐서 알렸다
        self.api = Api(self)
        self.win = None
        self.ins_win = self.ins_hwnd = None
        self.detail_size = None
        self._save_timer = None
        self._top_timer = None  # 윈도우: 앞 창이 바뀐 뒤 가려졌는지 확인할 예약
        self.tray = None
        self.hidden = False
        self.alerted = set()
        self.hwnd = None  # 윈도우: 위젯 창 핸들 (place 가 실제 픽셀로 배치)
        self._placed = None  # place 가 마지막으로 놓은 창 자리 [l, t, r, b]: 이 자리로 온 이동은 사용자가 옮긴 게 아니다
        self._center_at = None  # 더블클릭한 자리: (마우스 x, y, 작업 영역, 배율, 시각)
        self._last_fit = None
        self.remote = None  # 폰에서 보기 서버: (서버, Tailscale 주소, 폰에서 열 주소, 주소의 QR 코드)
        self.remote_msg = ""  # 켰는데 주소가 없을 때 안내 문구
        self.remote_lock = threading.Lock()

    def state(self):
        with self.lock:
            data, err = dict(self.data), dict(self.err)
            accounts = [dict(a) for a in self.cfg.get("accounts", [])]
            order, names = card_order(self.cfg), dict(self.cfg.get("names", {}))
        svc = {}
        for key in [s[0] for s in SERVICES] + [a["id"] for a in accounts]:
            if key == "gemini" and not self.cfg.get("gemini_enabled", False):
                continue
            d = data.get(key)
            if not d:
                continue
            k = wkey(d["r5"])
            hist = [[r["t"], r["u5"]] for r in self.hist.svc_rows(key) if k is not None and r["r5"] == k]
            # 오늘(기준 시각 이후) 실제로 시작된 5시간 창의 시작 시각
            day_start = day_end(datetime.now(), self.cfg["day_reset_hour"]).timestamp() - 86400
            starts = [s for s, _ in windows(self.hist, key) if s >= day_start]
            svc[key] = dict(d, hist=hist, starts=starts)
            day0 = day_start_usage(self.hist.svc_rows(key), d.get("rw"), day_start)
            if day0 is not None:  # 오늘 쓸 몫: 기준 시각의 주간 사용률에서 출발한다
                svc[key]["day0"] = day0
            if key == "claude" and self.cfg.get("shared_split"):
                with self.share_lock:
                    if self.share:
                        svc[key]["share"] = dict(self.share)
        with self.lock:
            status = dict(self.status)
            login = {k: self.login[k] for k in ("provider", "state", "msg")} if self.login else None
        remote = self.remote
        with self.lock:
            work = {p: self.work.get(p) or p for p in ("claude", "gpt")}  # 서비스 → 기본 로그인 자리에 있는 카드
        return {"svc": svc, "err": err, "fetched_at": self.fetched_at, "status": status, "credit": self.credit_view(), "update": self.update,
                "accounts": [{k: a.get(k, "") for k in ("id", "name", "provider", "email")} for a in accounts],
                "order": order, "names": names, "login": login, "work": work,
                "cfg": {"compact": self.cfg["compact"], "topmost": self.cfg["topmost"],
                        "startup": startup_enabled(), "packaged": PACKAGED, "east": self.anchored_east(),
                        "stale_after": self.stale_after(),
                        "day_reset_hour": self.cfg["day_reset_hour"],
                        "theme": self.cfg["theme"], "mac": MAC, "compact_hidden": self.cfg["compact_hidden"],
                        "gemini_enabled": self.cfg.get("gemini_enabled", False),
                        "onboarded": self.cfg.get("onboarded", True),
                        "shared_split": self.cfg.get("shared_split", False), "share_plan": self.cfg.get("share_plan", "auto"),
                        "remote_view": self.cfg.get("remote_view", False), "remote_url": remote[2] if remote else "",
                        "remote_qr": remote[3] if remote else [], "remote_msg": self.remote_msg,
                        "cycle_start": self.cfg.get("cycle_start", False), "cycle_times": list(self.cfg.get("cycle_times", [])),
                        "boot_start": self.cfg.get("boot_start", False),
                        "credit_day": self.cfg.get("credit_day", 0), "credit_amount": self.cfg.get("credit_amount", 100),
                        "credit_key": self.credit["has_key"]}}

    # ── 사이클 예약 ──
    def run_cycle(self, fired, now):
        """예약 시각이 됐고 Claude 의 5시간 창이 쉬고 있으면 한 마디를 보내 그때 시작하게 한다.
        최신 값으로 창이 돌고 있거나 주간 한도가 찬 것을 알면 보내지 않는다(값을 모르면 보낸다).
        예약 하나당 하루 한 번만 다루고, 때가 된 예약이 여럿이어도 한 번만 보낸다. 실패해도 다시 보내지 않는다."""
        due = cycle_due(self.cfg.get("cycle_times", []), now, fired)
        if not due:
            return
        fired.update(due)
        with self.lock:  # 공식 CLI 는 기본 로그인 자리의 계정(작업 계정)으로 보낸다
            d = dict(self.data.get(getattr(self, "work", {}).get("claude") or "claude") or {})
        ts = now.timestamp()
        if d.get("has5") is False:  # 5시간 창이 없는 계정(Max 등): 시작할 창이 없으니 사용량만 쓰게 된다
            return
        if d.get("src") == "live" and ((d.get("r5") or 0) > ts or d.get("uw", 0) >= 100 and (d.get("rw") or 0) > ts):
            return
        if start_claude_window():
            self.wake.set()  # 새 창의 리셋 시각을 바로 받아 온다
            self.tray_notify(f"예약한 {due[0][1]}에 Claude 5시간 창을 시작했어요")
        else:
            self.tray_notify("예약한 시각에 Claude 를 시작하지 못했어요 · Claude Code 설치와 로그인을 확인해 주세요")

    def tip_missed_cycles(self, since, now):
        """PC 가 꺼져 있는 동안 지나간 사이클 예약이 있으면, PC 없이 도는 클라우드 루틴을 권한다(하루 예약마다 한 번)."""
        if not self.cfg.get("cycle_start"):
            return
        missed = [k for k in cycle_missed(self.cfg.get("cycle_times", []), since, now) if k not in self.cycle_fired]
        if missed:
            self.cycle_fired.update(missed)
            at = missed[-1][1]
            self.tray_notify(f"PC 가 꺼져 있어 {at} 사이클 예약을 놓쳤어요 · " + CLOUD_TIP.format(at=at))

    def woke(self, since, now):
        """잠자기·최대 절전에서 깨어났다: 켤 때 Claude 시작을 한 번 더 할 수 있게 하고, 놓친 예약을 알리고, 바로 조회한다."""
        self.boot_until, self.boot_done = now.timestamp() + BOOT_WINDOW, False
        self.tip_missed_cycles(since, now)
        self.wake.set()

    def boot_start(self):
        """켤 때 Claude 시작: 켜거나 깨운 뒤 BOOT_WINDOW 안에 Claude 토큰이 만료돼 있으면 공식 CLI 로 한 마디를 보내
        토큰을 갱신하고 5시간 창을 연다(만료 토큰으로는 조회하지 않아 그대로면 Claude Code 를 켤 때까지 지난 값만 보인다).
        켤 때(깨울 때)마다 한 번만. 보내기 시작했으면 True."""
        with self.lock:
            err = self.err.get(getattr(self, "work", {}).get("claude") or "claude") or ""  # 작업 계정의 카드
        if (not self.cfg.get("boot_start") or self.boot_done or time.time() > self.boot_until
                or not err.startswith(provider_meta.TOKEN_EXPIRED)):
            return False
        self.boot_done = True
        threading.Thread(target=self._boot_send, daemon=True).start()
        return True

    def _boot_send(self):
        if start_claude_window():
            self.wake.set()  # 갱신된 토큰으로 바로 조회한다
            self.tray_notify("Claude 를 시작해 토큰을 갱신했어요")  # 5시간 창이 없는 계정도 있어 창 시작은 말하지 않는다
        else:
            self.tray_notify("켤 때 Claude 를 시작하지 못했어요 · Claude Code 설치와 로그인을 확인해 주세요")

    def cycle_loop(self):
        fired = self.cycle_fired
        with self.lock:  # 위젯이 마지막으로 깨어 있던 때: 지난 기록의 시각(없으면 오늘 0시)
            seen = [d.get("observed_at") or 0 for d in self.data.values()]
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        last = max([today] + [datetime.fromtimestamp(t) for t in seen if t])
        first = True
        while True:
            time.sleep(20)
            now = datetime.now()
            try:
                if first:  # 트레이가 뜬 뒤에 알린다
                    self.tip_missed_cycles(last, now)
                elif (now - last).total_seconds() > CYCLE_GRACE:  # 20초마다 도는데 크게 건너뛰었다 = 잠들었다 깨어났다
                    self.woke(last, now)
            except Exception:
                pass
            first, last = False, now
            if self.cfg.get("cycle_start"):
                try:
                    self.run_cycle(fired, datetime.now())
                except Exception:
                    pass
                if len(fired) > 40:  # 지난 날 기록은 버린다
                    today = datetime.now().strftime("%Y-%m-%d")
                    fired.intersection_update({k for k in fired if k[0] == today})

    # ── 폰에서 보기 ──
    def remote_state(self):
        """폰에서 보기로 내보내는 값. 화면에 보이는 카드와 같은 순서·이름이고, remote_values 가 이름·숫자·오류 문구만 남긴다.
        위젯이 이미 가진 값만 쓰고 새로 조회하지 않는다. 폰이 자주 물어도 5초에 한 번만 다시 만든다."""
        now = time.monotonic()
        cached = getattr(self, "_remote_cache", None)
        if cached and now - cached[0] < 5:
            return cached[1]
        st = self.state()
        base = {p.KEY: p.NAME for p in provider_meta.ALL}
        # 이름을 비우고 연결한 계정은 이메일이 이름이다. 이메일은 내보내지 않고 서비스 이름으로 바꾼다
        public = lambda name, kind: base.get(kind, "") if "@" in name else name
        cards = {k: (public(st["names"].get(k) or n, k), k) for k, n in base.items()}
        cards.update({a["id"]: (public(a["name"], a["provider"]), a["provider"]) for a in st["accounts"]})
        out = remote_values([(k, *cards[k]) for k in st["order"] if k in cards], st["svc"], st["err"], st["cfg"]["stale_after"],
                            st["cfg"].get("day_reset_hour", 6))
        self._remote_cache = (now, out)
        return out

    def sync_remote(self):
        """설정과 Tailscale 연결에 맞춰 폰에서 보기 서버를 띄우거나 내린다. 서버는 이 PC 의 Tailscale 주소에만 붙는다."""
        with self.remote_lock:
            on = self.cfg.get("remote_view", False)
            ip = tailnet_ip() if on else None
            if self.remote and self.remote[1] != ip:  # 껐거나 Tailscale 이 끊겼거나 주소가 바뀌었다
                self.remote[0].shutdown()
                self.remote[0].server_close()
                self.remote = None
            msg = ""
            if on and not ip:
                msg = "Tailscale 에 연결하면 주소가 나와요"
            elif on and not self.remote:
                if not REMOTE_TOKEN.fullmatch(self.cfg.get("remote_token", "")):
                    self.cfg["remote_token"] = new_remote_token()
                    save_config(self.cfg)
                token = self.cfg["remote_token"]
                try:
                    app = remote_app(WEB_DIR, f"{ip}:{REMOTE_PORT}", token, self.remote_state)
                    url = f"http://{ip}:{REMOTE_PORT}/{token}/"
                    code = qr.rows(url)  # 서버를 띄우기 전에 만든다(실패해도 서버가 남지 않게)
                    self.remote = (serve_remote(app, ip, REMOTE_PORT), ip, url, code)
                except OSError:
                    msg = f"{REMOTE_PORT}번 포트를 다른 프로그램이 쓰고 있어요"
            self.remote_msg = msg
            return {"on": on, "url": self.remote[2] if self.remote else "", "qr": self.remote[3] if self.remote else [], "msg": msg}

    def remote_loop(self):
        """Tailscale 이 위젯보다 늦게 켜지거나 다시 연결돼도 따라가게 30초마다 맞춘다."""
        while True:
            try:
                self.sync_remote()
            except Exception:
                pass
            time.sleep(30)

    def stale_after(self):
        """실시간 값을 이 초보다 오래 확인하지 못하면 '이전 기록'으로 본다. 화면·트레이·리셋 알림이 같은 기준을 쓴다."""
        return max(180, self.cfg["poll_sec"] * 2)

    def anchored_east(self):
        """창이 오른쪽 가장자리를 기준으로 늘고 주는지. 메뉴를 띄우느라 창이 커져도 내용이 제자리에 있게 화면이 쓴다."""
        if not MAC:  # place 와 같은 기준. 처음 실행은 화면 오른쪽에 뜬다
            return (self.cfg.get("anchor") or ["east"])[0] == "east"
        try:  # Api.fit 과 같은 기준: 창이 있는 화면의 오른쪽 절반이면 오른쪽 기준
            win = self.win
            s = next((s for s in self.webview.screens
                      if s.x <= win.x + win.width / 2 < s.x + s.width and s.y <= win.y + win.height / 2 < s.y + s.height),
                     self.webview.screens[0])
            return win.x + win.width / 2 > s.x + s.width / 2
        except Exception:
            return True

    def poll_loop(self):
        """서비스·계정마다 정한 다음 조회 시각(next_poll)에 깬다. 새로고침·리셋 알림·계정 추가는 wake 로 바로 깨운다."""
        force, synced = True, 0
        while True:
            if time.monotonic() - synced >= 300:  # 작업 계정: 켤 때와 5분마다(시간 초과로 끝난 전환도 여기서 맞춘다)
                self.sync_work()
                synced = time.monotonic()
            self._fetch_all(force)
            self._fetch_credit(force)
            self.update_tray()
            self.boot_start()
            with self.lock:
                due = min(self.next_at.values(), default=time.time() + self.cfg["poll_sec"])
            force = self.wake.wait(max(1, min(due - time.time(), 3600)))
            self.wake.clear()

    def login_loop(self):
        """계정 추가 로그인이 끝나 새 폴더에 로그인 파일이 생기면 계정으로 연결한다."""
        while True:
            time.sleep(2)
            with self.lock:
                lg = dict(self.login) if self.login and self.login["state"] == "waiting" else None
            if not lg:
                continue
            p = provider_meta.BY_KEY[lg["provider"]]
            file = Path(lg["dir"]) / p.LOGIN["file"]
            proc = lg.get("proc")
            gone = proc is not None and proc.poll() is not None  # 파일보다 먼저 본다: 끝난 뒤에 파일이 없으면 실패다
            try:
                ready = file.exists() and provider_call("inspect", lg["provider"], str(file))["valid"]
            except Exception:
                ready = False  # 쓰는 중일 수 있다
            if ready:
                try:  # 이메일 정보(.claude.json) 저장과 브라우저 완료 화면까지 CLI 가 스스로 끝내게 둔다
                    proc.wait(timeout=5) if proc else time.sleep(1)
                except Exception:
                    pass
                with self.lock:
                    if not (self.login and self.login["dir"] == lg["dir"]):
                        continue  # 그사이에 취소했다
                r = self.api._add_account(lg["provider"], lg["name"], str(file))
                state, msg = ("done", "연결됨") if r.get("ok") else ("error", r["error"])
            elif gone:
                state, msg = "error", "로그인 창이 닫혔어요"
            elif time.time() - lg["started"] > LOGIN_TIMEOUT:
                state, msg = "error", "로그인 시간 초과"
            else:
                continue
            stop_login(proc)
            with self.lock:
                if self.login and self.login["dir"] == lg["dir"]:
                    self.login.update(state=state, msg=msg)

    def import_loop(self):
        """지난 기록 가져오기와 토큰·비용 집계: 시작할 때 한 번, 이후 1시간마다 새로 생긴 로그만."""
        while True:
            try:
                import_past(self.hist)
            except Exception:
                pass
            try:
                cache = scan_costs()
                self.cost = cost_summary(cache, self.cfg.get("prices"))
                with self.share_lock:
                    self.share_rows = claude_rows(cache)
                    self.share_ready = True
                self.update_share()
            except Exception:
                pass
            time.sleep(3600)

    def on_moved(self, x, y):
        if MAC:
            self.cfg["wx"], self.cfg["wy"] = x, y
        else:
            if not self.hwnd:
                return
            (l, t, r, b), (ml, _, mr, _), _ = win_geometry(self.hwnd)
            # 위젯이 직접 놓은 자리는 기준점으로 삼지 않는다(재시작마다 옆으로 밀리지 않게).
            # 시간으로 가리지 않고 자리로 가린다: 크기가 바뀐 직후에 끌어도 그 이동을 놓치지 않는다
            if [l, t, r, b] == self._placed:
                return
            self._center_at = None  # 사용자가 옮겼다: 더블클릭한 자리로 되돌리지 않는다
            east = (l + r) / 2 > (ml + mr) / 2
            self.cfg["anchor"] = ["east" if east else "west", r if east else l, t]
            self.cfg["wx"], self.cfg["wy"] = x, y  # 다음 실행 때 창을 처음 띄울 자리(이후 place 가 맞춘다)
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.6, save_config, (self.cfg,))
        self._save_timer.start()

    def _insights_spot(self, w, h):
        """처음 띄울 때 줄 대략의 자리(pywebview 좌표). 정확한 자리는 창이 뜬 뒤 place_insights 가 맞춘다."""
        try:
            if MAC:
                x = self.win.x - w - 8 if self.win.x >= w + 8 else self.win.x + self.win.width + 8
                return int(x), int(self.win.y)
            if self.hwnd:
                rect, work, scale = win_geometry(self.hwnd)
                x, y = beside(rect, work, w * scale, h * scale, 8 * scale)
                return int(x / scale), int(y / scale)
        except Exception:
            pass
        return None, None

    def place_insights(self, w=None, h=None):
        """윈도우: 인사이트 창을 위젯 옆에 붙인다(실제 픽셀). w·h 를 주면 그 크기(CSS px)로 바꾸면서 옮긴다."""
        if MAC or not self.ins_win or not self.hwnd:
            if self.ins_win and w and h:
                self.ins_win.resize(int(w), int(h))
            return
        self.ins_hwnd = getattr(self, "ins_hwnd", None) or find_hwnd("AI 사용 인사이트")
        if not self.ins_hwnd:
            return
        (il, it, ir, ib), _, scale = win_geometry(self.ins_hwnd)
        pw, ph = (int(w * scale), int(h * scale)) if w and h else (ir - il, ib - it)
        rect, work, _ = win_geometry(self.hwnd)
        x, y = beside(rect, work, pw, ph, int(8 * scale))
        set_win_rect(self.ins_hwnd, x, y, pw, ph)

    def open_insights(self):
        if self.ins_win is not None:
            try:
                self.ins_win.restore()
                self.ins_win.show()
                self.place_insights()  # 위젯을 옮긴 뒤 다시 열어도 위젯 옆에
                return
            except Exception:
                self.ins_win = None
        x, y = self._insights_spot(300, 560)
        self.ins_hwnd = None
        self.ins_win = self.webview.create_window(
            "AI 사용 인사이트", web_page("insights.html"), x=x, y=y,
            width=300, height=560, resizable=False, frameless=True, easy_drag=False, shadow=True,
            background_color=web_bg(self.cfg["theme"]), on_top=self.cfg["topmost"])
        self.api._expose(self.ins_win, insights=True)
        if not MAC:
            protect_window(self.ins_win)
        self.ins_win.events.closed += self._ins_closed
        if not MAC:  # 제목 표시줄은 페이지가 그린다
            self.ins_win.events.shown += lambda: (round_corners("AI 사용 인사이트"), self.place_insights())

    def _ins_closed(self):
        self.ins_win = self.ins_hwnd = None

    # ── 알림 영역(작업 표시줄 오른쪽 아래) 아이콘 ──
    def start_tray(self):
        try:
            import pystray
            from PIL import Image
        except ImportError:  # pip install pystray 전에는 트레이 없이
            return
        M = pystray.MenuItem
        menu = pystray.Menu(
            M(lambda _: "위젯 보이기" if self.hidden else "위젯 숨기기",
              lambda: self.toggle_visible(), default=True),
            M("새로고침", lambda: self.api.refresh()),
            M("인사이트·예측", lambda: self.open_insights()),
            M(lambda _: f"새 버전 {self.update} 받기", lambda: self.api.open_update(), visible=lambda _: bool(self.update)),
            pystray.Menu.SEPARATOR,
            M("항상 위", lambda: self.api.toggle_topmost(), checked=lambda _: self.cfg["topmost"]),
            M("로그인 시 실행" if MAC else "시작 앱 설정 열기" if PACKAGED else "윈도우 시작 시 실행",
              lambda: toggle_startup(), checked=None if PACKAGED else lambda _: STARTUP_FILE.exists()),
            M("리셋 알림음", lambda: self.toggle_reset_alert(), checked=lambda _: self.cfg["reset_alert"]),
            M("Claude 공유 사용량 나눠 보기", lambda: self.api.toggle_shared_split(), checked=lambda _: self.cfg.get("shared_split", False)),
            M("폰에서 보기 (Tailscale)", lambda: self.api.toggle_remote_view(), checked=lambda _: self.cfg.get("remote_view", False)),
            M("사이클 예약 (정한 시각에 Claude 5시간 창 시작)", lambda: self.api.toggle_cycle_start(), checked=lambda _: self.cfg.get("cycle_start", False)),
            M("켤 때 Claude 시작 (토큰이 만료돼 있으면 한 마디 보내 갱신)", lambda: self.api.toggle_boot_start(), checked=lambda _: self.cfg.get("boot_start", False)),
            *([M("메뉴 막대에 사용률 표시", lambda: self.toggle_menubar_text(),
                 checked=lambda _: self.cfg["menubar_text"])] if MAC else []),
            *([] if MAC else [M(f"리밋 풀리면 Claude 이어 가기 (\"{self.cfg['resume_text']}\")",
                                lambda: self.toggle_auto_resume(), checked=lambda _: self.cfg["auto_resume"]),
                              M("  └ VS Code 패널도 (자리 비웠을 때, 실험)", lambda: self.toggle_panel_resume(),
                                checked=lambda _: self.cfg["panel_resume"], enabled=lambda _: self.cfg["auto_resume"])]),
            M("테마", pystray.Menu(*(
                M(label, (lambda v: lambda: self.set_theme(v))(v),
                  checked=(lambda v: lambda _: self.cfg["theme"] == v)(v), radio=True)
                for v, label in (("auto", "자동 (시스템 설정)"), ("light", "라이트"), ("dark", "다크"))))),
            pystray.Menu.SEPARATOR,
            M("종료", lambda: self.api.quit()),
        )
        self.tray = pystray.Icon("QuotaTank", Image.open(WEB_DIR / "icon.ico"), "QuotaTank", menu)
        self._tray_key = None
        self.tray.run_detached()  # 맥은 pywebview 가 돌리는 NSApplication 루프에 얹힌다

    def toggle_visible(self):
        self.hidden = not self.hidden
        if self.hidden:
            self.win.hide()
        else:
            self.win.show()
        on_main(self.tray.update_menu)

    def set_theme(self, t):
        if t in ("auto", "light", "dark"):
            self.cfg["theme"] = t
            save_config(self.cfg)
            if self.tray:
                on_main(self.tray.update_menu)

    def toggle_reset_alert(self):
        self.cfg["reset_alert"] = not self.cfg["reset_alert"]
        save_config(self.cfg)
        if self.cfg["reset_alert"]:
            play_reset_sound()

    def reset_alert_loop(self):
        """한도를 reset_alert_min% 이상 쓴 창의 리셋 시각이 지나면 알림음 + 알림.
        최신 값으로 확인한 창을 기억해 두므로, 리셋 무렵 조회가 실패해 값이 오래돼도 알림이 빠지지 않는다."""
        names = {k: n for k, n, _ in SERVICES}
        watch = {}  # (서비스, "5시간"|"주간") → (리셋 시각, 마지막으로 확인한 사용률)
        while True:
            time.sleep(2)
            note = self._credit_alert(time.time())
            if note:
                self.tray_notify(note)
            fired = self._due_resets(watch, names, time.time())
            if fired:
                self.wake.set()  # 새 사이클 수치로 바로 새로고침
                if self.cfg["reset_alert"]:
                    play_reset_sound()
                    self.tray_notify("\n".join(fired))

    def _due_resets(self, watch, names, now):
        with self.lock:
            data = {k: d for k, d in self.data.items() if k != "gemini" or self.cfg.get("gemini_enabled", False)}
            accounts = {**self.cfg.get("names", {}), **{a["id"]: a["name"] for a in self.cfg.get("accounts", [])}}
        fired = []
        for (key, label), (r, u) in list(watch.items()):
            if now < r:
                continue
            del watch[(key, label)]
            if key not in data or (key, r) in self.alerted:  # 연결 해제한 계정은 알리지 않는다
                continue
            self.alerted.add((key, r))
            if u >= self.cfg["reset_alert_min"]:
                fired.append(f"{accounts.get(key, names.get(key, key))} {label} 한도가 풀렸어요 (쓴 양 {u:.0f}%)")
        for key, d in data.items():
            if d.get("src") != "live" or not d.get("observed_at") or now - d["observed_at"] > self.stale_after():
                continue
            for label, u, r in (("5시간", d["u5"], d["r5"]), ("주간", d["uw"], d["rw"])):
                if r and (key, r) not in self.alerted:
                    watch[(key, label)] = (r, u)
        return fired

    def toggle_auto_resume(self):
        self.cfg["auto_resume"] = not self.cfg["auto_resume"]
        save_config(self.cfg)

    def toggle_panel_resume(self):
        self.cfg["panel_resume"] = not self.cfg["panel_resume"]
        save_config(self.cfg)

    def resume_loop(self):
        """리밋 에러로 멈춘 Claude Code 터미널 세션(VS Code 터미널 포함)에, 리밋이 풀리는 시각에 맞춰 resume_text 를
        입력해 이어 가게 한다. 보조 프로세스를 RESUME_LEAD 초 먼저 띄워 리셋 시각까지 기다리게 하므로 리셋 1~3초 안에 들어간다.
        Claude Code 가 스스로 이어 갔으면 마지막 항목이 에러가 아니라 건너뛴다.
        VS Code 확장 패널 세션(panel_resume)은 콘솔이 없어 키 입력으로 보내므로, 자리를 비웠을 때만 5초마다 따로 본다."""
        done, attempts, due, live, scan_at, d = set(), {}, {}, {}, 0, {}
        while True:
            time.sleep(.5)
            if not self.cfg["auto_resume"]:
                continue
            now = time.time()
            scan = now >= scan_at
            if scan:  # 멈춘 세션은 5초마다 찾고, 입력할 시각은 0.5초마다 본다
                scan_at = now + 5
                with self.lock:  # 멈춘 세션은 기본 로그인 자리의 계정(작업 계정)으로 돈다
                    d = dict(self.data.get(getattr(self, "work", {}).get("claude") or "claude") or {})
                live = {}
                for pid, sid, cwd, text, ts in limited_claude_sessions():
                    at = resume_at(text, ts, d)
                    if not at:
                        continue
                    # 보조 프로세스를 띄울 때까지는 최신 값으로 고치고, 그 뒤로는 기억한 시각을 쓴다
                    # (리셋 뒤에는 에러 문구의 분 단위 시각만 남는다)
                    if (sid, ts) not in due or now < due[(sid, ts)] - RESUME_LEAD:
                        due[(sid, ts)] = at
                    live[(sid, ts)] = (pid, cwd, due[(sid, ts)])
            jobs = []
            for key, (pid, cwd, at) in live.items():
                tries, retry_at = attempts.get(key, (0, 0))
                if key in done or tries >= 3 or now < max(at - RESUME_LEAD, retry_at):
                    continue
                attempts[key] = (tries + 1, now + (RESUME_LEAD + 15 if not tries else 120))
                jobs.append((key, pid, cwd, at, tries))
            sent = []
            if jobs:
                text = self.cfg["resume_text"]
                with ThreadPoolExecutor(max_workers=4) as pool:  # 멈춘 세션이 여럿이어도 같은 시각에 넣는다
                    results = list(pool.map(lambda j: type_into_console(j[1], text, j[3] + RESUME_DELAY), jobs))
                for (key, pid, cwd, at, tries), ok in zip(jobs, results):
                    if ok:
                        done.add(key)
                        sent.append(Path(cwd).name or cwd)
                    elif tries == 2:
                        self.tray_notify("Claude 이어가기 실패 · 터미널에서 직접 계속해 주세요")
            # 서버가 요청을 줄이라고 한 동안(429)은 확인 조회를 보내지 않게 기다린다. 풀린 지 오래된 한도 에러는 건드리지 않는다
            if scan and self.cfg.get("panel_resume") and not MAC and not self.limited.get("claude"):
                for pid, sid, cwd, text, ts in limited_claude_sessions(PANEL_ENTRY):
                    at = resume_at(text, ts, d)
                    if (sid, ts) in done or not at or now < at + 30 or now > at + PANEL_MAX_AGE:
                        continue
                    if not user_away():
                        continue  # 자리에 있거나 발표·전체 화면 중이면 기다린다(시도 횟수에 넣지 않는다)
                    tries, retry_at = attempts.get(("panel", sid, ts), (0, 0))
                    if tries >= 4 or now < retry_at:
                        continue
                    attempts[("panel", sid, ts)] = (tries + 1, now + 60)
                    result = authorized_panel_resume(pid, self.cfg["resume_text"])
                    if result == "sent":
                        done.add((sid, ts))
                        sent.append((Path(cwd).name or cwd) + " (VS Code)")
                    elif result == "unverified":  # 글자를 넣은 뒤에는 다시 시도하지 않는다(다른 곳에 두 번 들어가지 않게)
                        done.add((sid, ts))
                        self.tray_notify("VS Code 패널에 입력했지만 전송을 확인하지 못했어요 · 패널을 확인해 주세요")
                    elif tries == 3:
                        self.tray_notify("VS Code 패널 이어 가기 실패 · 패널에서 직접 계속해 주세요")
            if sent:
                self.tray_notify("리밋이 풀려 Claude 를 이어 갔어요: " + ", ".join(sent))

    def on_update(self):
        self.tray_notify(f"새 버전 {self.update} 이 나왔어요 · 메뉴에서 받을 수 있어요")
        if self.tray:
            on_main(self.tray.update_menu)

    def tray_notify(self, msg):
        if MAC:
            return mac_notify(msg)
        if not self.tray:
            return
        try:  # 소리는 직접 내므로 윈도우 기본 알림음은 끈다(NIIF_NOSOUND)
            from pystray._util import win32
            self.tray._message(win32.NIM_MODIFY, win32.NIF_INFO, szInfo=msg[:255],
                               szInfoTitle="QuotaTank", dwInfoFlags=0x10)
        except Exception:
            self.tray.notify(msg, "QuotaTank")

    def toggle_menubar_text(self):
        self.cfg["menubar_text"] = not self.cfg["menubar_text"]
        save_config(self.cfg)
        self._tray_key = None
        self.update_tray()

    def on_status(self):
        self.update_tray()

    def update_tray(self):
        """아이콘 = 서비스별 5시간 막대, 툴팁 = 사용률·장애. 맥은 메뉴 막대에 숫자도 쓴다."""
        if not self.tray:
            return
        with self.lock:
            data, status, errors = dict(self.data), dict(self.status), dict(self.err)
        now = time.time()
        lines, levels = ["QuotaTank"], []
        for key, name, _ in SERVICES:
            if key == "gemini" and not self.cfg.get("gemini_enabled", False):
                levels.append(None)
                continue
            d = data.get(key)
            if not d:
                levels.append(None)
                if key in errors:
                    lines.append(f"{name} · 조회 실패")
                continue
            if key in errors or d.get("src") != "live" or not d.get("observed_at") or now - d["observed_at"] > self.stale_after():
                levels.append(None)
                lines.append(f"{name} · 최신 사용량 확인 필요")
                continue
            if d.get("has5") is False:
                levels.append(None)
                lines.append(f"{name} · 5시간 한도 없음 · 주 {d['uw']:.0f}%")
                continue
            u5 = d["u5"] if d["r5"] and d["r5"] > now else 0
            uw = d["uw"] if d["rw"] and d["rw"] > now else 0
            levels.append(u5)
            bad = (status.get(key) or {}).get("level", "none") not in ("none", "maintenance")
            lines.append(f"{name} {u5:.0f}% · 주 {uw:.0f}%" + (" · 장애" if bad else ""))
        on_main(setattr, self.tray, "title", "\n".join(lines)[:127])
        key = (tuple(None if u is None else round(u) for u in levels), MAC and self.cfg["menubar_text"])
        if key == self._tray_key:  # 같은 그림이면 아이콘을 다시 만들지 않는다(깜빡임 방지)
            return
        self._tray_key = key
        try:
            on_main(setattr, self.tray, "icon", tray_image(levels))
        except Exception:
            pass
        if MAC:
            on_main(self.set_menubar_title, levels if self.cfg["menubar_text"] else None)

    def set_menubar_title(self, levels):
        """맥 메뉴 막대: 'C 4%  X 18%  G 0%' — 글자는 서비스 색, 숫자는 시스템 글자색. None 이면 아이콘만."""
        try:
            import AppKit
            btn = self.tray._status_item.button()
            if not levels or all(u is None for u in levels):
                btn.setTitle_("")
                btn.setImagePosition_(AppKit.NSImageOnly)
                return
            font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(12, AppKit.NSFontWeightMedium)
            title = AppKit.NSMutableAttributedString.alloc().init()
            for (letter, hexc), u in zip((("C", "#d97757"), ("X", "#10a37f"), ("G", "#4c8bf5")), levels):
                if u is None:
                    continue
                r, g, b = (int(hexc[i:i + 2], 16) / 255 for i in (1, 3, 5))
                for text, color in ((("  " if title.length() else "") + letter + " ",
                                     AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1)),
                                    (f"{u:.0f}%", AppKit.NSColor.labelColor())):
                    title.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        text, {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color}))
            btn.setAttributedTitle_(title)
            btn.setImagePosition_(AppKit.NSNoImage)  # 숫자가 막대 아이콘을 대신한다
        except Exception:
            pass

    def place(self, w, h):
        """윈도우: 내용 크기(CSS px) w×h 로 창을 맞춘다. 기준점 = 사용자가 마지막으로 둔 위쪽 모서리.
        자세히 보기가 화면 아래로 넘치면 그때만 위로 올리고, 기준점은 바꾸지 않는다."""
        self._last_fit = (w, h)
        self.hwnd = self.hwnd or find_hwnd("QuotaTank")
        if not self.hwnd:
            return
        (l, t, r, b), (ml, mt, mr, mb), scale = win_geometry(self.hwnd)
        mx, my = cursor_monitor()[:2] if mouse_down() else (None, None)
        if mx is not None and l <= mx <= r and t <= my <= b:
            # 사용자가 창을 끌고 있다: 자리는 건드리지 않고 크기만 맞춘다(끄는 중에 기준점으로 되돌아가지 않게)
            self._center_at = None
            pw, ph = round(w * scale), round(h * scale)
            self.cfg["anchor"] = ["east" if l + pw / 2 > (ml + mr) / 2 else "west",
                                  l + pw if l + pw / 2 > (ml + mr) / 2 else l, t]
            if [l, t, l + pw, t + ph] != [l, t, r, b]:
                self._placed = [l, t, l + pw, t + ph]
                set_win_rect(self.hwnd, l, t, pw, ph)
            return
        c = self._center_at
        if c and time.time() - c[4] < 1.5:
            # 더블클릭한 자리에: 간단히 보기는 창 가운데, 자세히 보기는 위쪽 가장자리 가운데.
            # 그 자리가 새 기준점이 된다. 화면 밖으로 나가면 안쪽으로 붙인다.
            # 보기가 바뀌는 동안 크기 요청이 여러 번 오므로 그동안은 매번 같은 자리에 다시 맞춘다
            # (처음 온 중간 크기로 기준점을 굳히면 마지막 크기에서 자리가 어긋난다)
            cx, cy, (ml, mt, mr, mb), scale = c[:4]
            pw, ph = round(w * scale), round(h * scale)
            x = min(max(cx - pw // 2, ml), mr - pw)
            # 자세히 보기는 클릭한 자리가 첫 카드의 사이클 줄 바로 아래(창 위에서 44 CSS px)에 오게
            y = cy - ph // 2 if self.cfg["compact"] else cy - round(DETAIL_CLICK_Y * scale)
            y = min(max(y, mt), max(mt, mb - ph))
            east = x + pw / 2 > (ml + mr) / 2
            anchor = ["east" if east else "west", x + pw if east else x, y]
            if self.cfg.get("anchor") != anchor:
                self.cfg["anchor"] = anchor
                save_config(self.cfg)
            if [x, y, x + pw, y + ph] != [l, t, r, b]:
                self._placed = [x, y, x + pw, y + ph]
                set_win_rect(self.hwnd, x, y, pw, ph)
            return
        pw, ph = round(w * scale), round(h * scale)
        side, ax, ay = self.cfg.get("anchor") or ["east" if (l + r) / 2 > (ml + mr) / 2 else "west",
                                                  r if (l + r) / 2 > (ml + mr) / 2 else l, t]
        x = ax - pw if side == "east" else ax
        x = min(max(x, ml), mr - pw)
        y = min(max(ay, mt), max(mt, mb - ph))
        if [x, y, x + pw, y + ph] != [l, t, r, b]:
            self._placed = [x, y, x + pw, y + ph]
            set_win_rect(self.hwnd, x, y, pw, ph)

    def on_foreground(self):
        """윈도우: 앞에 나온 창이 바뀌었다. 바탕 화면 보기는 창 순서를 조금 뒤에 바꾸므로 잠깐 기다렸다가 한 번 확인한다."""
        if self._top_timer:
            self._top_timer.cancel()
        self._top_timer = threading.Timer(0.3, self.raise_if_sunk)
        self._top_timer.daemon = True
        self._top_timer.start()

    def raise_if_sunk(self):
        """'항상 위'인데 바탕 화면 보기·전체 화면 창에 가려졌으면 다시 올린다."""
        try:
            if self.cfg["topmost"] and not self.hidden and self.hwnd:
                keep_on_top(self.hwnd)
        except Exception:
            pass

    def start_position(self):
        x, y = self.cfg.get("wx"), self.cfg.get("wy")
        screens = self.webview.screens
        if x is not None and y is not None and any(
                s.x <= x < s.x + s.width - 40 and s.y <= y < s.y + s.height - 40 for s in screens):
            return x, y
        s = screens[0]
        return s.x + s.width - 136 - 24, s.y + 80

    def run(self):
        if not MAC:  # 작업 표시줄이 pythonw 대신 이 앱의 창 아이콘을 쓰게
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QuotaTank")
            except Exception:
                pass
        x, y = self.start_position()
        self.win = self.webview.create_window(
            "QuotaTank", web_page("main.html"),
            width=136, height=70, x=x, y=y, min_size=(60, 40), resizable=False,
            frameless=True, easy_drag=True, on_top=self.cfg["topmost"], background_color=web_bg(self.cfg["theme"]))
        self.api._expose(self.win)
        if not MAC:
            protect_window(self.win)
        self.win.events.moved += self.on_moved
        if MAC:
            hide_from_dock()
            self.win.events.shown += lambda: on_main(hide_from_dock)  # pywebview 가 정책을 되돌려 놓는 경우 대비
        else:
            self.win.events.shown += lambda: hide_from_taskbar("QuotaTank")
        self.start_tray()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.reset_alert_loop, daemon=True).start()
        threading.Thread(target=self.import_loop, daemon=True).start()
        threading.Thread(target=self.status_loop, daemon=True).start()
        threading.Thread(target=self.update_loop, daemon=True).start()
        threading.Thread(target=self.login_loop, daemon=True).start()
        threading.Thread(target=self.remote_loop, daemon=True).start()
        threading.Thread(target=self.cycle_loop, daemon=True).start()
        if not MAC:
            threading.Thread(target=self.resume_loop, daemon=True).start()
            threading.Thread(target=watch_foreground, args=(self.on_foreground,), daemon=True).start()
        # icon: 윈도우 작업 표시줄·Alt+Tab 에 파이썬 로고 대신 이 앱 아이콘
        self.webview.settings.update(ALLOW_DOWNLOADS=False, ALLOW_FILE_URLS=False,
                                     IGNORE_SSL_ERRORS=False, REMOTE_DEBUGGING_PORT=None,
                                     OPEN_DEVTOOLS_IN_DEBUG=False)
        self.webview.start(debug=False, private_mode=True, http_server=False,
                           storage_path=str(APP_DIR / "webview"), icon=str(WEB_DIR / "icon.ico"),
                           gui=None if MAC else "edgechromium")


# ── tk widget (pywebview 가 없을 때의 대체 화면) ─────────────────────────
class TkWidget(Core):
    W = 340

    def __init__(self):
        super().__init__()

        self.root = tk.Tk()
        self.root.title("QuotaTank")
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", self.cfg["topmost"])
        self.root.attributes("-alpha", self.cfg["alpha"])
        # 디스플레이 배율 (폰트는 자동, 좌표는 수동 보정). 맥 Tk 는 포인트 단위라 보정이 필요 없다.
        self.S = 1 if MAC else self.root.winfo_fpixels("1i") / 96
        self.cv = tk.Canvas(self.root, width=self.W * self.S, height=200, bg=BG, highlightthickness=0)
        self.cv.pack()

        x = self.cfg["x"] if self.cfg["x"] is not None else int(self.root.winfo_screenwidth() - (self.W + 24) * self.S)
        self.root.geometry(f"+{x}+{self.cfg['y']}")
        self.root.after(50, self.round_corners)

        self.cv.bind("<ButtonPress-1>", self.drag_start)
        self.cv.bind("<B1-Motion>", self.drag_move)
        self.cv.bind("<ButtonRelease-1>", self.drag_end)
        self.cv.bind("<Double-Button-1>", lambda e: self.toggle("compact"))
        if MAC:  # 맥 Tk 의 오른쪽 버튼은 Button-2, Control+클릭도 우클릭
            self.cv.bind("<Button-2>", self.menu)
            self.cv.bind("<Control-Button-1>", self.menu)
        else:
            self.cv.bind("<Button-3>", self.menu)

        self._polled_at = 0
        self.refresh()
        self.tick()

    # window chrome
    def round_corners(self):
        if MAC:
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    def drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def drag_end(self, e):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)

    def toggle(self, key):
        self.cfg[key] = not self.cfg[key]
        save_config(self.cfg)
        if key == "topmost":
            self.root.attributes("-topmost", self.cfg["topmost"])
        self.draw()

    def menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="지금 새로고침", command=self.refresh)
        m.add_command(label="인사이트 / 예측 보기", command=self.show_insights)
        m.add_separator()
        m.add_checkbutton(label="간단히 보기 (더블클릭)", command=lambda: self.toggle("compact"),
                          variable=tk.BooleanVar(value=self.cfg["compact"]))
        m.add_checkbutton(label="항상 위", command=lambda: self.toggle("topmost"),
                          variable=tk.BooleanVar(value=self.cfg["topmost"]))
        m.add_checkbutton(label="로그인 시 실행" if MAC else "윈도우 시작 시 실행", command=toggle_startup,
                          variable=tk.BooleanVar(value=STARTUP_FILE.exists()))
        m.add_separator()
        m.add_command(label="종료", command=self.root.destroy)
        m.tk_popup(e.x_root, e.y_root)

    # data
    def refresh(self):
        self._polled_at = time.time()
        threading.Thread(target=self._fetch_all, daemon=True).start()

    def tick(self):
        # fetched_at 은 실시간 조회에 성공했을 때만 바뀐다. 실패가 이어져도 다시 조회하도록 시도 시각을 따로 둔다
        if time.time() - self._polled_at > self.cfg["poll_sec"]:
            self.refresh()
        self.draw()
        self.root.after(1000, self.tick)

    # drawing
    def bar(self, x, y, w, h, pct, elapsed_frac, color):
        cv = self.cv
        cv.create_rectangle(x, y, x + w, y + h, fill=TRACK, width=0)
        fw = max(0, min(1, pct / 100)) * w
        if fw > 0:
            cv.create_rectangle(x, y, x + fw, y + h, fill=color, width=0)
        if elapsed_frac is not None:  # 시간 경과 눈금: 막대가 이 선보다 앞서면 과속
            tx = x + max(0, min(1, elapsed_frac)) * w
            cv.create_line(tx, y - 2, tx, y + h + 2, fill=TICK, width=2)

    def text(self, x, y, s, size=9, color=FG, bold=False, anchor="nw"):
        self.cv.create_text(x, y, text=s, fill=color, anchor=anchor,
                            font=(FONT, size, "bold" if bold else "normal"))

    def draw(self):
        cv = self.cv
        cv.delete("all")
        now = time.time()
        compact = self.cfg["compact"]
        y = 10
        with self.lock:
            data, err = dict(self.data), dict(self.err)
        for key, name, _ in SERVICES:
            y = self.draw_service(key, name, data.get(key), err.get(key), now, y, compact)
        if not compact:
            upd = f"업데이트 {hm(self.fetched_at)}" if self.fetched_at else "불러오는 중…"
            self.text(self.W - 12, y, upd + "  · 우클릭 메뉴", 7, SUB, anchor="ne")
            y += 16
        cv.scale("all", 0, 0, self.S, self.S)
        cv.config(width=self.W * self.S, height=(y + 4) * self.S)

    def draw_service(self, key, name, d, err, now, y, compact):
        W, x0 = self.W, 12
        top = y
        cv = self.cv
        h = 34 if compact else 112
        cv.create_rectangle(6, y - 4, W - 6, y + h, fill=CARD, width=0)
        cv.create_oval(x0, y + 4, x0 + 8, y + 12, fill=BRAND[key], width=0)
        self.text(x0 + 14, y, name, 10, FG, True)
        if d is None:
            self.text(x0 + 70, y + 2, err or "불러오는 중…", 8, RED if err else SUB)
            return top + h + 10

        active = bool(d["r5"]) and d["r5"] > now
        u5 = d["u5"] if active else 0.0
        weekly_ok = bool(d["rw"]) and d["rw"] > now
        uw = d["uw"] if weekly_ok else 0.0
        tag = d["plan"] + (" · 기록" if d["src"] == "log" else "")
        if err:
            tag = "⚠ " + tag
        self.text(W - 12, y + 1, tag, 7, SUB, anchor="ne")

        # 5시간
        el5 = 1 - (d["r5"] - now) / FIVE_H if active else None
        if compact:
            self.bar(x0 + 64, y + 6, 110, 7, u5, el5, level_color(u5))
            self.text(x0 + 180, y + 1, f"{u5:.0f}%", 8, FG)
            self.text(W - 12, y + 14, (dur(d["r5"] - now) + " 남음") if active else "대기 중", 8, SUB, anchor="ne")
            self.bar(x0 + 64, y + 20, 110, 3, uw, None, level_color(uw))
            self.text(x0 + 14, y + 16, f"주 {uw:.0f}%", 7, SUB)
            return top + h + 10

        y += 22
        self.text(x0, y - 2, "5시간", 8, SUB)
        self.bar(x0 + 42, y + 3, 150, 8, u5, el5, level_color(u5))
        self.text(x0 + 198, y - 2, f"{u5:.0f}%", 9, FG, True)
        if active:
            self.text(W - 12, y - 2, dur(d["r5"] - now), 10, FG, True, anchor="ne")
        else:
            self.text(W - 12, y - 1, "대기 · 첫 대화부터 5h", 8, SUB, anchor="ne")

        y += 18
        elw = 1 - (d["rw"] - now) / WEEK if weekly_ok else None
        self.text(x0, y - 2, "주간", 8, SUB)
        self.bar(x0 + 42, y + 3, 150, 8, uw, elw, level_color(uw))
        self.text(x0 + 198, y - 2, f"{uw:.0f}%", 9, FG, True)
        if weekly_ok:
            self.text(W - 12, y - 1, day_label(d["rw"]), 8, SUB, anchor="ne")

        # 사이클 계획
        y += 20
        starts = plan_cycles(now, d["r5"], active, self.cfg["day_reset_hour"])
        if active:
            label = f"리셋 {hm(d['r5'])}" + "".join(f" → {hm(t)}" for t in starts[1:])
            label += f"  (오늘 +{len(starts)}회)"
        else:
            label = "지금 시작 → " + " → ".join(hm(t + FIVE_H) for t in starts[:3]) + f" 리셋  ({len(starts)}회)"
        self.text(x0, y, "⟳ " + label, 8, FG)

        # 예측
        y += 17
        self.text(x0, y, self.forecast_line(key, d, u5, active, now), 8, SUB)
        y += 15
        self.text(x0, y, self.weekly_line(d, uw, weekly_ok, now), 8, SUB)
        return top + h + 10

    def forecast_line(self, key, d, u5, active, now):
        if not active:
            return "5h 창 없음 — 다음 첫 메시지 시각이 사이클 시작점"
        if u5 >= 100:
            return f"⛔ 한도 도달 — {hm(d['r5'])}에 풀림"
        rate = burn_rate(self.hist, key, d["r5"], now)
        if rate is None:
            return "소모 속도 측정 중… (몇 분 사용하면 표시)"
        if rate == 0:
            return "최근 90분 사용 없음"
        left_h = (d["r5"] - now) / 3600
        eta = now + (100 - u5) / rate * 3600
        if eta < d["r5"]:
            return f"⚠ {rate:.0f}%/h 속도면 {hm(eta)} 소진 · {dur_short(d['r5'] - eta)} 공백"
        return f"{rate:.0f}%/h 속도 · 리셋 때 약 {min(100, u5 + rate * left_h):.0f}% 예상"

    def weekly_line(self, d, uw, ok, now):
        if not ok:
            return "주간 창 정보 없음"
        left = d["rw"] - now
        elapsed = WEEK - left
        days_left = max(left / 86400, 0.01)
        budget = (100 - uw) / days_left
        diff = uw - elapsed / WEEK * 100  # 이상적인 소비선 대비: + 면 앞섬
        pace = "페이스 적정" if abs(diff) < 3 else f"{diff:+.0f}%p " + ("앞섬" if diff > 0 else "여유")
        if elapsed > 43200 and uw > 0:  # 첫 12시간은 표본이 적어 과장되므로 제외
            eta = now + (100 - uw) / (uw / elapsed)
            if eta < d["rw"]:
                return f"⚠ 주간 {pace} — {day_label(eta)} 소진 예상 · 하루 {budget:.0f}% 권장"
        return f"주간 {pace} · 남은 {days_left:.1f}일 동안 하루 {budget:.0f}%씩"

    # insights
    def show_insights(self):
        win = tk.Toplevel(self.root)
        win.title("AI 사용 인사이트")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        t = tk.Text(win, width=62, height=30, bg=CARD, fg=FG, font=(FONT, 9), relief="flat",
                    padx=14, pady=12, wrap="word")
        t.pack(fill="both", expand=True)
        t.insert("1.0", self.insights_text())
        t.config(state="disabled")

    def insights_text(self):
        now = time.time()
        out = []
        with self.lock:
            data = dict(self.data)
        span_days = (now - self.hist.rows[0]["t"]) / 86400 if self.hist.rows else 0
        out.append(f"기록 기간: {span_days:.1f}일  ({len(self.hist.rows)}개 샘플)\n")
        for key, name, _ in SERVICES:
            out.append(f"━━ {name} ━━")
            wins = [w for w in windows(self.hist, key) if w[0] >= now - 14 * 86400]
            if not wins:
                out.append("  아직 기록된 사이클이 없어요. 며칠 쓰면 패턴이 보여요.\n")
                continue
            by_day = defaultdict(list)
            for start, peak in wins:
                by_day[datetime.fromtimestamp(start).date()].append((start, peak))
            peaks = [p for _, p in wins]
            hits = sum(p >= 99 for p in peaks)
            first_starts = [min(s for s, _ in v) for v in by_day.values()]
            first_min = statistics.median(
                datetime.fromtimestamp(s).hour * 60 + datetime.fromtimestamp(s).minute for s in first_starts)
            out.append(f"  최근 14일 사이클 {len(wins)}회 · 사용한 날 {len(by_day)}일 · "
                       f"하루 평균 {len(wins) / len(by_day):.1f}회")
            out.append(f"  사이클당 평균 최고 사용률 {statistics.mean(peaks):.0f}% · 한도 도달 {hits}회")
            out.append(f"  보통 첫 사이클 시작: {int(first_min // 60):02d}:{int(first_min % 60):02d}")

            # 주간 소모 속도
            d = data.get(key)
            if d and d.get("rw") and d["rw"] > now:
                elapsed_days = max((WEEK - (d["rw"] - now)) / 86400, 0.1)
                per_day = d["uw"] / elapsed_days
                proj = per_day * 7
                out.append(f"  이번 주 하루 평균 {per_day:.1f}% 소모 → 주 전체 예상 {proj:.0f}%"
                           + ("  (아직 1일 미만이라 부정확)" if elapsed_days < 1 else ""))

            # 추천
            tips = []
            if hits >= 2:
                tips.append("5시간 한도에 자주 닿아요. 무거운 작업(긴 코드/파일)은 사이클 초반에 몰고, "
                            "막히면 다른 AI로 넘기는 흐름을 추천해요.")
            if statistics.mean(peaks) < 40 and len(wins) >= 3:
                tips.append("사이클마다 여유가 많아요. 5시간 한도보다 주간 한도 관리가 더 중요해요.")
            fm = int(first_min)
            pre = (fm - 150) % 1440
            tips.append(f"[사이클 앵커] 5시간 창은 첫 메시지부터 시작돼요. 평소 시작({fm // 60:02d}:{fm % 60:02d})보다 "
                        f"2~3시간 일찍(예: {pre // 60:02d}:{pre % 60:02d}) 짧은 인사 한 줄을 보내 두면, "
                        f"본격 작업 도중에 리셋이 한 번 더 와서 하루에 쓸 수 있는 사이클이 늘어나요.")
            if d and d.get("rw") and d["rw"] > now:
                if elapsed_days < 1:
                    pass
                elif proj > 100:
                    tips.append(f"이 속도면 주간 한도가 모자라요({proj:.0f}%). 남은 기간 하루 "
                                f"{(100 - d['uw']) / max((d['rw'] - now) / 86400, 0.1):.0f}% 안쪽으로 조절하세요.")
                elif proj < 60:
                    tips.append("주간 한도가 넉넉해요. 더 적극적으로 써도 돼요.")
            for tip in tips:
                out.append("  • " + tip)
            out.append("")
        return "\n".join(out)

    def run(self):
        self.root.mainloop()


def run_tk():
    if tk is None:
        sys.exit("화면을 띄울 수 없어요: pip install pywebview (또는 tkinter 설치)")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = TkWidget()
    watch_quit_signal(lambda: app.root.after(0, app.root.destroy))
    app.run()


# ── 한 번만 실행: 이미 떠 있으면 그쪽을 끄고 나간다(아이콘 한 번 = 켜기/끄기) ──
def quit_socket_path(app_dir, tmp_dir, uid):
    """맥 종료 신호 소켓 경로. 유닉스 소켓 경로는 104바이트까지라, 사용자 이름이 길어
    설정 폴더 경로가 넘치면 사용자 전용 임시 폴더($TMPDIR)에 둔다."""
    path = str(Path(app_dir) / "quit.sock")
    if len(os.fsencode(path)) > 100:
        path = os.path.join(tmp_dir, f"quotatank-{uid}.sock")
    return path


if MAC:
    import fcntl
    import tempfile

    _LOCK_PATH = APP_DIR / "widget.lock"
    try:  # 사용자 전용 임시 폴더. TMPDIR 이 없을 때 모두가 쓰는 /tmp 로 떨어지지 않게 직접 묻는다
        _TMP = os.confstr("CS_DARWIN_USER_TEMP_DIR") or tempfile.gettempdir()
    except (ValueError, OSError):
        _TMP = tempfile.gettempdir()
    _QUIT_SOCK = quit_socket_path(APP_DIR, _TMP, os.getuid())
    _lock_fd = None

    def toggle_off_if_running():
        """이미 실행 중이면(잠금 파일이 잡혀 있으면) 그 위젯의 소켓에 종료 신호를 보내고 True."""
        global _lock_fd
        _lock_fd = open(_LOCK_PATH, "w")  # 프로세스가 끝날 때까지 쥔다
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            pass
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(3)
                c.connect(_QUIT_SOCK)
                c.sendall(b"quit")
        except OSError:
            pass
        return True

    def watch_quit_signal(quit_fn):
        try:
            os.unlink(_QUIT_SOCK)
        except FileNotFoundError:
            pass
        except OSError:  # 다른 사용자의 파일 등. bind 도 실패한다
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(_QUIT_SOCK)
            srv.listen(1)
        except OSError:  # 종료 신호 없이 계속 돈다(아이콘 다시 누르기로 끄기만 안 된다)
            srv.close()
            return

        def wait():
            conn, _ = srv.accept()
            conn.close()
            on_main(quit_fn)
        threading.Thread(target=wait, daemon=True).start()
else:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.CreateEventW.restype = ctypes.c_void_p
    _k32.OpenEventW.restype = ctypes.c_void_p
    _k32.SetEvent.argtypes = [ctypes.c_void_p]
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _QUIT_EVENT = r"Local\QuotaTank.quit"

    def toggle_off_if_running():
        """이미 실행 중이면 그 위젯에 종료 신호를 보내고 True."""
        _k32.CreateMutexW(None, False, r"Local\QuotaTank")  # 핸들은 프로세스가 끝날 때까지 쥔다
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return False
        ev = _k32.OpenEventW(0x0002, False, _QUIT_EVENT)  # EVENT_MODIFY_STATE
        if ev:
            _k32.SetEvent(ev)
        return True

    def watch_quit_signal(quit_fn):
        ev = _k32.CreateEventW(None, True, False, _QUIT_EVENT)

        def wait():
            _k32.WaitForSingleObject(ev, 0xFFFFFFFF)
            quit_fn()
        threading.Thread(target=wait, daemon=True).start()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--type-into"]:  # type_into_console 이 부르는 내부 명령: 위젯을 켜고 끄지 않는다
        try:
            ok = len(sys.argv) in (4, 5) and authorized_console_resume(int(sys.argv[2]), sys.argv[3], float(sys.argv[4]) if len(sys.argv) == 5 else 0)
        except (ValueError, IndexError):
            ok = False
        sys.exit(0 if ok else 1)
    if toggle_off_if_running():
        sys.exit()
    migrate_startup()
    if "--tk" in sys.argv:
        run_tk()
    else:
        try:
            import webview
        except ImportError:  # pip install pywebview 전에는 기존 Tk 화면
            run_tk()
        else:
            app = WebWidget(webview)
            watch_quit_signal(app.api.quit)
            app.run()
