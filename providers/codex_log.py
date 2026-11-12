"""Codex 사용률 형식 읽기: HTTP 응답·세션 로그의 rate_limits 를 기간·사용률·리셋 시각으로 맞춘다.
토큰과 무관해서 화면 프로세스(지난 기록 가져오기)와 브로커(providers.codex)가 같이 쓴다."""
from .meta import FIVE_H, WEEK


def windows(rl):
    """HTTP / CLI / App Server 창을 기간(초), 사용률, 리셋 시각으로 정규화한다."""
    out = []
    for slot in ("primary", "secondary"):
        w = rl.get(slot + "_window") or rl.get(slot)
        if not isinstance(w, dict):
            continue
        u = w.get("used_percent", w.get("usedPercent"))
        if u is None:
            continue
        seconds = w.get("limit_window_seconds")
        minutes = w.get("window_minutes", w.get("windowDurationMins"))
        try:
            seconds = float(seconds) if seconds is not None else float(minutes) * 60 if minutes is not None else None
            u = float(u)
            if not 0 <= u <= 100 or seconds is not None and not 0 < seconds < float("inf"):
                continue
        except (TypeError, ValueError):
            continue
        out.append((seconds, u, w.get("reset_at", w.get("resets_at", w.get("resetsAt")))))
    return out


def window_name(seconds):
    if seconds is None:
        return "기간 미확인"
    if seconds == WEEK:
        return "주간"
    if seconds % 86400 == 0:
        return f"{seconds / 86400:g}일"
    if seconds % 3600 == 0:
        return f"{seconds / 3600:g}시간"
    return f"{seconds / 60:g}분"


def main_limits(rl):
    ws = windows(rl)
    p = next((w for w in ws if w[0] == FIVE_H), None)
    s = next((w for w in ws if w[0] == WEEK), None)
    return {"u5": p[1] if p else 0.0, "r5": p[2] if p else None,
            "uw": s[1] if s else 0.0, "rw": s[2] if s else None,
            "has5": p is not None, "hasw": s is not None}


def is_5h(rl):
    """Codex 로그의 rate_limits 가 5시간 창인지. 30일 창(window_minutes=43200) 같은 다른 한도는 뺀다."""
    return rl.get("limit_id") in (None, "codex") and any(seconds == FIVE_H and r for seconds, _, r in windows(rl))
