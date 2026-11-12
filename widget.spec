# PyInstaller 빌드 설정: `pyinstaller widget.spec`
#   윈도우 → dist/QuotaTank.exe (파일 하나, 설치 없음)
#            AIW_ONEDIR=1 이면 dist/QuotaTank/ 폴더 (MSIX 에 담는다: tools/build_msix.py)
#   맥     → dist/QuotaTank.app (Dock 에 안 뜨는 메뉴 막대 앱)
import os
import sys
from pathlib import Path

MAC = sys.platform == "darwin"
VERSION = os.environ.get("APP_VERSION", "0.0.0").lstrip("v")
ONEDIR = os.environ.get("AIW_ONEDIR") == "1"

a = Analysis(
    ["widget.py"],
    # Explicit allowlist: adding a credential/log file under web must not ship it.
    datas=[(str(Path("web") / name), str(Path("web") / name.parent)) for name in map(Path, (
        "main.html", "insights.html", "remote.html", "common.js", "common.css", "icon.ico", "reset.wav",
        "fonts/PretendardVariable.woff2", "fonts/LICENSE.txt"))],
    # pystray 는 실행 중에 OS별 백엔드를 import 해서 PyInstaller 가 못 찾는다
    # 서비스 모듈은 자격 증명 브로커에서만 늦게 import 해서(providers/__init__.py) 이름을 적어 둔다
    hiddenimports=["pystray._darwin" if MAC else "pystray._win32", "provider_worker",
                   "providers.claude", "providers.codex", "providers.gemini", "providers.anthropic_api"],
    excludes=["unittest", "pydoc", "test"],
)
pyz = PYZ(a.pure)

if MAC:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="QuotaTank",
              console=False, argv_emulation=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="QuotaTank")
    app = BUNDLE(
        coll, name="QuotaTank.app", icon="web/icon.ico", bundle_identifier="com.quotatank",
        version=VERSION,
        info_plist={"LSUIElement": True, "NSHighResolutionCapable": True,
                    "CFBundleShortVersionString": VERSION, "CFBundleDisplayName": "QuotaTank"},
    )
elif ONEDIR:
    # MSIX 는 설치 폴더(WindowsApps)를 OS 가 보호하므로, 실행할 때마다 임시 폴더에 푸는 파일 하나 방식보다 폴더가 낫다
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="QuotaTank",
              console=False, icon="web/icon.ico", upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="QuotaTank", upx=False)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="QuotaTank",
              console=False, icon="web/icon.ico", upx=False)
