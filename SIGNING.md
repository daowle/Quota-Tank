# 코드 서명과 MSIX

## 무엇을 막나

- **코드 서명**: 사용자가 실행파일의 게시자를 확인할 수 있다. 배포 후 파일이 한 바이트라도 바뀌면 서명이 깨져 윈도우가 "서명이 올바르지 않음"으로 표시한다.
  서명했다고 SmartScreen 경고가 바로 사라지지는 않는다. 스토어 밖에서 배포한 새 앱은 다운로드 기록이 쌓일 때까지 경고가 뜰 수 있다(Microsoft 문서).
- **MSIX**: OS가 보호하는 폴더(WindowsApps)에 설치되고 깔끔하게 제거된다. 매니페스트의 `uap10:PackageIntegrity`(Enforcement="on")를 켜 두어,
  설치 후 파일이 바뀌거나 깨지면 윈도우가 실행 전에 막고 복구한다(Windows 10 2004 이상). MSIX는 서명이 없으면 일반 사용자가 설치할 수 없다.

## 인증서 받는 방법 (2026-10 Microsoft 문서 기준, 한국 개인 개발자 관점)

| 방법 | 비용 | 한국에서 | 이 저장소 준비 상태 |
|---|---|---|---|
| **Microsoft Store로 MSIX 배포** | 개발자 계정 무료 | 개인·회사 계정 모두 가능 | 스토어가 대신 서명한다. 인증서가 필요 없다. Partner Center가 주는 Publisher 값을 `MSIX_PUBLISHER`에 넣고 서명 없는 MSIX를 올린다 |
| **Azure Artifact Signing** (구 Trusted Signing) | Basic 월 약 10달러 | 공개 신뢰 인증서는 한국 **조직**은 가능, **개인**은 미국·캐나다 거주자만 | `release.yml`의 `sign` 작업이 이 방식이다. 변수·비밀만 넣으면 된다 |
| **인증 기관 OV 코드 서명 인증서** (Sectigo, DigiCert, SSL.com, Certum 등) | 연 단위 유료 | 개인 신원 확인 가능 | 2023년 6월부터 개인 키를 하드웨어 토큰이나 업체 클라우드 HSM에 두어야 한다. `.pfx` 파일을 GitHub 비밀에 넣는 방식은 쓸 수 없다. 토큰을 꽂고 로컬에서 서명하거나, 업체 클라우드 서명 도구로 `sign` 작업 단계를 바꿔야 한다 |

개인으로 시작한다면 **스토어 배포(MSIX)** 가 비용 없이 서명·자동 업데이트까지 해결한다. GitHub 릴리스의 exe도 서명하려면 사업자(조직)로 Artifact Signing을 쓰거나 OV 인증서를 산다.
스토어 심사에서는 `runFullTrust` 권한을 쓰는 이유(다른 CLI의 로그인 파일 읽기, CLI 로그인 창 띄우기, 트레이)를 설명해야 한다.

## Artifact Signing으로 태그 빌드에 서명하기

1. Azure 구독에서 Artifact Signing 계정을 만든다. 계정 지역의 엔드포인트를 기록한다(예: 한국 중부 `https://krc.codesigning.azure.net/`).
2. 신원 확인(Identity validation)을 마치고 Public Trust 인증서 프로필을 만든다. 프로필에 나오는 인증서 주체(Subject)를 기록한다.
3. Microsoft Entra에 앱을 등록하고 페더레이션 자격 증명을 추가한다. 주체는 `repo:<소유자>/<저장소>:environment:release`이다.
   이 앱에 인증서 프로필의 **Artifact Signing Certificate Profile Signer** 역할을 준다. 비밀 키는 만들지 않는다(OIDC).
4. GitHub 저장소 설정
   - Environments에 `release`를 만들고 승인자(required reviewers)를 지정한다. 서명 작업은 승인 후에만 Azure 자격을 받는다.
   - Secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
   - Variables: `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE`, `MSIX_PUBLISHER`(2의 Subject를 글자 하나까지 그대로), `MSIX_PUBLISHER_NAME`(표시 이름)
5. `v1.2.3` 태그를 올리면 build → (승인) sign → 초안 릴리스 순서로 돈다. sign은 저장소 코드를 실행하지 않고 내려받은 파일에만 서명한다.
   서명 후 `signtool verify /pa`로 확인하고 SHA-256을 다시 만든다. 서명이 실패하면 릴리스를 만들지 않는다.

변수가 없으면 sign 작업을 건너뛰고 서명 없는 exe만 릴리스에 올린다. 서명 없는 MSIX는 Actions 아티팩트(`Windows-msix-unsigned`)로만 남는다.
MSIX 버전은 태그에서 만든다(`v1.2.3` → `1.2.3.0`). `v1.2.3-beta` 같은 태그는 MSIX 버전으로 바꿀 수 없어 빌드가 실패한다.

## 로컬에서 MSIX 시험 (서명 없이, 개발자 모드)

```powershell
$env:AIW_ONEDIR = "1"; .venv-release\Scripts\python -m PyInstaller widget.spec --noconfirm; Remove-Item Env:AIW_ONEDIR
.venv-release\Scripts\python tools/check_bundle.py dist/QuotaTank
.venv-release\Scripts\python tools/build_msix.py --version v0.0.1 --publisher "CN=QuotaTank Dev" --layout-only
Add-AppxPackage -Register build\msix\layout\AppxManifest.xml   # 설정 > 개발자용 > 개발자 모드 필요
# 시험 후 제거
Get-AppxPackage QuotaTank | Remove-AppxPackage
```

`.msix` 파일을 만들려면 Windows SDK의 `makeappx.exe`가 필요하다(`--layout-only` 없이 실행). GitHub windows 러너에는 기본으로 있다.

## MSIX 전환 호환성 검토

| 기능 | MSIX에서 달라지는 점 | 상태 |
|---|---|---|
| 로그인 파일 읽기 (`~/.claude`, `~/.codex`, `~/.claude.json`) | 홈 폴더는 파일 가상화 대상이 아니다 | 그대로 동작 예상 · 실기 확인 필요 |
| Gemini 로그인(자격 증명 관리자) | `runFullTrust` 앱은 일반 앱처럼 읽는다 | 그대로 동작 예상 · 실기 확인 필요 |
| 시작 시 실행 | 시작 프로그램 폴더에 쓴 파일이 패키지 전용 위치로 옮겨져 동작하지 않는다 | **반영함**: 매니페스트에 StartupTask(기본 꺼짐)를 넣고, 메뉴가 "시작 앱 설정 열기"로 바뀐다 |
| 설정·기록 (`%APPDATA%\QuotaTank`) | AppData 쓰기가 패키지 전용 위치로 리디렉션되고, 제거하면 같이 지워진다 | exe 버전과 기록이 나뉠 수 있다 · 실기 확인 후 필요하면 가져오기 기능 추가 |
| 추가 계정 로그인 (`claude auth login`, `codex login`을 숨긴 콘솔로) | 자식 프로세스도 패키지 안에서 돈다. 로그인 결과는 홈 폴더(`CLAUDE_CONFIG_DIR`/`CODEX_HOME`)에 저장된다 | 동작 예상. CLI가 AppData에 쓰는 캐시·업데이트는 위젯 패키지 안에 갇힐 수 있다 · 실기 확인 필요 |
| 트레이 아이콘·알림 | 일반 Win32 알림 영역 API | 그대로 동작 예상 · 실기 확인 필요 |
| 자격 증명 브로커·터미널 입력 보조 프로세스 | 패키지 안의 자기 실행파일을 다시 실행한다. 보조 프로세스는 `AttachConsole`로 다른 콘솔에 입력한다 | 브로커는 폴더 빌드 실행파일로 파이프 통신을 확인했다. 패키지 설치 상태의 콘솔 입력은 실기 확인 필요 |
| 단일 실행 잠금 (`Local\QuotaTank`) | full trust 앱은 이름 공간이 분리되지 않는다 | exe 버전과 MSIX 버전도 동시에 뜨지 않는다(의도대로) |
| 파일 하나 빌드의 임시 폴더 풀기 | 폴더 빌드(`AIW_ONEDIR=1`)를 담아 쓰지 않는다 | 반영함 |
| 자동 업데이트 | 스토어 또는 `.appinstaller` 파일로 가능 | 아직 없음 |

"실기 확인 필요" 항목은 서명한 MSIX를 깨끗한 윈도우 계정에 설치해 확인한다. 위 개발자 모드 등록(`-Register`)으로도 패키지 ID와 가상화가 적용되므로 먼저 시험할 수 있다.
