# 에이전트 작업 안내

Claude Code·Codex 등 모든 코딩 에이전트가 따르는 규칙. Claude Code 는 `CLAUDE.md` 가 이 파일을 불러온다.

## 이 저장소

Claude · Codex(ChatGPT) · Gemini 구독의 5시간·주간 한도를 보여 주는 데스크톱 위젯(Windows·macOS).
사용자 PC 의 CLI 로그인 정보로 각 서비스의 사용량 조회 API 를 부르고, 결과를 pywebview(WebView2/WKWebView) 화면과 트레이에 띄운다.

| 위치 | 하는 일 |
|---|---|
| `widget.py` | 화면 프로세스: 조회 일정(`next_poll`)·기록·트레이·창·JS 브리지(`Api`) |
| `provider_worker.py` | 자격 증명 브로커: 토큰을 읽는 유일한 프로세스. 파이프로 정해진 질문만 받는다 |
| `providers/<서비스>.py` | 토큰 읽기·사용량 조회(브로커에서만 불러온다) |
| `providers/meta.py`, `codex_log.py`, `net.py` | 토큰과 무관한 서비스 정보·로그 해석·HTTPS 요청(화면 프로세스가 쓴다) |
| `share.py`, `qr.py` | 순수 계산: 공유 사용량 나누기, 폰에서 보기 주소의 QR 코드 |
| `hardening.py` | 입력 경계: 설정·기록 검증, 사용량 값 정리, 로컬 웹 서버·CSP, 폰에서 보기 서버(Tailscale 주소 전용), 브리지 메시지 검사, 설정 폴더 |
| `web/` | 화면(HTML/CSS/JS). 파일 목록은 `hardening.ASSETS` 로 고정 |
| `tools/` | 번들 검사, WebView2 스모크 테스트, MSIX 만들기 |
| `tests/` | 단위 테스트(실제 계정·네트워크를 쓰지 않는다) |

설명 문서: 사용법 `README.md`, 보안 경계 `SECURITY.md`, 서명·MSIX `SIGNING.md`, 배포 `DISTRIBUTION.md`.

## 명령 (Windows, 저장소 루트)

```powershell
python -m venv .venv-release
.venv-release\Scripts\python -m pip install --require-hashes -r requirements-bootstrap.lock
.venv-release\Scripts\python -m pip install --require-hashes --no-build-isolation -r requirements-windows.lock

.venv-release\Scripts\python -m unittest discover -s tests          # 전체 테스트 — 변경마다 돌린다
.venv-release\Scripts\python tools/smoke_webview.py main.html        # 숨긴 WebView2 창 + 모의 데이터(화면을 바꿨을 때)
.venv-release\Scripts\python tools/smoke_webview.py insights.html
.venv-release\Scripts\python -m PyInstaller widget.spec --noconfirm  # 실행파일 이름은 widget.spec 의 name
.venv-release\Scripts\python tools/check_bundle.py dist/<name>.exe   # 개인 파일·웹 파일·브로커 모듈 검사
```

맥은 `pip install -r requirements-build.txt` 후 같은 명령(`python3`). 빌드 산출물(`build/`, `dist/`)과 임시 파일은 커밋하지 않는다.

## 반드시 지킬 것

### 1. 사용자 비밀을 읽지 않는다
- 실제 로그인 정보를 열거나 출력하거나 복사하지 않는다: `~/.claude*/.credentials.json`, `~/.codex*/auth.json`,
  윈도우 자격 증명 관리자·맥 키체인(`gemini:antigravity`, `Claude Code-credentials`), 앱 설정·기록 폴더
  (`hardening.app_dir()` — Windows `%APPDATA%`, 맥 `~/Library/Application Support` 아래 `AIUsageWidget`/`QuotaTank`).
- 테스트는 임시 폴더와 가짜 로그인 파일, `mock` 으로만 쓴다.
- 실제 API 확인이 꼭 필요하면 한 번만, 브로커를 거쳐 사용률 숫자만 출력하고 기록 쓰기는 끈다(`Core().hist.add = lambda *a: None`).
  조회도 서버의 요청 제한(429)에 들어간다.

### 2. 위젯을 마음대로 띄우지 않는다
- `pythonw widget.py` 나 실행파일을 다시 실행하면 이미 켜져 있는 사용자 위젯이 **꺼진다**(켜기/끄기 토글).
  화면 확인은 `tools/smoke_webview.py` 나 브라우저 headless 로 하고, 실제 실행은 사용자에게 먼저 묻는다.

### 3. 보안 경계를 유지한다 (`SECURITY.md`)
- 토큰을 읽는 코드는 `providers/<서비스>.py` 에만 두고 브로커만 불러온다. `widget.py` 는 `providers.meta`·`codex_log`·`net` 만 import 한다
  (`tests/test_broker.py` 가 확인한다).
- 브로커 질문(`fetch`·`inspect`·`profiles`·`default_email`)을 늘리면 `provider_worker.OPS` 와 `ProviderBroker.OPS`, 테스트를 같이 바꾼다.
  응답에는 숫자·이메일·유효 여부만 담는다. 사용량 값은 `hardening.usage_values` 를 거친다.
- JS 에서 부를 함수는 `Api._expose` 허용 목록에 넣고 테스트를 갱신한다. 밑줄 함수는 브리지에 내놓지 않는다.
- 웹 파일을 더하거나 빼면 `hardening.ASSETS`·`widget.spec` 의 datas·`tools/check_bundle.py` 를 함께 맞춘다.
  인라인 `<script>` 는 CSP 해시가 자동 계산된다. `eval`·인라인 이벤트 속성·외부 URL·외부 요청을 쓰지 않는다.
- 외부 요청은 `providers.net.http_json`(HTTPS·리디렉션 거부·크기 제한·429 구분)으로 한다. 조회 간격(`next_poll`)을 우회하는 요청을 만들지 않는다.

### 4. 의존성과 배포
- Windows 의존성은 버전과 해시를 고정한다(`requirements-windows.lock`). 바꾸면 잠금 파일과 `pip-audit` 결과를 같이 갱신한다.
- GitHub Actions 는 커밋 SHA 로 고정한다. 서명 비밀·인증서·개인 키를 저장소에 두지 않는다.

### 5. 여러 에이전트가 함께 일한다
- 다른 에이전트가 같은 작업 폴더에서 일하고 있을 수 있다. 고치기 전에 `git status`·`git log` 로 확인하고, 내가 만들지 않은 변경을 되돌리거나 덮어쓰지 않는다.
- 커밋에는 내가 바꾼 파일만 넣는다(`git add -A`·`git commit -a` 금지). `main` 이 아닌 브랜치에서 일한다.
- push·강제 push·히스토리 수정·브랜치 삭제는 사용자가 요청했을 때만 한다.

## 코드 스타일
- 주석과 화면 문구는 한국어. 주변 코드의 주석 밀도와 이름 짓는 방식을 따른다.
- 바꾼 동작마다 테스트를 더하고, 끝내기 전에 전체 테스트를 돌린다. 확인하지 못한 것(실기, 맥, 서명 등)은 그렇다고 보고한다.
- 사용자에게 보이는 동작이 바뀌면 `README.md`, 보안 경계가 바뀌면 `SECURITY.md` 를 같이 고친다.
- 커밋 메시지는 영어로 짧게: 무엇을 바꿨는지 한 줄, 필요하면 본문 1~2줄. 세부 내역은 쓰지 않는다.
