@AGENTS.md

## Claude Code 전용

- `.claude/settings.json` 이 실제 로그인 파일·앱 설정 폴더 읽기를 막는다(권한 규칙 + `.claude/hooks/guard_secrets.py`). 막히면 우회하지 말고 사용자에게 묻는다.
- `git push` 는 매번 확인을 받는다. 개인 설정은 `.claude/settings.local.json`(커밋하지 않음)에 둔다.
- 브로커·providers·hardening·web·widget.spec·release.yml 을 바꿨으면 끝내기 전에 `boundary-reviewer` 하위 에이전트로 점검한다.
