"""Tests for .gitignore rules to prevent hiding source code and ensure sensitive paths are ignored."""

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestGitIgnore(unittest.TestCase):
    def check_ignore(self, rel_path: str) -> bool:
        """Return True if git check-ignore matches rel_path, False otherwise."""
        res = subprocess.run(
            ["git", "check-ignore", "-q", rel_path],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        return res.returncode == 0

    def test_sensitive_paths_are_ignored(self):
        """Ensure sensitive files, credentials, local configs, and profile dirs are ignored."""
        sensitive_paths = [
            "dummy.keyring",
            "foo/bar.keyring",
            "config.local.toml",
            "state/main_identity.json",
            ".state/secret",
            "salt.txt",
            "secret.salt",
            "run.lock",
            "workers/worker-1",
            "worker_profiles/worker-1",
            "journals/test.journal",
            "logs/output.log",
        ]
        for path in sensitive_paths:
            with self.subTest(path=path):
                self.assertTrue(
                    self.check_ignore(path),
                    f"Sensitive path '{path}' should be ignored by .gitignore",
                )

    def test_source_and_code_paths_are_not_ignored(self):
        """Ensure source code, docs, tests, and nested packages (e.g. multigravity/workers/) are NOT ignored."""
        code_paths = [
            "multigravity/cli.py",
            "multigravity/doctor.py",
            "multigravity/workers/mod.py",
            "multigravity/state/mod.py",
            "tests/test_core.py",
            "docs/SPEC.md",
            "AGENTS.md",
            "pyproject.toml",
        ]
        for path in code_paths:
            with self.subTest(path=path):
                self.assertFalse(
                    self.check_ignore(path),
                    f"Code/doc path '{path}' should NOT be ignored by .gitignore",
                )


if __name__ == "__main__":
    unittest.main()
