#!/bin/bash
# QuotaTank 맥 설치: 최신 릴리스를 받아 SHA-256 을 확인하고 응용 프로그램 폴더에 넣은 뒤 실행한다.
#   curl -fsSL https://raw.githubusercontent.com/daowle/Quota-Tank/main/install.sh | bash
# 터미널(curl)로 받은 파일에는 브라우저 다운로드의 격리 표시가 붙지 않아 Gatekeeper 경고 없이 열린다.
# (소스에서 직접 돌리는 설치는 install_mac.sh)
set -euo pipefail

REPO="daowle/Quota-Tank"
ASSET="QuotaTank-mac-arm64.zip"
URL="https://github.com/$REPO/releases/latest/download"

[ "$(uname -s)" = Darwin ] || { echo "macOS 전용 설치 스크립트예요."; exit 1; }
[ "$(uname -m)" = arm64 ] || { echo "지금은 Apple Silicon(M1 이상) 맥만 지원해요."; exit 1; }

DEST="${QUOTATANK_DIR:-/Applications}"
[ -w "$DEST" ] || DEST="$HOME/Applications"
mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "QuotaTank 내려받는 중…"
curl -fL --progress-bar -o "$TMP/$ASSET" "$URL/$ASSET"
curl -fsSL -o "$TMP/$ASSET.sha256" "$URL/$ASSET.sha256"
if ! (cd "$TMP" && shasum -a 256 -c "$ASSET.sha256" >/dev/null); then
  echo "받은 파일 확인(SHA-256)에 실패했어요. 잠시 뒤 다시 시도해 주세요."
  exit 1
fi

ditto -x -k "$TMP/$ASSET" "$TMP/app"
pkill -x QuotaTank 2>/dev/null || true  # 켜져 있던 위젯은 끄고 바꾼다
rm -rf "$DEST/QuotaTank.app"
ditto "$TMP/app/QuotaTank.app" "$DEST/QuotaTank.app"
open "$DEST/QuotaTank.app"

echo
echo "설치 완료: $DEST/QuotaTank.app"
echo "화면 오른쪽 위 메뉴 막대에 사용률이 보여요(Dock 에는 뜨지 않아요)."
echo "처음 실행할 때 키체인 접근을 물으면 '항상 허용'을 누르세요(Claude Code 로그인 정보)."
