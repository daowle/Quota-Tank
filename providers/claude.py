"""Claude (Claude Code 구독): ~/.claude/.credentials.json(맥은 키체인)의 OAuth 토큰 → Anthropic usage API."""
import json
import subprocess
import time
import urllib.error
from pathlib import Path

from .common import HOME, MAC, commit_swap, http_json, iso_to_epoch, read_json, write_next
from .meta import CLAUDE as _META, TOKEN_EXPIRED

KEY, NAME, STATUS_URL, LOGIN, DEFAULT_FILE = _META.KEY, _META.NAME, _META.STATUS_URL, _META.LOGIN, _META.DEFAULT_FILE


def credentials():
    """맥의 Claude Code 는 로그인 정보를 키체인("Claude Code-credentials")에 둔다. 없으면 파일."""
    if MAC:
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return json.loads(out.stdout)["claudeAiOauth"]
        except Exception:
            pass
    return read_json(DEFAULT_FILE)["claudeAiOauth"]


def valid(cred):
    """로그인 파일 내용이 이 서비스의 것인지."""
    try:
        return bool(cred["claudeAiOauth"]["accessToken"])
    except (KeyError, TypeError):
        return False


def email(path=None):
    """표시·중복 확인용 계정 이메일. 설정 폴더의 .claude.json(기본 계정은 ~/.claude.json)에 있다."""
    meta = Path(path).parent / ".claude.json" if path else HOME / ".claude.json"
    try:
        return (read_json(meta, limit=256 * 1024 * 1024).get("oauthAccount") or {}).get("emailAddress")
    except Exception:
        return None


def find_profiles():
    """홈 폴더의 다른 Claude 설정 폴더(~/.claude-work 등)에 있는 로그인 파일."""
    return [p for p in HOME.glob(".claude*/.credentials.json") if p.parent.name != ".claude"]


# 작업 계정 바꾸기에서 계정과 함께 옮기는 칸. mcpOAuth 같은 나머지 칸은 PC 에 묶여 있어 제자리에 둔다.
ACCOUNT_KEYS = ("claudeAiOauth", "trustedDeviceToken")
CONFIG_LIMIT = 256 * 1024 * 1024


def _config_file(folder):
    """설정 폴더의 계정 정보 파일. 기본 자리(~/.claude)는 홈의 ~/.claude.json 을 쓴다."""
    return HOME / ".claude.json" if Path(folder) == DEFAULT_FILE.parent else Path(folder) / ".claude.json"


def swap_files(folder):
    """맞바꿀 때 고쳐 쓰는 파일들: 두 자리의 로그인 파일과 계정 정보 파일."""
    return [DEFAULT_FILE, Path(folder) / ".credentials.json", _config_file(DEFAULT_FILE.parent), _config_file(folder)]


def switch_locks(folders):
    """Claude Code 가 토큰을 갱신할 때 잡는 잠금(<폴더>/.oauth_refresh.lock, <폴더>.lock, 60초)과
    계정 정보 파일을 고쳐 쓸 때 잡는 잠금(<파일>.lock, 10초). Claude Code 와 같은 순서로 잡는다."""
    homes = [DEFAULT_FILE.parent, *map(Path, folders)]
    creds = [(lock, 60) for h in homes for lock in (h / ".oauth_refresh.lock", h.parent / (h.name + ".lock"))]
    return creds + [(Path(str(_config_file(h)) + ".lock"), 10) for h in homes]


def swap(folder, parked):
    """기본 자리와 folder 의 로그인(ACCOUNT_KEYS)과 계정 정보(oauthAccount)를 맞바꾼다.
    parked: 바꾼 뒤 folder 가 원래 기본 계정의 로그인을 맡아 두는지."""
    cred_a, cred_b, conf_a, conf_b = finals = swap_files(folder)
    try:
        a, b = read_json(cred_a), read_json(cred_b)
    except (OSError, ValueError):
        a = b = None
    if not (valid(a) and valid(b)):
        raise RuntimeError("로그인 정보를 찾지 못했어요 · Claude Code 로그인을 확인하세요")
    try:
        ca, cb = (read_json(f, limit=CONFIG_LIMIT) if f.is_file() else {} for f in (conf_a, conf_b))
    except (OSError, ValueError):
        ca = cb = None
    if not (isinstance(ca, dict) and isinstance(cb, dict)):
        raise RuntimeError("Claude 계정 정보 파일(.claude.json)을 읽지 못했어요")

    def trade(mine, theirs, keys):
        out = {k: v for k, v in mine.items() if k not in keys}
        out.update((k, theirs[k]) for k in keys if k in theirs)
        return out
    for final, value in zip(finals, (trade(a, b, ACCOUNT_KEYS), trade(b, a, ACCOUNT_KEYS),
                                     trade(ca, cb, ("oauthAccount",)), trade(cb, ca, ("oauthAccount",)))):
        write_next(final, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
    commit_swap(folder, finals, parked)


def expired(cred, now=None):
    """Claude Code 가 저장한 토큰 만료 시각(expiresAt, ms)이 1분 안으로 다가왔는지. 값이 없으면 모른다고 보고 조회한다.
    만료된 토큰으로 usage 를 부르면 401 이 아니라 429(긴 Retry-After)가 와서, Claude Code 가 갱신한 뒤에도 그만큼 조회가 막힌다."""
    try:
        return float(cred["expiresAt"]) / 1000 <= (time.time() if now is None else now) + 60
    except (KeyError, TypeError, ValueError):
        return False


def fetch(path=None):
    cred = read_json(path)["claudeAiOauth"] if path else credentials()
    if expired(cred):  # 요청하지 않는다: 위젯은 poll_sec 마다 로그인 파일만 다시 읽다가 갱신되면 바로 조회한다
        raise RuntimeError(f"{TOKEN_EXPIRED} · Claude Code를 켜면 곧 다시 조회해요")
    try:
        d = http_json("https://api.anthropic.com/api/oauth/usage", {
            "Authorization": "Bearer " + cred["accessToken"],
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "ai-usage-widget/1.0",
        })
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError(f"{TOKEN_EXPIRED} — Claude Code를 한 번 실행하면 갱신돼요")
        raise
    fh, sd = window(d, "five_hour", "session"), window(d, "seven_day", "weekly_all")
    # 서버가 창을 주지 않으면(five_hour: null) 0% 로 채우지 않고 '없음'으로 알린다(화면에는 —)
    return {"u5": fh[0] if fh else 0.0, "r5": fh[1] if fh else None,
            "uw": sd[0] if sd else 0.0, "rw": sd[1] if sd else None,
            "has5": fh is not None, "hasw": sd is not None,
            "plan": (cred.get("subscriptionType") or "").capitalize(), "src": "live",
            "extra": extra_limits(d), "credit": cloud_credit(d)}


def window(d, key, kind):
    """한도 창의 (사용률, 초기화 epoch). 예전 칸(five_hour 등)을 먼저 보고, 없으면 limits 목록의 같은 종류를 본다.
    둘 다 없으면 None — 이 계정에 그 창이 없거나 서버가 지금 알려 주지 않는다."""
    b = d.get(key)
    if isinstance(b, dict) and b.get("utilization") is not None:
        return b["utilization"], iso_to_epoch(b.get("resets_at"))
    for lim in d.get("limits") or []:
        if isinstance(lim, dict) and lim.get("kind") == kind and lim.get("percent") is not None:
            return lim["percent"], iso_to_epoch(lim.get("resets_at"))
    return None


def cloud_credit(d):
    """클라우드 세션(Claude Code on the web) 크레딧. {"used_pct": 0~100 또는 None, "text": 표시 문자열, "r": 만료 epoch 또는 None}.
    usage 응답의 어느 필드에 오는지 아직 확인하지 못해서 지금은 None — 화면에서 줄이 숨겨진다(만료가 지나도 숨겨진다)."""
    return None


def extra_limits(d):
    """5시간·전체 주간 말고 따로 걸리는 한도(모델별 주간 등). 요금제에 없으면 빈 목록."""
    out = []
    seen = set()
    for lim in d.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        kind = str(lim.get("kind") or "")
        if kind in ("session", "weekly_all") or lim.get("percent") is None:
            continue
        scope = lim.get("scope")
        if isinstance(scope, dict):  # {"model": {"display_name": ...}, "surface": ...}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            scope = model.get("display_name") or scope.get("surface")
        scope = scope if isinstance(scope, str) else ""
        name = (scope or kind.replace("weekly_", "").replace("session_", "")).replace("_", " ").replace("-", " ").title()
        span = "5시간" if lim.get("group") == "session" or kind.startswith("session") else "주간" if lim.get("group") == "weekly" or kind.startswith("weekly") else "추가 한도"
        try:
            out.append({"name": f"{name} {span}", "u": float(lim["percent"]), "r": iso_to_epoch(lim.get("resets_at"))})
            seen.add(f"{name} {span}")
        except (TypeError, ValueError):
            continue
    for key, b in d.items():  # 모델별 한도는 서버가 제공한 항목만 표시한다
        if not key.startswith("seven_day_") or not isinstance(b, dict) or b.get("utilization") is None:
            continue
        name = key.removeprefix("seven_day_").replace("_", " ").title() + " 주간"
        if name in seen:
            continue
        try:
            out.append({"name": name, "u": float(b["utilization"]), "r": iso_to_epoch(b.get("resets_at"))})
        except (TypeError, ValueError):
            continue
    return out
