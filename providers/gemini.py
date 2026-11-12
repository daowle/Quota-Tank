"""Gemini (Antigravity): 윈도우 자격 증명 / 맥 키체인(gemini:antigravity)의 OAuth 토큰 → Cloud Code usage API."""
import json
import subprocess
import urllib.error
import urllib.request

from .common import MAC, iso_to_epoch, read_win_cred, http_json
from .meta import GEMINI as _META

KEY, NAME, STATUS_URL, LOGIN = _META.KEY, _META.NAME, _META.STATUS_URL, _META.LOGIN


def credentials():
    """읽기만 한다 — 토큰 갱신·저장은 agy 가 한다(다른 앱의 로그인 정보를 고쳐 쓰지 않는다)."""
    if MAC:
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", "gemini:antigravity", "-w"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout.strip())
        except Exception:
            pass
    else:
        cred = read_win_cred("gemini:antigravity")
        if cred:
            return cred
    raise RuntimeError("Antigravity 로그인 정보 없음 — agy 로그인 필요")


def fetch(path=None):
    # 매번 새로 읽는다: agy 가 토큰을 갱신해 두면 바로 반영된다
    access_token = (credentials().get("token") or {}).get("access_token")
    if not access_token:
        raise RuntimeError("토큰 없음 — agy를 한 번 실행하면 갱신돼요")
    headers = {
            "Authorization": "Bearer " + access_token,
            "Content-Type": "application/json",
            "User-Agent": "antigravity-cli/1.0",
        }
    try:
        d = http_json("https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary", headers, data=b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("토큰 만료 — agy를 한 번 실행하면 갱신돼요")
        raise

    groups = d.get("groups", [])
    gemini_group = None
    for g in groups:
        bids = [b.get("bucketId", "") for b in g.get("buckets", [])]
        if any("gemini" in b for b in bids) or "gemini" in g.get("displayName", "").lower():
            gemini_group = g
            break
    if not gemini_group and groups:
        gemini_group = groups[0]

    buckets = (gemini_group or {}).get("buckets", [])
    b5 = next((b for b in buckets if b.get("window") == "5h" or "5h" in b.get("bucketId", "")), None)
    bw = next((b for b in buckets if b.get("window") == "weekly" or "weekly" in b.get("bucketId", "")), None)
    # 응답 모양이 바뀌어 한도를 못 찾으면 0%(여유)로 보이지 않게 오류로 알린다
    if not b5 or b5.get("remainingFraction") is None:
        raise RuntimeError("5시간 한도를 응답에서 못 찾음")

    u5 = (1.0 - b5["remainingFraction"]) * 100
    r5 = iso_to_epoch(b5.get("resetTime"))
    remw = (bw or {}).get("remainingFraction")
    uw = (1.0 - remw) * 100 if remw is not None else 0.0  # 주간 한도가 없는 요금제도 있다
    rw = iso_to_epoch((bw or {}).get("resetTime")) if remw is not None else None

    return {"u5": u5, "r5": r5, "uw": uw, "rw": rw, "plan": "Gemini", "src": "live"}
