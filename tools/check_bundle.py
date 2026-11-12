"""Fail release builds that accidentally bundle private files or extra web assets.

    python tools/check_bundle.py dist/QuotaTank.exe      # onefile build
    python tools/check_bundle.py dist/QuotaTank           # onedir build (MSIX layout source)
"""
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hardening import ASSETS
from PyInstaller.archive.readers import CArchiveReader

PRIVATE = {"auth.json", ".credentials.json", "config.json", "history.jsonl", "cost_cache.json", ".env"}
# The credential broker must ship, or every usage query fails in the packaged app.
REQUIRED_MODULES = {"provider_worker", "providers.claude", "providers.codex", "providers.gemini", "providers.anthropic_api"}


def archive_names(exe):
    bundle = CArchiveReader(str(exe))
    names = {name.replace("\\", "/") for name in bundle.toc}
    modules = set()
    for name in names:
        if name.endswith(".pyz") or name == "PYZ-00.pyz":
            try:
                modules |= set(bundle.open_embedded_archive(name).toc)
            except Exception:
                pass
    return names, modules


target = Path(sys.argv[1])
if target.is_dir():
    files = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    names, modules = archive_names(target / "QuotaTank.exe")
    names |= files
    web_assets = {n.split("/web/", 1)[1] for n in files if "/web/" in n}
else:
    names, modules = archive_names(target)
    web_assets = {name[4:] for name in names if name.startswith("web/")}

bad = [name for name in names if PurePosixPath(name).name.lower() in PRIVATE
       or name.lower().endswith((".pem", ".key", ".pfx", ".p12"))]
missing = REQUIRED_MODULES - modules
if bad or web_assets != set(ASSETS) or missing:
    raise SystemExit(f"Unexpected bundle contents: private={bad}, web={web_assets.symmetric_difference(ASSETS)}, "
                     f"missing modules={sorted(missing)}")
print(f"Bundle checked: {len(names)} entries; {len(web_assets)} allowlisted web assets; "
      f"broker modules present; no private filenames.")
