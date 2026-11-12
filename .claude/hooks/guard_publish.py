"""PreToolUse 훅: 공개 저장소에 개인 이메일이 담긴 커밋이 올라가지 않게 git push·commit 을 막는다.

로컬에는 예전 이메일로 만든 브랜치(old-main, hardening-broker-msix)가 남아 있다.
- push: --all·--mirror·옛 브랜치 이름을 막고, 올라갈 커밋(원격에 아직 없는 것)의 작성자·커미터 이메일이 noreply 인지 본다.
- commit: 이 저장소의 user.email 과 --author 가 noreply 인지 본다.
"""
import json
import os
import re
import shlex
import subprocess
import sys

LEGACY_REFS = ("old-main", "hardening-broker-msix")
ALLOWED_EMAIL = re.compile(r"@users\.noreply\.github\.com$|^noreply@anthropic\.com$", re.IGNORECASE)
FIELDS = ("command",)


def git(cwd, *args):
    """(종료 코드, 출력). git 을 못 돌리면 None."""
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.returncode, out.stdout.strip()


def push_refs(words):
    """`git push [옵션] [원격] [ref...]` 에서 올릴 ref(src 쪽)를 뽑는다. 원격 이름 뒤의 단어들이 ref 다."""
    args = [w for w in words if not w.startswith("-")]
    refs = []
    for w in args[1:]:  # args[0] 은 원격
        src = w.lstrip("+").split(":", 1)[0]
        if src:  # ":브랜치"(원격 삭제)는 올라가는 커밋이 없다
            refs.append(src)
    return refs


def check_push(words, cwd):
    flags = {w.split("=", 1)[0] for w in words if w.startswith("-")}
    if flags & {"--all", "--mirror", "--tags"}:
        return "git push --all·--mirror·--tags 는 쓰지 않는다. 올릴 브랜치·태그 이름을 적는다."
    head = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    refs = push_refs(words) or [head[1] if head and head[0] == 0 else "HEAD"]
    for ref in refs:
        if any(re.search(rf"(^|/){re.escape(name)}$", ref) for name in LEGACY_REFS):
            return f"{ref} 브랜치에는 예전 개인 이메일 커밋이 있어 올리지 않는다."
        log = git(cwd, "log", "--format=%ae%n%ce", ref, "--not", "--remotes=origin")
        if log is None:
            return "git 을 실행하지 못해 올라갈 커밋을 확인할 수 없다."
        if log[0] != 0:
            continue  # ref 를 못 찾으면 push 도 실패한다
        bad = sorted({e for e in log[1].splitlines() if e and not ALLOWED_EMAIL.search(e)})
        if bad:
            return f"{ref} 에 noreply 가 아닌 이메일로 만든 커밋이 있다: {', '.join(bad)}"
    return None


def check_commit(words, cwd):
    for i, w in enumerate(words):
        author = w.split("=", 1)[1] if w.startswith("--author=") else words[i + 1] if w == "--author" and i + 1 < len(words) else None
        if author is not None:
            m = re.search(r"<([^>]*)>", author)
            if not (m and ALLOWED_EMAIL.search(m.group(1))):
                return "--author 에는 GitHub noreply 이메일만 쓴다."
    config = git(cwd, "config", "user.email")
    if config is None:
        return "git 을 실행하지 못해 커밋 작성자를 확인할 수 없다."
    email = config[1]
    if not ALLOWED_EMAIL.search(email):
        return f"이 저장소의 git user.email 이 noreply 가 아니다({email or '없음'}). git config user.email 을 noreply 주소로 둔다."
    return None


def check(command, cwd):
    """명령 안의 git push·commit 을 하나씩 본다. 막아야 하면 이유를 돌려준다."""
    for part in re.split(r"&&|\|\||[;|\n]", command):
        try:
            words = shlex.split(part)
        except ValueError:  # 따옴표가 안 맞으면 공백으로만 나눈다
            words = part.split()
        while words and ("=" in words[0] and not words[0].startswith("-")):  # VAR=값 git ...
            words = words[1:]
        if len(words) < 2 or not re.fullmatch(r"(.*[/\\])?git(\.exe)?", words[0]):
            continue
        rest = words[1:]
        while rest and rest[0] in ("-c", "-C") and len(rest) > 1:  # git -c x=y push, git -C dir push
            rest = rest[2:]
        if not rest:
            continue
        reason = check_push(rest[1:], cwd) if rest[0] == "push" else check_commit(rest[1:], cwd) if rest[0] == "commit" else None
        if reason:
            return reason
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    command = str((event.get("tool_input") or {}).get("command", ""))
    if "git" not in command:
        return
    cwd = event.get("cwd") or ""
    if not os.path.isdir(cwd):
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or None
    reason = check(command, cwd)
    if not reason:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "공개 저장소 보호: " + reason,
    }}))  # ASCII 이스케이프: 윈도우 콘솔 코드 페이지와 상관없이 그대로 읽힌다


if __name__ == "__main__":
    main()
