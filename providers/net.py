"""HTTPS JSON 요청. 토큰과 무관해서 화면 프로세스(장애 상태 조회)와 브로커(사용량 조회)가 같이 쓴다."""
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_RESPONSE = 2 * 1024 * 1024


class RateLimited(RuntimeError):
    """서버가 429(요청 많음)로 답했다. retry_after: 서버가 알려 준 대기 초(모르면 None). 위젯은 조회 간격을 늘린다."""

    def __init__(self, retry_after=None):
        super().__init__("요청 많음 · 잠시 뒤 다시 조회해요")
        self.retry_after = retry_after


def retry_seconds(value):
    """Retry-After 헤더의 초 값(0~3600). 날짜 형식이거나 없으면 None."""
    try:
        return min(3600, max(0, int(str(value).strip())))
    except (TypeError, ValueError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def http_json(url, headers, data=None):
    # Bearer tokens must never follow a redirect to another origin.
    if urlsplit(url).scheme != "https":
        raise ValueError("HTTPS required")
    req = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as r:
            raw = r.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(retry_seconds(e.headers.get("Retry-After") if e.headers else None)) from None
        raise
    if len(raw) > MAX_RESPONSE:
        raise ValueError("Response too large")
    return json.loads(raw.decode("utf-8"))
