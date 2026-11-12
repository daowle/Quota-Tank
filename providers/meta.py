"""화면 프로세스가 쓰는 서비스 정보: 이름·장애 주소·계정 추가 방법. 로그인 파일·토큰을 읽는 코드는 여기 두지 않는다.
토큰은 자격 증명 브로커 프로세스(provider_worker.py)가 불러오는 providers.<서비스> 모듈만 읽는다."""
import sys
from pathlib import Path
from types import SimpleNamespace

HOME = Path.home()
TOKEN_EXPIRED = "토큰 만료"  # 브로커 오류 문구의 머리말. 화면 프로세스가 '켤 때 Claude 시작'을 할지 이것으로 본다
MAC = sys.platform == "darwin"
FIVE_H = 5 * 3600
WEEK = 7 * 86400

# KEY 는 기록·설정에 저장되므로 바꾸지 않는다
CLAUDE = SimpleNamespace(
    KEY="claude", NAME="Claude", STATUS_URL="https://status.claude.com/api/v2/summary.json",
    # 계정 추가: 위젯이 만든 빈 폴더를 CLAUDE_CONFIG_DIR 로 주고 공식 로그인을 띄우면, 기존 로그인과 섞이지 않는다
    LOGIN={"env": "CLAUDE_CONFIG_DIR", "cmd": ["claude", "auth", "login"], "file": ".credentials.json", "cli": "Claude Code"},
    DEFAULT_FILE=HOME / ".claude" / ".credentials.json")
CODEX = SimpleNamespace(
    KEY="gpt", NAME="Codex", STATUS_URL="https://status.openai.com/api/v2/summary.json",
    # 계정 추가: 위젯이 만든 빈 폴더를 CODEX_HOME 으로 주고 공식 로그인을 띄운다
    # (키체인 대신 auth.json 파일에 저장하게 해서 위젯이 읽을 수 있게 한다)
    LOGIN={"env": "CODEX_HOME", "cmd": ["codex", "-c", "cli_auth_credentials_store=file", "login"],
           "file": "auth.json", "cli": "Codex"},
    DEFAULT_FILE=HOME / ".codex" / "auth.json")
GEMINI = SimpleNamespace(
    KEY="gemini", NAME="Gemini",
    STATUS_URL=None,  # 공개된 상태 JSON 이 없다
    LOGIN=None,       # 계정 추가 미지원 (agy 가 로그인 정보를 한 곳에만 둔다)
    DEFAULT_FILE=None)

ALL = [CLAUDE, CODEX, GEMINI]
BY_KEY = {p.KEY: p for p in ALL}
