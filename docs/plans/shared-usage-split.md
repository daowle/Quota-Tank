# 공유 계정 사용량 나누기 — 구현 설계

Claude MAX 를 여러 사람이 같이 쓸 때, 5시간·주간 한도에서 **이 PC 에서 내가 쓴 몫**과 **다른 곳(다른 사용자·다른 기기)의 몫**을
나눠 보여 주는 기능. 다른 모델(코딩 에이전트)이 이 문서만 보고 구현할 수 있게 쓴다.
구현 전에 `AGENTS.md` 와 `SECURITY.md` 를 먼저 읽는다. 계산 로직의 시제품은 `tools/share_probe.py`(테스트 `tests/test_share_probe.py`)에 있다.

## 1. 원리

- 서버(usage API)는 **사용률 %만** 준다. 누가 썼는지, 토큰이 몇 개인지는 주지 않는다.
- 이 PC 에서 내가 쓴 토큰은 **정확히** 안다. Claude Code 가 요청마다 입력·출력·캐시 토큰을 `~/.claude/projects/**/*.jsonl` 에 남긴다
  (비용 탭의 `widget._scan_claude_file` 이 이미 읽는다).
- 모르는 것은 "100% 가 토큰으로 얼마인가" 하나다. 이를 **환산 계수 k**(사용률 %p / API 환산 1달러)로 둔다.

```
M        = 이번 창 시작(리셋 시각 − 창 길이)부터 지금까지 내 요청의 API 환산 금액 합(USD)
내 몫    = min(u_now, k · M)
다른 곳  = u_now − 내 몫
```

구간별 증가분을 따로 추적하지 않는다. 로그는 위젯이 꺼져 있던 동안에도 남으므로 **창 전체를 한 번에 뺀다.**
상대가 여러 명이어도 합산만 가능하다. 개인 식별은 하지 않는다.

토큰을 금액으로 바꾸는 이유: 모델과 토큰 종류(입력·출력·캐시)마다 한도를 깎는 정도가 다른데 규칙이 공개되지 않았다.
API 단가(`widget.PRICES`, `price_of`)가 그 비율의 가장 좋은 공개 대용이다. 단가를 모르는 모델은 `(3, 15, 0.3, 3.75, 6)`.

## 2. 실측 결과 (2026-10-08, 사용자 PC, 최근 14일)

Pro 요금제를 혼자 쓴 5시간 창 20개(거의 전부 Opus):

| k 구하는 방법 | 값(%p/$) | 판정 |
|---|---|---|
| 혼자 쓴 창 합산 `Σu / ΣM` | 2.47 | 기준값 |
| 창별 `u/M` 의 아래쪽 20% | 2.42 | 채택 — 기준값과 거의 같다 |
| 창 안 구간별 `Δu/ΔM` 의 아래쪽 20% | 1.97 | **탈락** — 혼자 쓴 창에 10~20%p 를 남의 몫으로 돌린다 |

- 창별 `u/M` 은 2.2 ~ 3.0 에 모였다. 혼자 쓴 창에서 "다른 곳"으로 잘못 잡힌 양은 대부분 0 ~ 4%p.
- 주간 창은 하나뿐이라 참고값만 있다: Pro 에서 `uw/M ≈ 0.19`.
- Max 5배로 바꾼 뒤 첫 창(공유 중, Opus 82% + Fable 18%): 사용률 35%, 내 사용 $19.11. `k = 2.47 / 5 = 0.494` 를 적용하면
  내 몫 9.4%p, 다른 곳 25.6%p.
- **확인하지 못한 것**: Max 가 정확히 Pro 의 5배·20배인지, Fable 등 다른 모델에도 단가 가중치가 맞는지, 주간 한도의 배수.

## 3. 계수 k 정하기

요금제 단계별로 k 를 따로 가진다. 우선순위는 위에서 아래로.

1. **직접 맞춘 값**: 사용자가 "이번 창은 나만 썼어요"를 누르면 그 창의 `u/M` 을 그 요금제의 k 로 저장한다.
   `u ≥ 10`, `M ≥ 1` 일 때만 받는다. 여러 번 누르면 `Σu/ΣM` 으로 합친다.
2. **혼자 쓸 때 배운 값**: 기능이 **꺼져 있는 동안**은 혼자 쓴다고 보고, 끝난 창마다 `u/M` 을 모아(`u ≥ 5`, `M ≥ 0.5`, 최근 30개)
   4개 이상이면 아래쪽 20% 값을 그 요금제의 k 로 둔다. 기능을 켜면 배우기를 멈춘다(공유 중인 창은 k 를 부풀린다).
3. **기본값**: `K5_BASE = 2.47`(Pro, 5시간), `KW_BASE = 0.19`(Pro, 주간)를 요금제 배수로 나눈다. 배수: `pro 1`, `max5 5`, `max20 20`.
   다른 요금제에서 배운 값이 있으면 기본값 대신 그 값을 배수 비율로 환산해 쓴다(예: Pro 에서 2.6 을 배웠으면 Max 5배는 0.52).

요금제 단계: usage 응답의 `plan` 은 `"Pro"`/`"Max"` 까지만 구분된다. 5배·20배는 설정 `share_plan`(`"auto"|"pro"|"max5"|"max20"`)으로
사용자가 고른다. `auto` 는 `plan == "Pro"` → `pro`, 그 밖은 `max5`. 브로커 응답에 요금제 단계를 더하는 것은 이번 범위가 아니다.

화면에는 k 의 출처를 같이 보인다: "직접 맞춤" / "혼자 쓸 때 학습" / "기본 추정".

## 4. 구현

### 4.1 `share.py` (새 파일, 저장소 루트)

화면 프로세스 전용의 순수 함수. 파일을 읽지 않고 `providers` 를 import 하지 않는다. `tools/share_probe.py` 의
`windows`·`quantile`·`k_from_windows`·`k_from_solo`·`split` 을 옮겨 다듬는다(옮긴 뒤 스크립트는 `share` 를 불러 쓰게 바꾼다).

```python
K_BASE = {"h5": 2.47, "week": 0.19}          # Pro, %p / USD
MULT = {"pro": 1, "max5": 5, "max20": 20}

def spent(events, start, end) -> tuple[float, dict]      # (M, 모델 계열별 금액). events: [(t, usd, 계열)] 시간순
def pick_k(state, plan, span) -> tuple[float, str]       # (k, "manual"|"learned"|"base")
def split(u, m, k) -> tuple[float, float]                # (내 몫, 다른 곳)
def learn(state, plan, span, u, m) -> dict               # 끝난 창 하나를 학습 목록에 더한 새 state
def calibrate(state, plan, span, u, m) -> dict           # 직접 맞추기
def summary(data, events, state, plan, now) -> dict
    # {"h5": {"mine", "others", "m", "k", "src"}, "week": {...}, "plan": plan}  — 창이 시작 전이면 해당 항목 None
```

### 4.2 `widget.py`

- **요청 시각**: `_scan_claude_file` 의 행 끝에 `round(t)` 를 더하고 `scan_costs` 의 캐시 `version` 을 3 으로 올린다.
  `cost_summary` 는 `r[2:7]` 로 읽으므로 결과가 그대로인지 기존 테스트로 확인한다.
- **최신 로그 반영**: 비용 캐시는 1시간마다 갱신된다(`import_loop`). 나누기는 분 단위로 맞아야 하므로, Claude 조회가 성공할 때
  최근 6시간 안에 바뀐 로그 파일만 다시 읽는 가벼운 갱신을 더한다(`scan_costs` 의 mtime·크기 비교를 그대로 쓴다).
  같은 응답이 여러 파일에 있을 수 있으니 `cost_summary` 처럼 `message.id:requestId` 로 한 번만 센다.
- **계산 시점**: `Core._fetch_all` 에서 Claude 의 `live` 응답을 `hist.add` 한 직후 `self.share = share.summary(...)`.
  창이 바뀌었으면(이전 `r5` 와 다름) 끝난 창을 `learn` 에 넘긴다(기능이 꺼져 있을 때만).
- **상태 파일**: `APP_DIR / "share_state.json"` — `{"manual": {plan: {"h5": [Σu, ΣM], "week": [...]}}, "learned": {plan: {"h5": [[u, M], ...]}}}`.
  `hardening.atomic_json` 으로 쓰고, 읽을 때 `hardening.finite` 로 범위를 검사한다. 깨졌으면 버리고 기본값으로 간다.
- **설정**: `DEFAULTS` 에 `"shared_split": False`, `"share_plan": "auto"`. `hardening.config_values` 에 `share_plan` 허용값 검사를 더한다(`theme` 과 같은 방식).
- **화면 데이터**: `svc["claude"]` 에 `share=self.share` 를, `cfg` 에 `shared_split`·`share_plan` 을 붙인다.
- **브리지**: `Api.toggle_shared_split`, `Api.set_share_plan(plan)`, `Api.calibrate_share()` 를 `_expose` 허용 목록에 더하고
  `tests/test_hardening.py` 의 `test_bridge_only_exposes_explicit_functions` 를 갱신한다.
- 기본 계정만 대상이다. 추가 계정(`accounts`)과 Codex 는 이번 범위가 아니다.

새로 읽는 파일·외부 요청·브로커 질문은 없다. 조회 간격(`next_poll`)을 건드리지 않는다.

### 4.3 화면

**`web/main.html` — Claude 카드** (기능이 켜져 있을 때만)

- 5시간 막대와 주간 막대 아래에 한 줄씩: `나 9% · 다른 곳 26%`. 막대는 기존 사용률 막대를 두 색으로 나눠 칠한다
  (나 = Claude 브랜드색, 다른 곳 = `--muted`).
- 툴팁(title): "이 PC 의 Claude Code 기록으로 추정 · 기본 추정/학습/직접 맞춤".
- 컴팩트 모드에서 숨길 수 있게 `compact_hidden` 항목 `share` 를 더한다.
- 메뉴: "공유 사용량 나눠 보기"(토글), 그 아래 "요금제: 자동 / Pro / Max 5배 / Max 20배", "이번 창은 나만 썼어요".
- 인라인 이벤트 속성·`eval`·외부 URL 금지(CSP). 웹 파일을 더하지 않으므로 `hardening.ASSETS`·`widget.spec` 은 그대로다.

**`web/insights.html`** — "사용률" 탭에 "공유" 박스: 최근 5시간 창별 나/다른 곳 스택 막대. 탭은 늘리지 않는다.
지난 창의 값은 `history.jsonl` 표본과 비용 캐시로 `insights_data` 에서 다시 계산한다(따로 저장하지 않는다).

## 5. 한계 (README 와 화면 도움말에 적는다)

- 추정치다. 실측 오차는 혼자 쓸 때 0 ~ 4%p 였다.
- "나"는 이 PC 의 Claude Code 사용만이다. 내가 다른 PC·폰·웹(claude.ai)에서 쓴 양은 "다른 곳"에 들어간다.
- 요금제를 바꾸면 계수가 달라진다. 요금제 설정이 틀리면 내 몫이 5배·20배 단위로 틀린다.
- 기록이 거의 Opus 뿐이라 다른 모델에서의 정확도는 확인하지 못했다.
- Anthropic 소비자 약관은 계정 공유를 허용하지 않는다. 기능은 기본으로 꺼 두고 도움말에 한 줄 적는다.

## 6. 테스트 (`tests/test_share.py`, 실제 파일·네트워크 없음)

1. 혼자 쓴 합성 창: `others < 2`.
2. 다른 사용자가 30%p 를 쓴 창: `others` 가 30 ± 4.
3. `k·M > u` 이면 `mine == u`, `others == 0`(음수가 나오지 않는다).
4. `pick_k` 우선순위: 직접 맞춤 > 학습 > 기본. 다른 요금제에서 배운 값의 배수 환산.
5. `learn` 은 4개 미만이면 기본값을 유지하고, 30개를 넘으면 오래된 것을 버린다.
6. `calibrate` 는 `u < 10` 이나 `M < 1` 을 거부한다.
7. 창이 시작 전(`r5` 없음)이면 `summary` 의 해당 항목이 `None`.
8. 깨진 `share_state.json` 은 기본값으로 돌아간다. `share_plan` 에 허용하지 않는 값이 오면 `auto`.
9. `_scan_claude_file` 행에 시각이 붙어도 `cost_summary` 결과가 같다.
10. `tests/test_broker.py` 의 import 검사가 `share` 를 더한 뒤에도 통과한다.
11. `tools/smoke_webview.py` 모의 데이터에 `share` 를 넣어 두 화면이 그려진다.

## 7. 마무리

- `README.md`(기능·한계·켜는 방법), `SECURITY.md`(이미 읽던 로컬 로그와 자체 기록만 쓰고 새 요청이 없다는 한 줄), `ROADMAP.md` 갱신.
- 전체 테스트와 두 화면의 스모크 테스트를 돌린다.
- `hardening.py`·`web/` 을 바꾸므로 끝내기 전에 `boundary-reviewer` 하위 에이전트로 점검한다.
- 실기 확인은 사용자가 한다: 혼자 쓰는 창에서 "다른 곳"이 5%p 아래인지, 상대가 쓴 시간대에 "다른 곳"이 오르는지.

## 8. 뒤로 미룬 것

- 모델 계열별 k(Fable 등에서 어긋나는 것이 확인되면). `share_probe.py` 의 "모델 계열별 u/m" 표로 판단한다.
- Codex(`_scan_codex_file` 의 `token_count` 에 시각이 있어 같은 방식으로 가능).
- 내 다른 기기의 사용량 합치기(ROADMAP 의 "기기별 사용량"과 함께).
- 브로커가 요금제 단계(5배·20배)를 알려 주게 하기.
