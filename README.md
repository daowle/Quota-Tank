# QuotaTank

Claude / Codex(ChatGPT) / Gemini 구독의 5시간·주간 한도를 보여 주는 데스크톱 위젯.

이 저장소는 설치파일과 사용 안내를 배포합니다. 앱 소스코드는 별도 비공개 저장소에서 관리합니다.

## 다운로드

[최신 버전](https://github.com/daowle/Quota-Tank/releases/latest)에서 OS에 맞는 파일을 받으세요.
아직 Windows 코드 서명과 Apple 공증을 적용하지 않아 OS 경고가 나타날 수 있습니다.

### Windows 10 / 11

`QuotaTank-windows.exe`를 내려받아 원하는 폴더에서 실행합니다. Python 설치는 필요 없습니다.
WebView2 런타임이 필요합니다([Microsoft 다운로드](https://developer.microsoft.com/microsoft-edge/webview2/)).
PowerShell의 `Get-FileHash .\QuotaTank-windows.exe -Algorithm SHA256` 결과를 릴리스의 `.sha256`과 비교하세요.
서명한 MSIX를 제공하는 릴리스에서는 `.msix`로 설치할 수도 있습니다.

### macOS (Apple Silicon)

터미널에서 다음 명령으로 다운로드, SHA-256 확인, 설치와 실행을 진행합니다.

```sh
curl -fsSL https://raw.githubusercontent.com/daowle/Quota-Tank/main/install.sh | bash
```

이 스크립트는 Apple 공증을 대신하지 않습니다. 터미널 다운로드에는 브라우저의 격리 표시가 붙지 않습니다.
브라우저로 `QuotaTank-mac-arm64.zip`을 받았다면 앱을 응용 프로그램 폴더로 옮기고,
실행 경고 후 **시스템 설정 → 개인정보 보호 및 보안 → 그래도 열기**를 이용하세요.
Intel 맥용 설치파일은 아직 제공하지 않습니다.

## 계정 연결

Claude Code / Codex / Antigravity에 로그인한 계정을 사용합니다. 위젯은 해당 CLI를 함께 설치하지 않습니다.
자세히 보기의 **계정 관리 → 계정 추가 → 서비스 선택**(브라우저 로그인이 열립니다)에서 Claude와 Codex 추가 계정을 연결할 수 있습니다.
추가 계정의 로그인이 만료되면 해당 CLI 프로필에서 다시 로그인하세요.
macOS에서도 기본 Claude 계정은 조회할 수 있습니다. 다만 **Claude 추가 계정의 브라우저 로그인과
키체인 프로필 연결**은 현재 버전에 구현되어 있지 않습니다. 별도 `.credentials.json` 파일을 직접
연결하는 경로는 있으나, macOS에서의 실제 동작은 아직 검증하지 않았습니다.

## 사용법

- 드래그: 위치 이동. 더블클릭: 간단히 보기 / 자세히 보기.
- 트레이 또는 메뉴 막대 메뉴: 새로고침, 인사이트, 테마, 시작 시 실행, 종료.
- 켜진 상태에서 앱을 다시 실행하면 종료합니다. 업데이트 전에는 실행 중인 앱을 종료하세요.
- 리셋 알림과 Windows Claude 터미널 자동 이어가기는 메뉴에서 켜고 끕니다.
  자동 이어가기는 새 설치에서 기본으로 꺼져 있습니다. VS Code 확장 패널과 Codex 앱은 현재 대상이 아닙니다.
- 인사이트의 API 비용은 로컬 토큰 기록을 단가로 환산한 추정치이며 실제 청구액이 아닙니다.

## 데이터와 지원

[데이터 처리 안내](PRIVACY.md)를 확인하세요. 버그는 이 저장소의 Issues에 제보하세요.
로그인 파일, 토큰, 개인 대화 기록은 첨부하지 마세요.
이 앱은 OpenAI, Anthropic, Google의 공식 제품이 아닙니다.

## 라이선스

QuotaTank의 현재 라이선스는 [MIT](LICENSE)입니다. 소스 저장소의 비공개 전환은 기존 라이선스 권한을 취소하지 않습니다.
Pretendard 글꼴은 [SIL OFL 1.1](Pretendard-LICENSE.txt)입니다.
동봉된 외부 구성요소에는 각 구성요소의 라이선스가 적용됩니다.
