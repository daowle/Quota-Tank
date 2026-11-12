"""MSIX 패키지 만들기 (윈도우). 서명은 하지 않는다(SIGNING.md).

    set AIW_ONEDIR=1 && pyinstaller widget.spec --noconfirm        # dist/QuotaTank/ 폴더
    python tools/build_msix.py --version v1.2.3 --publisher "CN=..." --publisher-name "이름"

--layout-only 는 makeappx 없이 build/msix/layout 까지만 만든다(개발자 모드에서 Add-AppxPackage -Register 로 시험).
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "packaging" / "msix" / "AppxManifest.xml"
LOGOS = {"Square150x150Logo.png": 150, "Square44x44Logo.png": 44, "StoreLogo.png": 50}


def msix_version(tag):
    """v1.2.3 → 1.2.3.0. MSIX 는 네 자리 숫자이고, 스토어는 마지막 자리를 0 으로 둔다."""
    parts = re.fullmatch(r"v?(\d{1,5})\.(\d{1,5})\.(\d{1,5})", tag or "")
    if not parts or any(int(p) > 65535 for p in parts.groups()):
        raise ValueError(f"버전은 v1.2.3 모양이어야 해요: {tag!r}")
    return ".".join(parts.groups()) + ".0"


def render_manifest(version, publisher, publisher_name):
    if not re.fullmatch(r"CN=[^\r\n<>]{1,8000}", publisher):
        raise ValueError("Publisher 는 인증서 주체 그대로(CN=... 로 시작) 넣어야 해요")
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in (("VERSION", msix_version(version)), ("PUBLISHER", publisher),
                       ("PUBLISHER_NAME", publisher_name)):
        text = text.replace("{{" + key + "}}", escape(value, {'"': "&quot;"}))
    if re.search(r"\{\{[A-Z_]+\}\}", text):
        raise ValueError("채우지 않은 자리가 남았어요")
    return text


def make_logos(icon, assets):
    from PIL import Image
    assets.mkdir(parents=True, exist_ok=True)
    with Image.open(icon) as img:
        img = img.convert("RGBA")
        for name, size in LOGOS.items():
            img.resize((size, size), Image.LANCZOS).save(assets / name)


def find_makeappx():
    found = shutil.which("makeappx")
    if found:
        return found
    kits = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Windows Kits", "10", "bin")
    candidates = sorted(glob.glob(os.path.join(kits, "*", "x64", "makeappx.exe")))
    if not candidates:
        raise SystemExit("makeappx.exe 가 없어요 — Windows SDK 를 설치하세요 (GitHub windows 러너에는 있다)")
    return candidates[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--publisher", required=True, help="서명 인증서의 Subject, 예: CN=Hong Gildong, O=..., C=KR")
    ap.add_argument("--publisher-name", default="QuotaTank")
    ap.add_argument("--app", default=str(ROOT / "dist" / "QuotaTank"), help="AIW_ONEDIR=1 로 만든 폴더")
    ap.add_argument("--out", default=str(ROOT / "dist" / "QuotaTank.msix"))
    ap.add_argument("--layout-only", action="store_true")
    args = ap.parse_args()

    app = Path(args.app)
    if not (app / "QuotaTank.exe").is_file():
        raise SystemExit(f"{app} 에 QuotaTank.exe 가 없어요 — AIW_ONEDIR=1 로 pyinstaller 를 먼저 돌리세요")
    layout = ROOT / "build" / "msix" / "layout"
    if layout.exists():
        shutil.rmtree(layout)
    shutil.copytree(app, layout)
    make_logos(ROOT / "web" / "icon.ico", layout / "Assets")
    (layout / "AppxManifest.xml").write_text(render_manifest(args.version, args.publisher, args.publisher_name),
                                             encoding="utf-8")
    if args.layout_only:
        print(f"Layout ready: {layout}")
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([find_makeappx(), "pack", "/o", "/d", str(layout), "/p", str(out)], check=True)
    print(f"MSIX ready (unsigned): {out}")


if __name__ == "__main__":
    sys.exit(main())
