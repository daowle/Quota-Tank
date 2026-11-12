"""공유 계정 사용량 나누기: Claude 사용률(%) 중 이 PC 에서 내가 쓴 몫과 다른 곳(다른 사용자·다른 기기)의 몫을 추정한다.
설계와 실측은 docs/plans/shared-usage-split.md. 화면 프로세스 전용의 순수 함수만 둔다 — 파일·네트워크·토큰을 만지지 않는다.

서버는 사용률만 주고 토큰 수는 주지 않는다. 이 PC 의 Claude Code 기록에서 센 토큰을 API 단가로 환산한 금액 M 에
환산 계수 k(사용률 %p / 1달러)를 곱하면 내 몫이다:  내 몫 = min(사용률, k·M),  다른 곳 = 사용률 − 내 몫.
"""
import math

SPANS = ("h5", "week")
SPAN_SEC = {"h5": 5 * 3600, "week": 7 * 86400}
PLANS = ("pro", "max5", "max20")
MULT = {"pro": 1, "max5": 5, "max20": 20}
K_BASE = {"h5": 2.47, "week": 0.19}  # Pro 를 혼자 쓴 실측값(2026-10-08). 다른 요금제는 배수로 나눈다
FALLBACK_PRICE = (3, 15, 0.3, 3.75, 6)  # 단가를 모르는 모델($/1M 토큰: 입력·출력·캐시 읽기·캐시 쓰기 5분·1시간)
LEARN_MAX = 30       # 요금제·기간마다 모아 두는 지난 창
LEARN_MIN = 4        # 이만큼 모여야 배운 값을 쓴다
LEARN_QUANTILE = 0.2  # 다른 사용자는 사용률만 늘리므로 비율이 낮은 쪽이 '나만 쓴 창'에 가깝다
MIN_U, MIN_M = 5, 0.5          # 배우기: 너무 작은 창은 비율이 튄다
CAL_MIN_U, CAL_MIN_M = 10, 1   # 직접 맞추기


def family(model):
    return next((f for f in ("fable", "mythos", "opus", "sonnet", "haiku") if f in str(model)), "other")


def weigh(rows, price_of):
    """비용 집계의 Claude 응답 행 [날짜, 모델, 입력, 출력, 캐시읽기, 캐시쓰기5분, 캐시쓰기1시간, 시각] → [(시각, API 환산 USD, 모델 계열)] 시간순.
    price_of(model) 는 5개 단가 또는 None."""
    prices, out = {}, []
    for r in rows:
        try:
            t, model, tokens = float(r[7]), r[1] or "", r[2:7]
            if len(r) < 8 or not math.isfinite(t) or not all(type(n) in (int, float) and n >= 0 for n in tokens):
                continue
        except (IndexError, TypeError, ValueError):
            continue
        if model not in prices:
            prices[model] = price_of(model) or FALLBACK_PRICE
        out.append((t, sum(n * c for n, c in zip(tokens, prices[model])) / 1e6, family(model)))
    out.sort()
    return out


def spent(events, start, end):
    """[start, end] 동안 내가 쓴 API 환산 금액과 모델 계열별 금액."""
    by = {}
    for t, usd, fam in events:
        if start <= t <= end:
            by[fam] = by.get(fam, 0.0) + usd
    return sum(by.values()), by


def resolve_plan(setting, plan_name):
    """설정(auto·pro·max5·max20)과 서버가 알려 준 요금제 이름(Pro·Max)으로 요금제 단계를 정한다. 5배·20배는 서버 응답으로 구분되지 않는다."""
    if setting in PLANS:
        return setting
    return "pro" if str(plan_name or "").strip().lower() == "pro" else "max5"


def quantile(values, q):
    v = sorted(values)
    if not v:
        return None
    i = q * (len(v) - 1)
    lo = int(i)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (i - lo)


def _own(state, kind, plan, span):
    return ((state.get(kind) or {}).get(plan) or {}).get(span)


def _k_of(state, kind, plan, span):
    """이 요금제에서 직접 맞춘/배운 계수. 없으면 None."""
    mine = _own(state, kind, plan, span)
    if kind == "manual":
        return mine[0] / mine[1] if mine and mine[1] > 0 else None
    ratios = [u / m for u, m in mine or [] if m > 0]
    return quantile(ratios, LEARN_QUANTILE) if len(ratios) >= LEARN_MIN else None


def pick_k(state, plan, span):
    """(k, 출처). 직접 맞춤 > 혼자 쓸 때 배운 값 > 다른 요금제 값을 배수로 환산 > 기본값."""
    for kind, src in (("manual", "manual"), ("learned", "learned")):
        k = _k_of(state, kind, plan, span)
        if k:
            return k, src
    for kind in ("manual", "learned"):
        for other in PLANS:
            k = other != plan and _k_of(state, kind, other, span)
            if k:
                return k * MULT[other] / MULT[plan], "converted"
    return K_BASE[span] / MULT[plan], "base"


def split(u, m, k):
    """(내 몫, 다른 곳) 사용률 %p."""
    mine = min(u, k * m)
    return mine, u - mine


def _edit(state, kind, plan, span, fn):
    table = {p: {s: list(v) for s, v in spans.items()} for p, spans in (state.get(kind) or {}).items()}
    spans = table.setdefault(plan, {})
    spans[span] = fn(spans.get(span))
    return {**state, kind: table}


def learn(state, plan, span, u, m):
    """끝난 창 하나(마지막 사용률 u, 그 창에서 내가 쓴 금액 m)를 배우기 목록에 더한다. 너무 작은 창은 버린다."""
    if not (u >= MIN_U and m >= MIN_M):
        return state
    return _edit(state, "learned", plan, span, lambda old: ((old or []) + [[u, m]])[-LEARN_MAX:])


def calibrate(state, plan, span, u, m):
    """'나만 썼어요': 이 창의 비율을 직접 맞춘 값에 합친다. (새 상태, 받았는지)."""
    if not (u >= CAL_MIN_U and m >= CAL_MIN_M):
        return state, False
    return _edit(state, "manual", plan, span, lambda old: [(old[0] if old else 0) + u, (old[1] if old else 0) + m]), True


def reset_manual(state):
    return {k: v for k, v in state.items() if k != "manual"}


def clean_state(raw):
    """share_state.json 내용을 검사한다. 모양이 틀린 항목은 버린다."""
    ok = lambda x: type(x) in (int, float) and math.isfinite(x) and 0 <= x < 1e9
    out = {}
    manual = {}
    for plan, spans in (raw.get("manual") if isinstance(raw, dict) and isinstance(raw.get("manual"), dict) else {}).items():
        for span, v in (spans.items() if plan in PLANS and isinstance(spans, dict) else ()):
            if span in SPANS and isinstance(v, list) and len(v) == 2 and all(ok(x) for x in v) and v[1] > 0:
                manual.setdefault(plan, {})[span] = [float(v[0]), float(v[1])]
    learned = {}
    for plan, spans in (raw.get("learned") if isinstance(raw, dict) and isinstance(raw.get("learned"), dict) else {}).items():
        for span, items in (spans.items() if plan in PLANS and isinstance(spans, dict) else ()):
            if span in SPANS and isinstance(items, list):
                pairs = [[float(p[0]), float(p[1])] for p in items[-LEARN_MAX:]
                         if isinstance(p, list) and len(p) == 2 and all(ok(x) for x in p)]
                if pairs:
                    learned.setdefault(plan, {})[span] = pairs
    if manual:
        out["manual"] = manual
    if learned:
        out["learned"] = learned
    return out


def mine_series(rows, events, k, reset, limit=120):
    """현재 5시간 창의 내 몫 곡선 [[시각, 내 몫 %p]]. rows 는 사용률 기록 표본(t, u5, r5), events 는 시간순.
    표본마다 창 시작부터 쓴 금액에 k 를 곱하되 그 시각의 사용률을 넘지 않게 자른다."""
    key = int(round(reset / 60) * 60)  # 기록의 r5 는 분 단위로 맞춰 저장된다
    pts = [(r["t"], r["u5"]) for r in rows if r.get("r5") == key]
    if len(pts) > limit:
        step = -(-len(pts) // limit)
        pts = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
    start = reset - SPAN_SEC["h5"]
    pending = [e for e in events if e[0] >= start]
    out, i, total = [], 0, 0.0
    for t, u in pts:
        while i < len(pending) and pending[i][0] <= t:
            total += pending[i][1]
            i += 1
        out.append([round(t), round(min(u, k * total), 1)])
    return out


def window_share(u, reset, span, events, state, plan, now, end, rows=None):
    """현재 창 하나의 나누기. 창이 없거나 이미 끝났으면 None. rows 를 주면 5시간 창에 내 몫 곡선(series)을 더한다."""
    if reset is None or reset <= now or u is None:
        return None
    m, by = spent(events, reset - SPAN_SEC[span], end)
    k, src = pick_k(state, plan, span)
    mine, others = split(u, m, k)
    out = {"mine": round(mine, 1), "others": round(others, 1), "m": round(m, 2), "k": round(k, 4), "src": src,
           "models": {f: round(v, 2) for f, v in sorted(by.items(), key=lambda x: -x[1])[:4]}}
    if rows is not None and span == "h5":
        out["series"] = mine_series(rows, events, k, reset)
    return out


def summary(data, events, state, setting, now, rows=None):
    """화면용 요약: {"plan": 요금제 단계, "h5": 나누기|None, "week": 나누기|None}. 최신 값이 아니면(이전 기록) 나누지 않는다.
    rows(사용률 기록 표본)를 주면 5시간 창의 내 몫 곡선도 담는다."""
    plan = resolve_plan(setting, (data or {}).get("plan"))
    out = {"plan": plan, "h5": None, "week": None}
    if not data or data.get("src") != "live":
        return out
    end = min(now, data.get("observed_at") or now)
    if data.get("has5") is not False:
        out["h5"] = window_share(data.get("u5"), data.get("r5"), "h5", events, state, plan, now, end, rows)
    if data.get("hasw") is not False:
        out["week"] = window_share(data.get("uw"), data.get("rw"), "week", events, state, plan, now, end)
    return out


def past_windows(rows, events, state, setting, plan_name, now, limit=8):
    """인사이트용: 최근 5시간 창별 나누기. rows 는 위젯이 남긴 사용률 기록 표본(t, u5, r5), 오래된 창이 앞.
    각 창의 사용률은 위젯이 켜져 있는 동안 본 가장 높은 값이고, 지금의 환산 계수로 나눈다."""
    plan = resolve_plan(setting, plan_name)
    k, src = pick_k(state, plan, "h5")
    seen = {}
    for r in rows:
        key = r.get("r5")
        if key and r["t"] <= now:
            w = seen.setdefault(key, [0.0, 0.0])
            w[0], w[1] = max(w[0], r["u5"]), max(w[1], r["t"])
    out = []
    for key, (u, last) in sorted(seen.items())[-limit:]:
        used, _ = spent(events, key - SPAN_SEC["h5"], min(key, last))
        mine, others = split(u, used, k)
        out.append({"reset": key, "u": round(u, 1), "mine": round(mine, 1), "others": round(others, 1), "m": round(used, 2)})
    return {"plan": plan, "src": src, "k": round(k, 4), "windows": out}
