"""PreToolUse 훅: 실제 로그인 파일·키체인·앱 설정 폴더를 읽는 도구 호출을 막는다.

권한 규칙(Read deny)은 Bash·PowerShell 로 여는 경로까지 막지 못해서, 명령과 경로 인자를 직접 본다.
코드 검색은 막지 않도록 Grep 의 검색어는 보지 않고 경로만 본다. 테스트의 임시 auth.json 은 .codex 폴더 밖이라 통과한다.
"""
import json
import re
import sys

PATTERNS = [
    r"\.claude[\w.-]*[/\\]+\.credentials\.json",                 # Claude Code 로그인
    r"\.codex[\w.-]*[/\\]+auth\.json",                            # Codex 로그인
    r"gemini:antigravity|Claude Code-credentials",                # 자격 증명 관리자·키체인 항목
    r"find-generic-password|cmdkey\b|CredRead|Get-StoredCredential",
    r"(AIUsageWidget|QuotaTank)[/\\]+(config\.json|history\.jsonl|cost_cache\.json|claude_past\.json|import_state\.json|webview)",
]
FIELDS = ("command", "file_path", "path", "notebook_path")


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    args = event.get("tool_input") or {}
    text = "\n".join(str(args.get(k, "")) for k in FIELDS)
    hit = next((p for p in PATTERNS if re.search(p, text, re.IGNORECASE)), None)
    if not hit:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "사용자의 실제 로그인 정보·앱 설정 폴더는 읽지 않는다(AGENTS.md 1번). "
                                    "테스트는 임시 폴더와 mock 을 쓰고, 코드 검색은 Grep 도구로 한다.",
    }}))  # ASCII 이스케이프: 윈도우 콘솔 코드 페이지와 상관없이 그대로 읽힌다


if __name__ == "__main__":
    main()
