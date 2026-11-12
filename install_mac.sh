#!/bin/bash
# 맥 설치: 가상환경(.venv-mac)에 pywebview·pystray를 깔고 ~/Applications 에 실행 아이콘을 만든다.
# 사용: bash install_mac.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

# 파이썬 3.11 이상. 맥 기본 /usr/bin/python3 는 3.9 라 requirements.txt 의 고정 버전(pillow 등)을 못 깐다
PY=""
for c in python3.13 python3.12 python3.11 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  p="$(command -v "$c" || true)"
  if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$p"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "파이썬 3.11 이상이 필요해요. https://www.python.org/downloads/ 에서 설치하거나 'brew install python' 후 다시 실행하세요."
  exit 1
fi
echo "사용할 파이썬: $PY ($("$PY" --version))"

rm -rf .venv-mac  # 예전 파이썬으로 만든 가상환경이 남아 있으면 새로 만든다
"$PY" -m venv .venv-mac
.venv-mac/bin/python -m pip install --upgrade pip >/dev/null
# 위젯이 맞춰 둔 버전(pywebview 의 내부 동작에 기대는 부분이 있다)
.venv-mac/bin/python -m pip install -r requirements.txt

APP="$HOME/Applications/QuotaTank.app"
mkdir -p "$HOME/Applications"
rm -rf "$APP" "$HOME/Applications/AI 사용량 위젯.app"  # 이름을 바꾸기 전에 만든 앱도 지운다
# 아이콘 클릭 = 켜기/끄기 (이미 떠 있으면 widget.py 가 알아서 끈다)
osacompile -o "$APP" -e "do shell script \"cd \" & quoted form of \"$DIR\" & \" && nohup .venv-mac/bin/python widget.py >/dev/null 2>&1 &\""

# 앱 아이콘 (실패해도 설치는 계속)
TMP="$(mktemp -d)"
if sips -s format png web/icon.ico --out "$TMP/icon.png" >/dev/null 2>&1; then
  mkdir -p "$TMP/icon.iconset"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/icon.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$TMP/icon.iconset" -o "$APP/Contents/Resources/applet.icns" 2>/dev/null || true
  touch "$APP"
fi
rm -rf "$TMP"

echo
echo "설치 완료: $APP"
echo "Launchpad/Spotlight 에서 'QuotaTank' 을 실행하세요. 한 번 더 실행하면 꺼집니다."
echo "처음 실행할 때 키체인 접근을 물으면 '항상 허용'을 누르세요 (Claude Code 로그인 정보)."
