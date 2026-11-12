"""자격 증명 브로커: 로그인 토큰을 읽는 코드는 이 프로세스에서만 돈다.

위젯(화면) 프로세스가 `--provider-worker` 로 한 번 띄우고, 표준 입출력 파이프로 한 줄짜리 JSON 요청을 보낸다.
    요청  {"id": 정수, "op": "fetch"|"inspect"|"profiles"|"default_email"|"active"|"switch"|"credit"|"credit_key",
           "provider": 서비스 키, "path": 로그인 파일|null, "value"?: credit 은 결제 주기 시작 epoch, credit_key 는 관리자 키|""|null}
    응답  {"id": 정수, "ok": 값} 또는 {"id": 정수, "error": 문구, "retry_after"?: 429 일 때 대기 초(모르면 0)}
응답에는 사용량 숫자·이메일·유효 여부·로그인 파일 경로만 담긴다. 토큰을 돌려주거나 임의 파일 내용을 읽어 주는 요청은 없다.
API 크레딧(credit·credit_key): 관리자 키는 저장·지우기만 받고 돌려주지 않는다. 답은 쓴 금액과 저장돼 있는지뿐이다.
작업 계정 바꾸기(switch)는 기본 로그인 자리와 설정에 연결된 추가 계정 폴더 사이에서만 로그인을 맞바꾼다(SECURITY.md).
파이프는 위젯이 만든 익명 파이프라 다른 프로그램이 이 브로커에 접속할 수 없고, 위젯이 끝나면(입력이 닫히면) 같이 끝난다.

같은 사용자 권한으로 도는 프로세스라 OS 샌드박스는 아니다(SECURITY.md). 화면 쪽 코드가 오작동하거나
조작돼도 정해진 질문만 할 수 있게 하는 경계다.
"""
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import providers
from hardening import app_dir, config_values
from providers import anthropic_api
from providers.common import SWITCH_LOCK, RateLimited, parked_folder, read_json, switch_account

OPS = ("fetch", "inspect", "profiles", "default_email", "active", "switch", "credit", "credit_key")
KEYS = ("claude", "gpt", "gemini")
MAX_REQUEST = 8192
MAX_REPLY = 65536
MAX_INFLIGHT = 16          # 동시에 처리 중인 요청
RATE = (240, 60)           # 60초에 240개까지 — 기본 3개 + 추가 계정 20개를 10초마다 새로 고쳐도 남는다
CONFIG_PATH = app_dir() / "config.json"


def registered_paths(key):
    """연결할 때 확인해서 설정(config.json)에 저장한 이 서비스의 로그인 파일들."""
    try:
        accounts = config_values({}, read_json(CONFIG_PATH))["accounts"]
    except (OSError, ValueError):
        return set()
    out = set()
    for a in accounts:
        if a["provider"] == key:
            try:
                out.add(Path(a["path"]).resolve(strict=True))
            except (OSError, RuntimeError):
                continue
    return out


def handle(request):
    """요청 하나를 검사하고 처리한다. 형식이 틀리면 ValueError."""
    if not isinstance(request, dict) or set(request) - {"op", "provider", "path", "value"}:
        raise ValueError("잘못된 조회 요청")
    op, key = request.get("op"), request.get("provider")
    if op not in OPS or key not in KEYS:
        raise ValueError("지원하지 않는 조회 요청")
    if op in ("credit", "credit_key"):
        return credit(op, key, request)
    if "value" in request:
        raise ValueError("잘못된 조회 요청")
    provider = providers.BY_KEY[key]
    path = request.get("path")
    if path is not None:
        login_file = (getattr(provider, "LOGIN", None) or {}).get("file")
        if not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute() or not login_file:
            raise ValueError("잘못된 로그인 파일 경로")
        try:
            resolved = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("로그인 파일을 찾지 못했어요") from None
        if not resolved.is_file():
            raise ValueError("잘못된 로그인 파일 경로")
        # 표준 이름(.credentials.json / auth.json)이면 받는다. 바로 가기로 다른 이름의 파일을 가리키지 않게 실제 이름도 본다.
        # 이름을 바꾼 사본은 연결할 때 확인해 설정에 등록한 경로만 사용량을 조회한다.
        # 연결 전 확인(inspect)은 JSON 파일이면 받는다 — 답은 유효 여부·이메일뿐이다.
        if not (Path(path).name == login_file and resolved.name == login_file):
            allowed = resolved.suffix.lower() == ".json" if op == "inspect" else resolved in registered_paths(key)
            if not allowed:
                raise ValueError("잘못된 로그인 파일 경로")
        path = str(resolved)
    if op == "fetch":
        return provider.fetch(path)
    if op in ("active", "switch"):
        return work_account(provider, key, op, path)
    if key == "gemini" or path is not None and op != "inspect":
        raise ValueError("지원하지 않는 계정 요청")
    if op == "inspect":
        if path is None:
            raise ValueError("로그인 파일을 선택하세요")
        return {"valid": bool(provider.valid(read_json(path))), "email": provider.email(path)}
    if op == "default_email":
        return provider.email()
    found = []
    for file in provider.find_profiles()[:40]:
        try:
            resolved = file.resolve(strict=True)
            if resolved.name == provider.LOGIN["file"] and resolved.is_file():
                found.append({"path": str(resolved), "email": provider.email(str(resolved))})
        except (OSError, ValueError, RuntimeError):
            continue
    return found


def credit(op, key, request):
    """API 크레딧. credit: value(결제 주기 시작 epoch, 40일 안) 이후 쓴 금액. credit_key: 관리자 키 저장·지우기·저장 여부."""
    value = request.get("value")
    if key != "claude" or request.get("path") is not None:
        raise ValueError("지원하지 않는 조회 요청")
    if op == "credit":
        if type(value) is not int or not time.time() - 40 * 86400 <= value <= time.time():
            raise ValueError("잘못된 결제 주기")
        return anthropic_api.fetch(value)
    if not (value is None or isinstance(value, str) and len(value) <= 256):
        raise ValueError("잘못된 관리자 키")
    return anthropic_api.set_key(value)


def work_account(provider, key, op, path):
    """작업 계정. active: 원래 기본 계정의 로그인을 맡아 둔 추가 계정의 로그인 파일(없으면 None — 기본 계정이 작업 중).
    switch: path 의 추가 계정을 기본 자리로(None 이면 원래 기본 계정으로 되돌리기). 표준 이름의 연결된 로그인 폴더만 받는다."""
    if key == "gemini" or op == "active" and path is not None:
        raise ValueError("지원하지 않는 계정 요청")
    login_file = provider.LOGIN["file"]
    home, default = Path(provider.HOME).resolve(), provider.DEFAULT_FILE.parent.resolve()
    # 홈 바로 아래의 이 서비스 설정 폴더(~/.claude-이름, ~/.codex-이름)만 맞바꾼다 — 원래 기본 계정의 로그인을 맡기는 곳이다
    folders = [p.parent for p in sorted(registered_paths(key)) if p.name == login_file and p.parent.parent == home
               and p.parent.name.startswith(default.name) and p.parent != default]
    target = None
    if path is not None:
        target = Path(path).parent
        if Path(path).name != login_file or target not in folders:
            raise ValueError("이 계정은 작업 계정으로 바꿀 수 없어요 · 계정 추가로 다시 연결하세요")
    try:
        if op == "switch":
            parked = switch_account(provider, target, folders)
        else:
            with SWITCH_LOCK:  # 진행 중인 전환의 준비 파일을 끊긴 것으로 보고 지우지 않게
                parked = parked_folder(provider, folders)
    except OSError:
        raise RuntimeError("계정을 바꾸지 못했어요 · 잠시 뒤 다시 시도하세요") from None
    return str(parked / login_file) if parked else None


def public_error(ex):
    """화면으로 보낼 오류 문구. 서비스 모듈이 직접 쓴 안내(ValueError/RuntimeError)만 그대로 보내고,
    OS·네트워크 오류 원문은 경로나 헤더가 섞일 수 있어 보내지 않는다."""
    if type(ex) in (ValueError, RuntimeError):
        return str(ex)[:120] or "계정 조회 실패"
    return "계정 조회 실패"


def answer(request):
    try:
        value = {"ok": handle(request)}
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_REPLY - 64:
            raise ValueError("조회 결과가 너무 큽니다")
        return value
    except RateLimited as ex:
        return {"error": str(ex), "retry_after": ex.retry_after or 0}
    except Exception as ex:
        return {"error": public_error(ex)}


class Broker:
    def __init__(self, out):
        self.out = out
        self.write_lock = threading.Lock()
        self.inflight = threading.BoundedSemaphore(MAX_INFLIGHT)
        self.recent = deque()
        self.pool = ThreadPoolExecutor(max_workers=4)

    def reply(self, rid, body):
        line = json.dumps({"id": rid, **body}, ensure_ascii=False, allow_nan=False) + "\n"
        with self.write_lock:
            self.out.write(line.encode("utf-8"))
            self.out.flush()

    def allowed(self):
        now = time.monotonic()
        while self.recent and now - self.recent[0] > RATE[1]:
            self.recent.popleft()
        if len(self.recent) >= RATE[0]:
            return False
        self.recent.append(now)
        return True

    def work(self, rid, request):
        try:
            self.reply(rid, answer(request))
        except (OSError, ValueError):
            os._exit(0)  # 위젯이 사라졌다
        finally:
            self.inflight.release()

    def serve(self, inp):
        while True:
            line = inp.readline(MAX_REQUEST + 1)
            if not line:
                return  # 위젯이 끝났다
            if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                return  # 규칙을 어긴 상대와는 더 이야기하지 않는다(위젯이 다시 띄운다)
            try:
                message = json.loads(line.decode("utf-8"))
                rid = message.pop("id")
                if type(rid) is not int or not 0 < rid < 2 ** 53:
                    return
            except (ValueError, KeyError, AttributeError, TypeError):
                return
            if not self.allowed():
                self.reply(rid, {"error": "요청이 너무 많아요 · 잠시 뒤 다시 시도하세요"})
                continue
            if not self.inflight.acquire(blocking=False):
                self.reply(rid, {"error": "처리 중인 요청이 많아요 · 잠시 뒤 다시 시도하세요"})
                continue
            self.pool.submit(self.work, rid, message)


def main():
    # PyInstaller 의 창 모드 실행파일은 파이프를 줘도 sys.stdin/stdout 을 None 으로 둘 수 있다
    inp = sys.stdin.buffer if sys.stdin is not None else os.fdopen(0, "rb", closefd=False)
    out = sys.stdout.buffer if sys.stdout is not None else os.fdopen(1, "wb", closefd=False)
    try:
        Broker(out).serve(inp)
    finally:
        os._exit(0)  # 진행 중인 조회를 기다리지 않는다


if __name__ == "__main__":
    main()
