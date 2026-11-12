"""PostToolUse 훅: 파이썬 파일을 고치면 전체 테스트(약 3초)를 돌리고, 실패할 때만 결과를 알린다.

테스트가 실제 설정 폴더에 닿지 않게 APPDATA·HOME·USERPROFILE 을 임시 폴더로 바꿔 돌린다.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def python():
    for venv in (".venv-release/Scripts/python.exe", ".venv-release/bin/python"):
        if (ROOT / venv).is_file():
            return str(ROOT / venv)
    return sys.executable


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    path = str((event.get("tool_input") or {}).get("file_path", ""))
    if not path.endswith(".py"):
        return
    try:
        Path(path).resolve().relative_to(ROOT)
    except ValueError:
        return  # 이 저장소 밖의 파일
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "APPDATA": home, "HOME": home, "USERPROFILE": home, "PYTHONIOENCODING": "utf-8"}
        try:
            out = subprocess.run([python(), "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=ROOT, env=env,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
        except subprocess.TimeoutExpired:
            out = None
    if out is not None and out.returncode == 0:
        return
    log = "테스트가 150초 안에 끝나지 않았다." if out is None else (out.stdout + out.stderr)[-3000:]
    print(json.dumps({"decision": "block",
                      "reason": f"{Path(path).name} 을 고친 뒤 테스트가 실패했다(여러 파일을 고치는 중이면 마저 고친 뒤 다시 본다):\n{log}"}))


if __name__ == "__main__":
    main()
