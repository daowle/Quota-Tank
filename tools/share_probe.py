"""공유 사용량 나누기 실험(docs/plans/shared-usage-split.md): 위젯 기록의 사용률(%)과 이 PC 의 Claude Code 로그 토큰을 맞춰 본다.
위젯에 넣기 전에 환산 계수 k(% / API 환산 USD)를 두 가지로 구해 비교하는 용도다. 숫자만 출력하고 아무것도 쓰지 않는다.

    python tools/share_probe.py                 # 5시간 창 목록 + 자동 추정 k
    python tools/share_probe.py --solo 3        # 3번 창을 "나만 썼다"로 보고 k 를 맞춘다(여러 개면 3,5)
    python tools/share_probe.py --k 0.494       # 계수를 직접 준다(요금제 배수로 환산한 값)
    python tools/share_probe.py --days 14 --history <history.jsonl> --logs <projects 폴더>
    python tools/share_probe.py --since "2026-10-09 00:43"   # 이 시각부터 나만 썼다고 보고 1%가 토큰·달러로 얼마인지 잰다
"""
import argparse
import ast
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIVE_H, WEEK = 5 * 3600, 7 * 86400
FALLBACK_PRICE = (3, 15, 0.3, 3.75, 6)  # 단가를 모르는 모델


def load_prices(source=ROOT / "widget.py"):
    """widget.py 의 PRICES 표만 읽는다(widget 을 import 하면 창·트레이 코드까지 불러온다)."""
    for node in ast.parse(Path(source).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "PRICES":
            return ast.literal_eval(node.value)
    return {}


def price_of(model, prices):
    best = max((k for k in prices if model.startswith(k)), key=len, default=None)
    return prices[best] if best else FALLBACK_PRICE


def family(model):
    return next((f for f in ("fable", "mythos", "opus", "sonnet", "haiku") if f in model), "기타")


def iso_to_epoch(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def scan_logs(root, since):
    """Claude Code 대화 기록 → {응답 키: (시각, 모델, 입력, 출력, 캐시읽기, 캐시쓰기5분, 캐시쓰기1시간)}. usage 숫자만 본다."""
    rows = {}
    for p in Path(root).rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < since:
                continue
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
                    t = iso_to_epoch(d.get("timestamp"))
                    if model == "<synthetic>" or not isinstance(u, dict) or not t or t < since:
                        continue
                    cw = u.get("cache_creation_input_tokens") or 0
                    w1 = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
                    rows[f"{m.get('id')}:{d.get('requestId')}"] = (
                        t, model, u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                        u.get("cache_read_input_tokens") or 0, max(cw - w1, 0), w1)
        except OSError:
            continue
    return rows


def weigh(rows, prices):
    """[(시각, API 환산 USD, 모델 계열)] 시간순."""
    out = []
    for t, model, *tok in rows:
        p = price_of(model, prices)
        out.append((t, sum(n * c for n, c in zip(tok, p)) / 1e6, family(model)))
    return sorted(out)


def load_history(path, since, svc="claude"):
    rows = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("s") == svc and r["t"] >= since and all(isinstance(r.get(k), (int, float)) for k in ("u5", "uw")):
                        rows.append(r)
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
    except OSError:
        pass
    return sorted(rows, key=lambda r: r["t"])


def windows(samples, events, reset="r5", used="u5", length=FIVE_H):
    """리셋 시각이 같은 표본끼리 한 창. 창마다 마지막 사용률 u 와, 창 시작부터 마지막 표본까지 내가 쓴 양 m.
    로그는 위젯이 꺼져 있던 동안에도 남으므로 창 시작부터 센다."""
    groups = defaultdict(list)
    for r in samples:
        if r.get(reset):
            groups[r[reset]].append(r)
    out = []
    for key, rs in sorted(groups.items()):
        start, last = key - length, rs[-1]
        mine = [e for e in events if start <= e[0] <= last["t"] + 60]
        by = defaultdict(float)
        for _, w, fam in mine:
            by[fam] += w
        out.append({"reset": key, "start": start, "seen": last["t"], "u": max(r[used] for r in rs),
                    "m": sum(by.values()), "by": dict(by), "samples": rs})
    return out


def quantile(values, q):
    v = sorted(values)
    if not v:
        return None
    i = q * (len(v) - 1)
    lo = int(i)
    return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (i - lo)


def k_from_windows(wins, q=0.2, min_u=5, min_m=0.5, min_n=4):
    """자동 추정 1: 창 전체 비율 u/m 의 아래쪽 분위수. 다른 사용자는 u 만 늘리므로 낮은 쪽이 '나만 쓴 창'에 가깝다."""
    ratios = [w["u"] / w["m"] for w in wins if w["u"] >= min_u and w["m"] >= min_m]
    return quantile(ratios, q) if len(ratios) >= min_n else None, len(ratios)


def k_from_bins(wins, events, q=0.2, step=3, gap=1800, min_m=0.2, min_n=6, lag=60):
    """자동 추정 2: 창 안에서 사용률이 step %p 오를 때마다 끊은 구간의 비율. 사용률이 정수로 올 수 있어 시간 대신 상승폭으로 끊는다."""
    ratios = []
    for w in wins:
        a = prev = w["samples"][0]
        for r in w["samples"][1:]:
            if r["t"] - prev["t"] > gap:  # 위젯이 꺼져 있던 구간
                a = r
            elif r["u5"] - a["u5"] >= step:
                m = sum(e[1] for e in events if a["t"] - lag < e[0] <= r["t"] - lag)
                if m >= min_m:
                    ratios.append((r["u5"] - a["u5"]) / m)
                a = r
            prev = r
    return quantile(ratios, q) if len(ratios) >= min_n else None, len(ratios)



def k_from_solo(wins, picks):
    """직접 맞추기: 고른 창은 나만 썼다고 보고 k = Σu / Σm."""
    chosen = [wins[i] for i in picks if 0 <= i < len(wins)]
    m = sum(w["m"] for w in chosen)
    return sum(w["u"] for w in chosen) / m if m > 0 else None


def split(w, k):
    """(내 몫 %, 다른 곳 %). k 를 모르면 None."""
    if k is None:
        return None, None
    mine = min(w["u"], k * w["m"])
    return mine, w["u"] - mine


def solo(samples, rows, prices, since, reset="r5", used="u5", lag=60):
    """since 부터 나만 썼다고 보고 창마다 오른 사용률 du 와 그동안 쓴 금액 m·토큰 tok(입력, 출력, 캐시 읽기, 캐시 쓰기).
    사용률은 정수로 오므로 숫자가 바뀐 순간끼리만 잰다: 처음 숫자가 오른 표본부터 마지막으로 오른 표본까지.
    steps 는 숫자가 한 번 오를 때마다의 1%p 당 금액. rows 는 scan_logs 의 값."""
    groups = defaultdict(list)
    for r in samples:
        if r.get(reset) and r["t"] >= since:
            groups[r[reset]].append(r)
    out = []
    for key, rs in sorted(groups.items()):
        ticks = [r for prev, r in zip(rs, rs[1:]) if r[used] > prev[used]]
        if len(ticks) < 2:
            out.append({"reset": key, "du": 0, "m": 0.0, "tok": [0, 0, 0, 0], "steps": []})
            continue
        costs, tok = [0.0] * (len(ticks) - 1), [0, 0, 0, 0]
        n = 0
        for t, model, i, o, cr, w5, w1 in sorted(rows):
            if not ticks[0]["t"] - lag < t <= ticks[-1]["t"] - lag:
                continue
            while t > ticks[n + 1]["t"] - lag:
                n += 1
            costs[n] += sum(x * c for x, c in zip((i, o, cr, w5, w1), price_of(model, prices))) / 1e6
            tok = [a + b for a, b in zip(tok, (i, o, cr, w5 + w1))]
        out.append({"reset": key, "du": ticks[-1][used] - ticks[0][used], "m": sum(costs), "tok": tok,
                    "steps": [c / (b[used] - a[used]) for c, a, b in zip(costs, ticks, ticks[1:])]})
    return out


def per_percent(wins, min_du=3):
    """사용률이 min_du %p 넘게 오른 창을 합쳐 1%p 당 (달러, 입력+출력 토큰, 캐시까지 합친 토큰). 모자라면 None."""
    wins = [w for w in wins if w["du"] >= min_du]
    du = sum(w["du"] for w in wins)
    if not du:
        return None
    tok = [sum(w["tok"][i] for w in wins) for i in range(4)]
    return sum(w["m"] for w in wins) / du, (tok[0] + tok[1]) / du, sum(tok) / du


def by_family(wins, min_u=5, share=0.9):
    """한 모델 계열이 90% 넘게 차지한 창만 모아 계열별 u/m. 계열마다 값이 크게 다르면 단가 가중치가 한도와 안 맞는 것이다."""
    out = defaultdict(list)
    for w in wins:
        if w["u"] < min_u or w["m"] <= 0:
            continue
        fam, top = max(w["by"].items(), key=lambda x: x[1])
        if top / w["m"] >= share:
            out[fam].append(w["u"] / w["m"])
    return {f: (quantile(v, 0.2), len(v)) for f, v in out.items()}


def fmt(x, unit="%", digits=1):
    return "   -  " if x is None else f"{x:5.{digits}f}{unit}"


def print_solo(samples, logs, prices, since):
    print(f"{datetime.fromtimestamp(since):%m-%d %H:%M} 부터 나만 쓴 구간 (토큰 단위: 백만)")
    for name, reset, used in (("5시간", "r5", "u5"), ("주간", "rw", "uw")):
        wins = solo(samples, logs, prices, since, reset, used)
        print(f"\n{name} 창\n  리셋 시각     잰 사용률    내 사용($)   입력   출력  캐시읽기  캐시쓰기")
        for w in wins:
            print(f"  {datetime.fromtimestamp(w['reset']):%m-%d %H:%M}  {fmt(w['du'])}   {w['m']:9.2f}  "
                  + "  ".join(f"{n / 1e6:6.2f}" for n in w["tok"]))
        steps = [s for w in wins for s in w["steps"]]
        if steps:
            print(f"  숫자가 한 번 오를 때마다 1%당 $: 가운데 {quantile(steps, 0.5):.2f} · 범위 {min(steps):.2f} ~ {max(steps):.2f} ({len(steps)}번)")
        pp = per_percent(wins)
        if pp:
            print(f"  → 1% = ${pp[0]:.3f} = 입력+출력 {pp[1] / 1e3:.1f}천 토큰 (캐시 포함 {pp[2] / 1e6:.2f}백만) · 100% = ${pp[0] * 100:.0f}"
                  f" · k = {1 / pp[0]:.3f}" if pp[0] > 0 else "  → 이 PC 에서 쓴 기록이 없어요.")
        else:
            print("  → 아직 숫자가 바뀐 순간 사이로 3%p 넘게 오른 창이 없어요.")


def main():
    from hardening import app_dir
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", default=str(app_dir() / "history.jsonl"))
    ap.add_argument("--logs", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--solo", default="", help="나만 쓴 창 번호(쉼표로 여러 개)")
    ap.add_argument("--since", default="", help='이 시각부터 나만 썼다고 보고 1%%당 토큰을 잰다(예: "2026-10-09 00:43")')
    ap.add_argument("--k", type=float, help="계수를 직접 준다(예: Pro 기준 2.47 을 Max 5배로 나눈 0.494). --solo 보다 우선")
    a = ap.parse_args()
    since = time.time() - a.days * 86400
    samples = load_history(a.history, since)
    prices = load_prices()
    logs = list(scan_logs(a.logs, since - WEEK).values())
    if a.since:
        print_solo(samples, logs, prices, datetime.fromisoformat(a.since).timestamp())
        return
    events = weigh(logs, prices)
    wins = windows(samples, events)
    if not wins:
        print("5시간 창 기록이 없어요. 위젯을 켠 채로 조금 더 쓴 뒤 다시 돌리세요.")
        return
    picks = [int(x) for x in a.solo.split(",") if x.strip().isdigit()]
    kw, nw = k_from_windows(wins)
    kb, nb = k_from_bins(wins, events)
    ks = a.k if a.k else k_from_solo(wins, picks)
    print(f"표본 {len(samples)}개 · 내 요청 {len(events)}건 · 5시간 창 {len(wins)}개 (최근 {a.days}일)\n")
    print("환산 계수 k (사용률 %p / API 환산 1달러)")
    print(f"  자동·창 단위   {fmt(kw, '', 3)}   (창 {nw}개)")
    print(f"  자동·구간 단위 {fmt(kb, '', 3)}   (구간 {nb}개)")
    print(f"  직접 맞추기    {fmt(ks, '', 3)}   (--solo {a.solo or '없음'})\n")
    print(" 번호  리셋 시각     사용률   내 사용($)  u/m    | 다른 곳: 자동·창  자동·구간  직접   | 모델 비중")
    for i, w in enumerate(wins):
        mix = " ".join(f"{f} {v / w['m'] * 100:.0f}%" for f, v in sorted(w["by"].items(), key=lambda x: -x[1])) if w["m"] else "-"
        ratio = w["u"] / w["m"] if w["m"] > 0 else None
        cells = "  ".join(fmt(split(w, k)[1]) for k in (kw, kb, ks))
        print(f"{'*' if i in picks else ' '}{i:3d}  {datetime.fromtimestamp(w['reset']):%m-%d %H:%M}  {fmt(w['u'])}  {w['m']:9.2f}  {fmt(ratio, '', 3)} |      {cells} | {mix}")
    fams = by_family(wins)
    if fams:
        print("\n모델 계열별 u/m (한 계열이 90% 이상인 창만, 아래쪽 20% 값)")
        for f, (k, n) in sorted(fams.items()):
            print(f"  {f:8s} {fmt(k, '', 3)}   (창 {n}개)")
        print("  계열끼리 값이 비슷하면 단가 가중치 하나로 충분하고, 크게 다르면 계열별 k 가 필요하다.")
    week = windows(samples, events, "rw", "uw", WEEK)
    if week:
        print("\n주간 창")
        for w in week:
            print(f"  리셋 {datetime.fromtimestamp(w['reset']):%m-%d %H:%M}  사용률 {fmt(w['u'])}  내 사용 ${w['m']:.2f}  u/m {fmt(w['u'] / w['m'] if w['m'] > 0 else None, '', 3)}")


if __name__ == "__main__":
    main()
