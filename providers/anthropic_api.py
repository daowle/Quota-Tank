"""Claude API 크레딧(Max·Ultra 요금제의 월간 크레딧): 사용자가 넣은 Console 관리자 키(Admin API 키)로
이번 결제 주기에 쓴 금액을 조회한다(GET /v1/organizations/cost_report).

관리자 키는 윈도우 자격 증명 관리자 / 맥 키체인(QuotaTank:anthropic-admin-key)에만 두고 이 모듈(브로커)만 읽는다.
화면 프로세스는 저장·지우기·저장돼 있는지만 물을 수 있고, 키를 돌려받는 요청은 없다(SECURITY.md).
조직의 비용 보고서는 읽기만 한다. 다른 관리 기능(키·멤버·작업 공간 바꾸기)은 부르지 않는다."""
import ctypes
import json
import math
import re
import subprocess
import time
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlencode

from .common import MAC, http_json

TARGET = "QuotaTank:anthropic-admin-key"
# Admin API 키(sk-ant-admin01-…)와, 작업 공간에 묶이지 않은 개인·서비스 계정 키(sk-ant-api…)도 비용 보고서를 볼 수 있다.
# 형식만 확인하고 권한은 서버 응답(401·403)으로 가린다
KEY_RE = re.compile(r"sk-ant-[a-z]{2,12}[0-9]{0,4}-[0-9A-Za-z_-]{8,240}")
URL = "https://api.anthropic.com/v1/organizations/cost_report"
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
MAX_PAGES = 4  # 하루 단위 31칸 × 4쪽: 결제 주기(한 달)를 넉넉히 덮는다
NO_KEY = "관리자 키가 없어요 · 계정 관리 → API 크레딧에서 넣어 주세요"
BAD_KEY = "Claude Console 키 형식이 아니에요 · sk-ant- 로 시작하는 키 전체를 붙여 넣어 주세요"
WRONG_KEY = "키가 맞지 않거나 지워진 키예요 · Console 에서 키를 확인하세요"
NO_ACCESS = "이 키로는 비용 보고서를 볼 수 없어요 · Admin API 키(sk-ant-admin…)나 작업 공간에 묶이지 않은 키가 필요해요"


def needs_admin(error):
    """401 이 '틀린 키'가 아니라 '이 종류의 키로는 Admin API 를 못 쓴다'는 뜻인지(작업 공간에 묶인 개인 키 등).
    서버 문구는 판단에만 쓰고 화면으로 넘기지 않는다."""
    try:
        body = json.loads(error.read(4096) or b"{}")
        return "admin api" in str((body.get("error") or {}).get("message") or "").lower()
    except Exception:
        return False


def valid_key(key):
    return isinstance(key, str) and KEY_RE.fullmatch(key) is not None


if MAC:
    def read_key():
        try:
            out = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", TARGET, "-a", "QuotaTank", "-w"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        key = out.stdout.strip() if out.returncode == 0 else ""
        return key if valid_key(key) else None

    def write_key(key):
        # -U: 있으면 바꾼다. 키는 명령 인자로 넘어간다(같은 사용자 권한의 프로세스 목록에 잠깐 보일 수 있다)
        out = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-s", TARGET, "-a", "QuotaTank", "-w", key],
                             capture_output=True, timeout=10)
        return out.returncode == 0

    def delete_key():
        out = subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", TARGET, "-a", "QuotaTank"],
                             capture_output=True, timeout=10)
        return out.returncode in (0, 44)  # 44: 원래 없다
else:
    from .common import _CREDENTIAL

    def read_key():
        pcred = ctypes.POINTER(_CREDENTIAL)()
        if not ctypes.windll.advapi32.CredReadW(TARGET, 1, 0, ctypes.byref(pcred)):
            return None
        try:
            raw = bytes(pcred.contents.CredentialBlob[:pcred.contents.CredentialBlobSize])
        finally:
            ctypes.windll.advapi32.CredFree(pcred)
        try:
            key = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return key if valid_key(key) else None

    def write_key(key):
        blob = key.encode("utf-8")
        buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
        cred = _CREDENTIAL()
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = TARGET
        cred.UserName = "QuotaTank"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE: 이 PC 의 이 사용자만
        return bool(ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0))

    def delete_key():
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        if advapi.CredDeleteW(TARGET, 1, 0):
            return True
        return ctypes.get_last_error() == 1168  # ERROR_NOT_FOUND: 원래 없다


def set_key(value):
    """None: 저장돼 있는지만. "": 지우기. 그 밖의 문자열: 형식을 확인하고 저장. {"saved": 저장돼 있는지}"""
    if value is None:
        return {"saved": read_key() is not None}
    if value == "":
        if not delete_key():
            raise RuntimeError("관리자 키를 지우지 못했어요")
        return {"saved": False}
    if not valid_key(value):
        raise ValueError(BAD_KEY)
    if not write_key(value):
        raise RuntimeError("관리자 키를 저장하지 못했어요")
    return {"saved": True}


def fetch(since):
    """since(epoch, 결제 주기 시작) 이후 쓴 금액(달러)과 날짜별 금액. 비용 보고서는 UTC 하루 단위라 시작일은 그날 0시(UTC)부터 센다.
    {"spent": 합계, "daily": [["YYYY-MM-DD"(UTC), 달러], …], "observed_at"}"""
    key = read_key()
    if not key:
        raise RuntimeError(NO_KEY)
    start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "QuotaTank/1.0"}
    cents, page, daily = 0.0, None, {}
    for _ in range(MAX_PAGES):
        query = {"starting_at": start, "bucket_width": "1d", "limit": 31, **({"page": page} if page else {})}
        try:
            d = http_json(URL + "?" + urlencode(query), headers)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError(NO_ACCESS if needs_admin(e) else WRONG_KEY) from None
            if e.code == 403:
                raise RuntimeError(NO_ACCESS) from None
            if e.code == 404:
                raise RuntimeError("이 조직은 비용 보고서를 볼 수 없어요 · Console 에서 조직을 만들어야 해요") from None
            raise RuntimeError("API 크레딧 조회 실패") from None
        except RuntimeError:
            raise  # RateLimited 와 직접 쓴 안내
        except Exception:  # 요청 오류 원문에는 헤더(키)가 섞일 수 있어 넘기지 않는다
            raise RuntimeError("API 크레딧 조회 실패 · 네트워크를 확인하세요") from None
        for bucket in d.get("data") or []:
            day = str(bucket.get("starting_at") or "")[:10]
            day = day if DAY_RE.fullmatch(day) else None  # 날짜가 이상한 칸은 합계에만 넣는다
            for item in bucket.get("results") or []:
                if (item.get("currency") or "USD") != "USD":
                    continue
                try:
                    amount = float(item.get("amount") or 0)
                except (TypeError, ValueError):  # 서버 글자를 화면 오류로 넘기지 않는다
                    amount = math.nan
                if not math.isfinite(amount) or amount < 0:
                    raise RuntimeError("비용 보고서 값을 읽지 못했어요")
                cents += amount
                if day:
                    daily[day] = daily.get(day, 0.0) + amount
        page = d.get("next_page")
        if not d.get("has_more") or not isinstance(page, str):
            break
    return {"spent": round(cents / 100, 2), "daily": [[k, round(v / 100, 2)] for k, v in sorted(daily.items())][-62:],
            "observed_at": time.time()}
