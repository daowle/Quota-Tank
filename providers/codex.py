"""Codex (ChatGPT 구독): ~/.codex/auth.json 토큰 → ChatGPT usage API, 실패하면 Codex 세션 로그의 rate_limits."""
import json
import subprocess
from pathlib import Path

from .codex_log import is_5h, main_limits, window_name, windows  # noqa: F401 (is_5h: 기존 이름 유지)
from .common import (FIVE_H, HOME, WEEK, RateLimited, commit_swap, http_json, iso_to_epoch, jwt_claims, read_json,
                     write_next)
from .meta import CODEX as _META

KEY, NAME, STATUS_URL, LOGIN, DEFAULT_FILE = _META.KEY, _META.NAME, _META.STATUS_URL, _META.LOGIN, _META.DEFAULT_FILE


def valid(cred):
    try:
        return bool(cred["tokens"]["access_token"] and cred["tokens"]["account_id"])
    except (KeyError, TypeError):
        return False


def email(path=None):
    """표시·중복 확인용 계정 이메일 (id_token 안에 있다)."""
    try:
        return jwt_claims(read_json(path or DEFAULT_FILE)["tokens"]["id_token"]).get("email")
    except Exception:
        return None


def find_profiles():
    """홈 폴더의 다른 Codex 폴더(~/.codex-work 등)에 있는 로그인 파일."""
    return [p for p in HOME.glob(".codex*/auth.json") if p.parent.name != ".codex"]


def swap_files(folder):
    """작업 계정을 바꿀 때 맞바꾸는 파일: 기본 자리와 folder 의 auth.json."""
    return [DEFAULT_FILE, Path(folder) / "auth.json"]


def switch_locks(folders):
    return []  # Codex 는 바깥에서 같이 잡을 잠금이 없다. 대신 switch_ready 가 실행 중인 Codex 를 막는다


def running():
    """실행 중인 Codex(CLI·VS Code 확장·앱)가 있는지. 켜져 있으면 갱신한 옛 계정 토큰을 바꾼 자리에 다시 쓸 수 있다."""
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    names = [line.split('","')[0].strip('"').lower() for line in out.stdout.splitlines()]
    return any(n.startswith("codex") and n.endswith(".exe") for n in names)


def switch_ready():
    if running():
        raise RuntimeError("실행 중인 Codex를 모두 끝낸 뒤 바꾸세요 · 터미널은 그대로 둬도 돼요")


def swap(folder, parked):
    """기본 자리와 folder 의 auth.json 을 통째로 맞바꾼다. parked: 바꾼 뒤 folder 가 원래 기본 계정을 맡아 두는지."""
    finals = swap_files(folder)
    try:
        ok = all(valid(read_json(f)) for f in finals)
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise RuntimeError("로그인 정보를 찾지 못했어요 · Codex 로그인을 확인하세요")
    raw = [f.read_bytes() for f in finals]
    for final, data in zip(finals, reversed(raw)):
        write_next(final, data)
    commit_swap(folder, finals, parked)


def extra_limits(d):
    """Codex 의 모델별 추가 한도(additional_rate_limits). 응답 모양이 일정하지 않아 아는 필드만 읽는다."""
    out = []
    items = d.get("additional_rate_limits") or []
    if isinstance(items, dict):
        items = [dict(v, limit_name=k) if isinstance(v, dict) else {} for k, v in items.items()]
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("limit_name") or it.get("metered_feature") or it.get("model") or "추가")
        rl = it.get("rate_limit") if isinstance(it.get("rate_limit"), dict) else it
        for seconds, u, r in windows(rl):
            out.append({"name": f"{name} {window_name(seconds)}", "u": u, "r": r})
    for seconds, u, r in windows(d.get("rate_limit") or {}):
        if seconds not in (FIVE_H, WEEK):
            out.append({"name": "Codex " + window_name(seconds), "u": u, "r": r})
    return out


def fetch(path=None):
    try:
        auth = read_json(path or DEFAULT_FILE)["tokens"]
        d = http_json("https://chatgpt.com/backend-api/wham/usage", {
            "Authorization": "Bearer " + auth["access_token"],
            "ChatGPT-Account-Id": auth["account_id"],
            "User-Agent": "codex_cli_rs/0.50.0",
        })
        rl = d["rate_limit"]
        return {**main_limits(rl),
                "plan": (d.get("plan_type") or "").capitalize(), "src": "live",
                "extra": extra_limits(d)}
    except RateLimited as limited:
        if path:
            raise
        # 기본 계정은 세션 로그 값을 보여 주되, 요청 많음이었다는 것도 알려 조회 간격을 늘리게 한다
        try:
            return {**fetch_from_sessions(), "retry_after": limited.retry_after or 0}
        except RuntimeError:
            raise limited from None
    except Exception:
        if path:  # 추가 계정이 기본 계정의 세션 로그 값을 보여 주면 안 된다
            raise RuntimeError("Codex 계정 조회 실패 · 로그인 상태를 확인하세요")
        return fetch_from_sessions()


def fetch_from_sessions():
    files = sorted((HOME / ".codex" / "sessions").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    latest, buckets = None, {}
    for f in files[:10]:
        for line in reversed(f.read_text(encoding="utf-8", errors="ignore").splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                rl = json.loads(line)["payload"]["rate_limits"]
            except Exception:
                continue
            if not isinstance(rl, dict) or not windows(rl):
                continue
            try:
                stamp = iso_to_epoch(json.loads(line).get("timestamp")) or f.stat().st_mtime
            except (TypeError, ValueError):
                continue
            key = rl.get("limit_id") or "codex"
            if key not in buckets or stamp > buckets[key][0]:
                buckets[key] = (stamp, rl)
            if key == "codex" and (latest is None or stamp > latest[0]):
                latest = (stamp, rl)
    if latest:
        extra = [{"limit_name": b.get("limit_name") or key, "rate_limit": b}
                 for key, (_, b) in buckets.items() if key != "codex"]
        return {**main_limits(latest[1]), "plan": "", "src": "log", "observed_at": latest[0],
                "extra": extra_limits({"rate_limit": latest[1], "additional_rate_limits": extra})}
    raise RuntimeError("Codex 로그인 정보/기록 없음")
