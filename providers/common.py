"""서비스 모듈들이 같이 쓰는 것: 경로, 시간 상수, HTTP, 윈도우 자격 증명 읽기, 작업 계정 바꾸기."""
import base64
import ctypes
import json
import os
import random
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

from .meta import FIVE_H, HOME, MAC, WEEK  # noqa: F401 (서비스 모듈이 여기서 가져간다)
from .net import NoRedirect, RateLimited, http_json  # noqa: F401


def iso_to_epoch(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def read_json(path, limit=2 * 1024 * 1024):
    """로그인 파일은 2 MiB 까지. ~/.claude.json 처럼 대화 기록이 쌓이는 설정 파일은 limit 을 키워 읽는다."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size > limit:
        raise ValueError("Invalid credentials file")
    with p.open("rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Credentials file too large")
    return json.loads(raw.decode("utf-8"))


def jwt_claims(token):
    """서명은 확인하지 않고 JWT 본문만 읽는다(표시용 이메일)."""
    try:
        body = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return {}


if MAC:
    def read_win_cred(target):
        return None
else:
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def read_win_cred(target):
        """윈도우 자격 증명 관리자의 일반 자격 증명(JSON)을 읽는다."""
        pcred = ctypes.POINTER(_CREDENTIAL)()
        if ctypes.windll.advapi32.CredReadW(target, 1, 0, ctypes.byref(pcred)):
            try:
                raw = bytes(pcred.contents.CredentialBlob[:pcred.contents.CredentialBlobSize])
                return json.loads(raw.decode("utf-8"))
            finally:
                ctypes.windll.advapi32.CredFree(pcred)
        return None


# ── 작업 계정 바꾸기: 기본 로그인 자리(~/.claude, ~/.codex)와 추가 계정 폴더의 로그인을 맞바꾼다 ──
# 같은 토큰이 두 곳에 살아 있지 않게 복사하지 않고 맞바꾼다(갱신 토큰은 쓰면 바뀌어 사본은 곧 무효가 된다).
# 기본 계정의 로그인을 맡아 둔 폴더에는 PARKED 표시를 둔다. 표시가 있는 폴더는 많아야 하나다.
PARKED = ".quotatank-default"
JOURNAL = ".quotatank-swap.json"
NEXT = ".quotatank-next"
SWITCH_LOCK = threading.Lock()  # 브로커 안에서 전환을 한 번에 하나씩


def write_next(final, data):
    """final 옆에 새 내용을 써 두고(디스크까지) 그 임시 파일 경로를 돌려준다. 바꿔 끼우기는 commit_swap 이 한다."""
    tmp = Path(str(final) + NEXT)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def _replace(src, dst):
    """윈도우에서는 다른 프로그램이 잠깐 열어 둔 파일이면 이름 바꾸기가 실패한다. 조금 기다렸다 다시 한다."""
    for i in range(20):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.1 + i * 0.02)


def commit_swap(folder, finals, parked):
    """write_next 로 finals 의 새 내용을 모두 써 둔 뒤 부른다. 일지를 먼저 남기고 바꿔 끼운다.
    중간에 끊겨도(전원·오류) 다음 전환·상태 확인 때 finish_swap 이 일지대로 마저 끝낸다."""
    journal = Path(folder) / JOURNAL
    plan = json.dumps({"files": [str(f) for f in finals], "parked": bool(parked)}).encode("utf-8")
    os.replace(write_next(journal, plan), journal)
    finish_swap(folder, finals)


def finish_swap(folder, allowed):
    """일지가 있으면 남은 바꿔 끼우기와 표시를 마저 한다. 일지에 적힌 파일이 allowed(이 폴더 짝의 로그인·설정 파일)
    밖이면 손대지 않는다. 일지가 없는 준비 파일은 지우지 않는다 — 기본 자리 파일은 다른 폴더의 일지 몫일 수 있다."""
    folder = Path(folder)
    allowed = [Path(f) for f in allowed]
    journal = folder / JOURNAL
    if not journal.is_file():
        return
    try:
        plan = json.loads(journal.read_text(encoding="utf-8"))
        files = [Path(f) for f in plan["files"]]
        parked = plan["parked"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("계정 전환 기록을 읽지 못했어요") from None
    if type(parked) is not bool or not all(f in allowed for f in files):
        raise RuntimeError("계정 전환 기록이 올바르지 않아요")
    for f in files:
        nxt = Path(str(f) + NEXT)
        if nxt.is_file():
            _replace(nxt, f)
    marker = folder / PARKED
    if parked:
        marker.write_text("기본 계정의 로그인을 이 폴더에 맡아 두었어요. 지우지 마세요(QuotaTank).\n", encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)
    journal.unlink()


@contextmanager
def dir_lock(path, stale, timeout=5.0):
    """Claude Code(npm proper-lockfile)와 같은 방식의 잠금: 폴더를 만들 수 있으면 잡은 것이다.
    stale 초보다 오래 손대지 않은 잠금은 주인이 죽은 것으로 보고 가져온다. 잡고 있는 동안 3초마다 시각을 갱신한다."""
    path = Path(path)
    start = time.monotonic()
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            pass
        if time.monotonic() - start > timeout:
            raise RuntimeError("Claude Code가 로그인을 갱신하는 중이에요 · 잠시 뒤 다시 시도하세요")
        try:
            if time.time() - os.stat(path).st_mtime > stale:
                os.rmdir(path)
                continue
        except OSError:
            continue
        time.sleep(0.25 + random.random() * 0.25)
    stop = threading.Event()

    def touch():
        while not stop.wait(3):
            try:
                os.utime(path)
            except OSError:
                return
    threading.Thread(target=touch, daemon=True).start()
    try:
        yield
    finally:
        stop.set()
        try:
            os.rmdir(path)
        except OSError:
            pass


def switch_account(provider, target, folders):
    """작업 계정을 바꾼다. target: 기본 자리에 넣을 추가 계정 폴더, None 이면 원래 기본 계정으로 되돌린다.
    folders: 설정에 연결된 이 서비스의 추가 계정 폴더들. 돌려주는 값은 지금 기본 계정을 맡아 둔 폴더(없으면 None)."""
    if MAC:
        raise RuntimeError("작업 계정 바꾸기는 아직 윈도우만 지원해요")
    folders = [Path(f) for f in folders]
    with SWITCH_LOCK:
        current = parked_folder(provider, folders)
        if current == target:
            return current
        ready = getattr(provider, "switch_ready", None)
        if ready:
            ready()
        with ExitStack() as stack:
            for lock, stale in provider.switch_locks([x for x in (current, target) if x]):
                stack.enter_context(dir_lock(lock, stale))
            if current:
                provider.swap(current, parked=False)
            if target:
                provider.swap(target, parked=True)
        return target


def parked_folder(provider, folders):
    """끊긴 전환을 마저 끝내고, 기본 계정의 로그인을 맡아 둔 폴더를 찾는다(없으면 None)."""
    for f in folders:
        finish_swap(f, provider.swap_files(f))
    for f in folders:  # 일지를 다 끝낸 뒤 남은 준비 파일은 일지를 남기기 전에 끊긴 것이다
        for name in [*provider.swap_files(f), f / JOURNAL]:
            Path(str(name) + NEXT).unlink(missing_ok=True)
    found = [f for f in folders if (f / PARKED).is_file()]
    if len(found) > 1:
        raise RuntimeError("기본 계정을 맡아 둔 폴더가 여러 개예요 · 계정 관리에서 다시 로그인하세요")
    return found[0] if found else None
