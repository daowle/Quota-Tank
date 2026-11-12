"""Claude Code 훅(.claude/hooks) 동작. 임시 git 저장소만 쓴다."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOREPLY = "1+me@users.noreply.github.com"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".claude" / "hooks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load("guard_publish")


class GuardPublishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = self.temp.name
        self.git("init", "-q", "-b", "main")
        self.commit(NOREPLY)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def commit(self, email):
        self.git("config", "user.name", "me")
        self.git("config", "user.email", email)
        self.git("commit", "-q", "--allow-empty", "-m", "x")

    def test_noreply_history_can_be_pushed(self):
        self.assertIsNone(guard.check("git push -u origin main", self.repo))
        self.assertIsNone(guard.check("git push origin :refs/tags/v0.1.0", self.repo))

    def test_personal_email_commit_blocks_push(self):
        self.commit("me@gmail.com")
        self.assertIn("me@gmail.com", guard.check("git push origin main", self.repo))
        self.assertIn("me@gmail.com", guard.check("cd x && git push", self.repo))

    def test_bulk_push_and_legacy_branches_are_blocked(self):
        for cmd in ("git push --all", "git push --mirror origin", "git push origin --tags",
                    "git push origin old-main", "git push origin hardening-broker-msix:main", "git -C . push origin old-main"):
            self.assertIsNotNone(guard.check(cmd, self.repo), cmd)

    def test_commit_needs_noreply_identity(self):
        self.assertIsNone(guard.check('git commit -m "x"', self.repo))
        self.assertIsNotNone(guard.check('git commit --author="me <me@gmail.com>" -m x', self.repo))
        self.assertIsNone(guard.check(f'git commit --author="me <{NOREPLY}>" -m x', self.repo))
        self.git("config", "user.email", "me@gmail.com")
        self.assertIn("me@gmail.com", guard.check("git add a && git commit -q -m x", self.repo))

    def test_other_commands_pass(self):
        self.assertIsNone(guard.check("git status && git log --oneline", self.repo))
        self.assertIsNone(guard.check("echo git push --all", self.repo))

    def test_hook_output_denies(self):
        event = json.dumps({"tool_input": {"command": "git push --all"}, "cwd": self.repo})
        out = subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / "guard_publish.py")],
                             input=event, capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


class RunTestsHookTest(unittest.TestCase):
    def test_ignores_non_python_and_outside_files(self):
        hook = str(ROOT / ".claude" / "hooks" / "run_tests.py")
        for path in (str(ROOT / "README.md"), str(Path(tempfile.gettempdir()) / "x.py")):
            out = subprocess.run([sys.executable, hook], input=json.dumps({"tool_input": {"file_path": path}}),
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.stdout, "", path)


if __name__ == "__main__":
    unittest.main()
