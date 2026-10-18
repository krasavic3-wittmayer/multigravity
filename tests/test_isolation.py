"""Unit tests for PathGuard, Environment allow-list, identity hashing, and profile management."""

import os
import tempfile
import unittest
from pathlib import Path

from multigravity.isolation import (
    EnvironmentAllowList,
    PathGuard,
    PathGuardError,
    WorkerProfileManager,
    extract_email_from_token_payload,
    hash_identity,
    mask_email,
)


class TestPathGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake_home = Path(self.tmp.name) / "fake_home"
        self.fake_home.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reject_real_home_and_parents(self):
        with self.assertRaises(PathGuardError):
            PathGuard.validate_worker_profiles([self.fake_home], real_home=self.fake_home)

        parent = self.fake_home.parent
        with self.assertRaises(PathGuardError):
            PathGuard.validate_worker_profiles([parent], real_home=self.fake_home)

    def test_reject_gemini_config_dir(self):
        gemini_dir = self.fake_home / ".gemini"
        gemini_dir.mkdir()
        with self.assertRaises(PathGuardError):
            PathGuard.validate_worker_profiles([gemini_dir], real_home=self.fake_home)

        sub_gemini = gemini_dir / "antigravity-cli"
        sub_gemini.mkdir()
        with self.assertRaises(PathGuardError):
            PathGuard.validate_worker_profiles([sub_gemini], real_home=self.fake_home)

    def test_reject_duplicate_and_overlapping_worker_paths(self):
        w1 = Path(self.tmp.name) / "workers" / "w1"
        w2 = Path(self.tmp.name) / "workers" / "w1"
        with self.assertRaises(PathGuardError):
            PathGuard.validate_worker_profiles([w1, w2], real_home=self.fake_home)

        parent_w = Path(self.tmp.name) / "workers" / "w1"
        child_w = Path(self.tmp.name) / "workers" / "w1" / "nested"
        with self.assertRaises(PathGuardError):
            PathGuard.validate_worker_profiles([parent_w, child_w], real_home=self.fake_home)

    def test_accept_isolated_distinct_paths(self):
        w1 = Path(self.tmp.name) / "workers" / "worker-1"
        w2 = Path(self.tmp.name) / "workers" / "worker-2"
        w3 = Path(self.tmp.name) / "workers" / "worker-3"
        validated = PathGuard.validate_worker_profiles([w1, w2, w3], real_home=self.fake_home)
        self.assertEqual(len(validated), 3)


class TestEnvironmentAllowList(unittest.TestCase):
    def test_allow_list_filters_sensitive_variables(self):
        dirty_env = {
            "PATH": "/usr/bin:/bin",
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            "GEMINI_API_KEY": "secret-gemini-key",
            "ANTHROPIC_API_KEY": "secret-claude-key",
            "GH_TOKEN": "secret-github-token",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json",
            "RANDOM_PASSWORD": "password123",
            "USER_SECRET_AUTH": "auth-token",
        }
        clean_env = EnvironmentAllowList.filter_environment(parent_env=dirty_env)

        self.assertIn("PATH", clean_env)
        self.assertIn("TERM", clean_env)
        self.assertIn("LANG", clean_env)

        self.assertNotIn("GEMINI_API_KEY", clean_env)
        self.assertNotIn("ANTHROPIC_API_KEY", clean_env)
        self.assertNotIn("GH_TOKEN", clean_env)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", clean_env)
        self.assertNotIn("RANDOM_PASSWORD", clean_env)
        self.assertNotIn("USER_SECRET_AUTH", clean_env)

    def test_build_worker_env_sets_profile_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_root = Path(tmp_dir) / "worker-1"
            env = EnvironmentAllowList.build_worker_env(profile_root)

            self.assertEqual(env["HOME"], str(profile_root / "home"))
            self.assertEqual(env["XDG_CONFIG_HOME"], str(profile_root / "config"))
            self.assertEqual(env["XDG_DATA_HOME"], str(profile_root / "data"))
            self.assertEqual(env["XDG_RUNTIME_DIR"], str(profile_root / "runtime"))
            self.assertEqual(env["TMPDIR"], str(profile_root / "tmp"))


class TestIdentityMaskingAndHashing(unittest.TestCase):
    def test_mask_email(self):
        self.assertEqual(mask_email("user@example.com"), "us***r@example.com")
        self.assertEqual(mask_email("a@example.com"), "a***@example.com")
        self.assertEqual(mask_email("invalid"), "[UNKNOWN]")

    def test_hash_identity_is_salted_and_deterministic(self):
        email = "Test.Account@example.com"
        salt = "salt123"
        h1 = hash_identity(email, salt)
        h2 = hash_identity("test.account@example.com ", salt)
        self.assertEqual(h1, h2)

        # Different salt produces different hash
        h3 = hash_identity(email, "diffsalt")
        self.assertNotEqual(h1, h3)

    def test_extract_email_from_jwt_token(self):
        # Fake JWT with {"email": "worker@example.com"} in payload
        # payload = {"iss":"accounts.google.com","email":"worker@example.com"}
        import base64
        import json
        payload = json.dumps({"iss": "accounts.google.com", "email": "worker@example.com"}).encode()
        b64_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        fake_jwt = f"eyJhbGciOiJSUzI1NiJ9.{b64_payload}.signature"
        secret_json = json.dumps({"token": {}, "id_token": fake_jwt})

        extracted = extract_email_from_token_payload(secret_json)
        self.assertEqual(extracted, "worker@example.com")


class TestWorkerProfileManager(unittest.TestCase):
    def test_profile_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            mgr = WorkerProfileManager(Path(tmp_dir) / "worker-1")
            mgr.ensure_structure()

            initial = mgr.load_metadata()
            self.assertEqual(initial["state"], "NOT_LOGGED_IN")

            mgr.mark_pending()
            self.assertEqual(mgr.load_metadata()["state"], "PENDING")

            mgr.save_verified_identity("worker2@example.com")
            meta = mgr.load_metadata()
            self.assertEqual(meta["state"], "OK")
            self.assertEqual(meta["masked_identity"], "wo***2@example.com")
            self.assertIsNotNone(meta["identity_hash"])

            mgr.wipe_profile()
            self.assertEqual(mgr.load_metadata()["state"], "NOT_LOGGED_IN")


if __name__ == "__main__":
    unittest.main()
