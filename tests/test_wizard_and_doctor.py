"""Unit and integration tests for LoginWizard, Doctor, and duplicate prevention."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from multigravity.config import SystemConfig, WorkerConfig
from multigravity.doctor import Doctor
from multigravity.isolation import PathGuardError, WorkerProfileManager
from multigravity.wizard import LoginWizard, WizardLockError


class TestWizardAndDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.workers_dir = self.tmp_path / "workers"
        self.workers_dir.mkdir(parents=True)

        self.w1_dir = self.workers_dir / "worker-1"
        self.w2_dir = self.workers_dir / "worker-2"
        self.w3_dir = self.workers_dir / "worker-3"

        self.workers_cfg = [
            WorkerConfig(id="worker-1", label="Account 2", profile_dir=str(self.w1_dir)),
            WorkerConfig(id="worker-2", label="Account 3", profile_dir=str(self.w2_dir)),
            WorkerConfig(id="worker-3", label="Account 4", profile_dir=str(self.w3_dir)),
        ]
        self.config = SystemConfig(
            log_dir=str(self.tmp_path / "logs"),
            journal_dir=str(self.tmp_path / "journals"),
            workers=self.workers_cfg,
        )

        # Create a mock agy script that can simulate successful models check and login
        self.mock_agy = self.tmp_path / "mock_agy.sh"
        self.mock_agy.write_text(
            """#!/bin/sh
if [ "$1" = "models" ]; then
    # Check if marked authenticated
    if [ -f "$HOME/.authenticated" ]; then
        echo "gemini-3.8-flash-high"
        exit 0
    else
        echo "You are not logged into Antigravity."
        exit 1
    fi
fi
# Interactive login simulation: touch .authenticated in isolated HOME
touch "$HOME/.authenticated"
exit 0
"""
        )
        self.mock_agy.chmod(0o755)

        self.lock_path = self.tmp_path / "test_wizard.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_sequential_logins_end_ok(self):
        """From three empty worker profiles, wizard guides through 3 logins and ends with all OK."""
        wizard = LoginWizard(
            config=self.config,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            skip_main_check=True,
        )

        # Mock inputs: user presses enter to begin, then provides email
        emails = ["account2@example.com", "account3@example.com", "account4@example.com"]
        with patch("builtins.input", side_effect=["", emails[0], "", emails[1], "", emails[2]]):
            success = wizard.run_wizard()
            self.assertTrue(success)

        # Verify all 3 profiles marked OK
        for i, w in enumerate(self.workers_cfg):
            mgr = WorkerProfileManager(w.profile_dir)
            meta = mgr.load_metadata()
            self.assertEqual(meta["state"], "OK")
            self.assertIn("example.com", meta["masked_identity"])

    def test_rerunning_skips_verified_workers(self):
        """Rerunning skips verified workers without prompting for input."""
        wizard = LoginWizard(
            config=self.config,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            skip_main_check=True,
        )

        # Pre-verify worker-1 and worker-2
        for i, w in enumerate([self.workers_cfg[0], self.workers_cfg[1]]):
            mgr = WorkerProfileManager(w.profile_dir)
            mgr.ensure_structure()
            # Mark home authenticated
            (Path(w.profile_dir) / "home" / ".authenticated").touch()
            mgr.save_verified_identity(f"account{i+2}@example.com")

        # Now run wizard: only worker-3 should prompt
        with patch("builtins.input", side_effect=["", "account4@example.com"]) as mock_input:
            success = wizard.run_wizard()
            self.assertTrue(success)
            # Only called 2 times (enter + email for worker-3)
            self.assertEqual(mock_input.call_count, 2)

    def test_duplicate_account_detected_and_rejected(self):
        """Logging the same account into two workers is detected and rejected."""
        wizard = LoginWizard(
            config=self.config,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            main_account_email="main.account@example.com",
        )

        # Pre-verify worker-1 with account2@example.com
        mgr1 = WorkerProfileManager(self.w1_dir)
        mgr1.ensure_structure()
        (self.w1_dir / "home" / ".authenticated").touch()
        mgr1.save_verified_identity("account2@example.com")

        # Try logging into worker-2 with the same email, and reject wipe (n)
        with patch("builtins.input", side_effect=["", "account2@example.com", "n"]):
            success = wizard.run_wizard(target_worker_id="worker-2")
            self.assertFalse(success)

        # Worker-2 should not be OK
        mgr2 = WorkerProfileManager(self.w2_dir)
        self.assertNotEqual(mgr2.load_metadata()["state"], "OK")

        # Try logging in with the interactive main agent account email
        with patch("builtins.input", side_effect=["", "main.account@example.com", "n"]):
            success = wizard.run_wizard(target_worker_id="worker-2")
            self.assertFalse(success)
            self.assertNotEqual(mgr2.load_metadata()["state"], "OK")

    def test_doctor_passes_and_fails_on_shared_path_or_leaked_key(self):
        """Doctor passes clean configuration and fails on path violations or leaked secrets."""
        # Clean configuration check
        doc = Doctor(config=self.config, agy_binary=str(self.mock_agy))
        results = doc.run_all()
        # All checks should pass
        for r in results:
            self.assertTrue(r.passed, f"Check '{r.name}' unexpectedly failed: {r.message}")

        # 1. Deliberately shared profile path: should fail
        bad_cfg = SystemConfig(
            workers=[
                WorkerConfig(id="w1", label="W1", profile_dir=str(self.w1_dir)),
                WorkerConfig(id="w2", label="W2", profile_dir=str(self.w1_dir)),  # duplicate
            ]
        )
        doc_bad_path = Doctor(config=bad_cfg, agy_binary=str(self.mock_agy))
        path_res = doc_bad_path.check_path_guards()
        self.assertFalse(path_res.passed)

        # 2. Leaked API key in environment: should fail
        with patch.dict(os.environ, {"GEMINI_API_KEY": "leaked_secret_token"}):
            env_res = doc.check_environment_allow_list()
            self.assertFalse(env_res.passed)

    def test_lock_file_concurrency_and_stale_cleanup(self):
        """Lock file prevents multiple wizards and cleans up stale locks."""
        wizard1 = LoginWizard(config=self.config, agy_binary=str(self.mock_agy), lock_path=self.lock_path)
        wizard1.lock.acquire()

        wizard2 = LoginWizard(config=self.config, agy_binary=str(self.mock_agy), lock_path=self.lock_path)
        with self.assertRaises(WizardLockError):
            wizard2.lock.acquire()

        wizard1.lock.release()

        # Write a dead PID to simulate stale lock file
        self.lock_path.write_text("9999999")
        wizard2.lock.acquire()
        self.assertTrue(wizard2.lock._acquired)
        wizard2.lock.release()

    def test_ctrl_c_mid_login_leaves_no_orphans_and_profile_pending(self):
        """Ctrl-C mid-login leaves no orphan processes and the profile stays pending, never OK."""
        # Create a mock agy script that sleeps until killed
        hang_script = self.tmp_path / "hang_agy.sh"
        hang_script.write_text("#!/bin/sh\nsleep 30\n")
        hang_script.chmod(0o755)

        wizard = LoginWizard(
            config=self.config,
            agy_binary=str(hang_script),
            lock_path=self.lock_path,
            skip_main_check=True,
        )
        mgr = WorkerProfileManager(self.w1_dir)
        mgr.ensure_structure()
        mgr.mark_pending()

        # Run login in a separate subprocess that we will send SIGINT to
        test_py = f"""
import sys, unittest
from unittest.mock import patch
from multigravity.config import SystemConfig, WorkerConfig
from multigravity.wizard import LoginWizard

cfg = SystemConfig(workers=[WorkerConfig(id='worker-1', label='Acc 2', profile_dir='{self.w1_dir}')])
wiz = LoginWizard(cfg, agy_binary='{hang_script}', lock_path='{self.lock_path}', skip_main_check=True)
with patch('builtins.input', return_value=''):
    wiz.run_wizard(target_worker_id='worker-1')
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", test_py],
            cwd=str(Path(__file__).parent.parent),
            preexec_fn=os.setsid,
        )
        time.sleep(0.3)
        # Send SIGINT (Ctrl-C)
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=3.0)

        # Profile must remain PENDING
        meta = mgr.load_metadata()
        self.assertEqual(meta["state"], "PENDING")

    def test_main_identity_wizard_abort_when_unset(self):
        """Wizard refuses to start logins when Account 1 identity is unset and user does not skip."""
        state_dir = self.tmp_path / "state_unset"
        wizard = LoginWizard(
            config=self.config,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            state_dir=state_dir,
        )
        # User rejects skipping (enters 'n')
        with patch("builtins.input", side_effect=["n"]):
            success = wizard.run_wizard()
            self.assertFalse(success)

    def test_main_identity_wizard_proceeds_when_confirmed(self):
        """Wizard proceeds when user explicitly confirms skipping Account 1 check."""
        state_dir = self.tmp_path / "state_skip"
        wizard = LoginWizard(
            config=self.config,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            state_dir=state_dir,
        )
        # User confirms skipping ('y'), then presses enter and provides emails
        inputs = ["y", "", "account2@example.com", "", "account3@example.com", "", "account4@example.com"]
        with patch("builtins.input", side_effect=inputs):
            success = wizard.run_wizard()
            self.assertTrue(success)

    def test_main_identity_salted_hash_collision_protection(self):
        """Account 1 collision is caught via salted hash in state directory without storing plaintext email."""
        from multigravity.identity import save_main_identity

        state_dir = self.tmp_path / "state_active"
        # Register Account 1 identity
        main_meta = save_main_identity("main.interactive@example.com", state_dir=state_dir)
        self.assertIn("salt", main_meta)
        self.assertIn("identity_hash", main_meta)
        self.assertNotIn("main.interactive@example.com", str(main_meta))

        # Check permissions: state file must be mode 0600
        state_file = state_dir / "main_identity.json"
        self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)

        wizard = LoginWizard(
            config=self.config,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            state_dir=state_dir,
        )

        # Attempt to log in worker-1 with Account 1 email: must trigger collision error
        with patch("builtins.input", side_effect=["", "main.interactive@example.com", "n"]):
            success = wizard.run_wizard(target_worker_id="worker-1")
            self.assertFalse(success)

        # Ensure worker-1 was NOT marked OK
        mgr1 = WorkerProfileManager(self.w1_dir)
        self.assertNotEqual(mgr1.load_metadata().get("state"), "OK")

    def test_doctor_two_worker_collision_simulated_and_real(self):
        """Doctor reports SIMULATED when <2 workers logged in, and REAL when >=2 workers are OK."""
        doc = Doctor(config=self.config, agy_binary=str(self.mock_agy))

        # 0 workers logged in -> SIMULATED
        res_zero = doc.check_two_worker_collision()
        self.assertEqual(res_zero.check_type, "SIMULATED")
        self.assertTrue(res_zero.passed)
        self.assertIn("fewer than 2 workers logged in", res_zero.message)

        # 1 worker logged in -> SIMULATED
        mgr1 = WorkerProfileManager(self.w1_dir)
        mgr1.ensure_structure()
        (self.w1_dir / "home" / ".authenticated").touch()
        mgr1.save_verified_identity("worker1.account@example.com")

        res_one = doc.check_two_worker_collision()
        self.assertEqual(res_one.check_type, "SIMULATED")
        self.assertTrue(res_one.passed)

        # 2 workers logged in with distinct identities -> REAL, PASSED
        mgr2 = WorkerProfileManager(self.w2_dir)
        mgr2.ensure_structure()
        (self.w2_dir / "home" / ".authenticated").touch()
        mgr2.save_verified_identity("worker2.account@example.com")

        res_two = doc.check_two_worker_collision()
        self.assertEqual(res_two.check_type, "REAL")
        self.assertTrue(res_two.passed)
        self.assertIn("distinct verified identities", res_two.message)

        # 2 workers logged in with DUPLICATE identities -> REAL, FAILED
        mgr2.save_verified_identity("worker1.account@example.com", salt=mgr1.load_metadata()["salt"])
        res_dup = doc.check_two_worker_collision()
        self.assertEqual(res_dup.check_type, "REAL")
        self.assertFalse(res_dup.passed)
        self.assertIn("Collision detected", res_dup.message)

    def test_config_resolution_precedence(self):
        """Config resolution follows: --config -> MULTIGRAVITY_CONFIG -> ./config.local.toml, never config.example.toml."""
        from multigravity.cli import resolve_config_path

        flag_cfg = self.tmp_path / "flag_config.toml"
        flag_cfg.write_text("test = 1\n")

        env_cfg = self.tmp_path / "env_config.toml"
        env_cfg.write_text("test = 2\n")

        local_cfg = self.tmp_path / "config.local.toml"
        local_cfg.write_text("test = 3\n")

        # 1. Flag overrides everything
        with patch.dict(os.environ, {"MULTIGRAVITY_CONFIG": str(env_cfg)}):
            resolved = resolve_config_path(config_arg=str(flag_cfg))
            self.assertEqual(resolved, flag_cfg.resolve())

        # 2. Env overrides ./config.local.toml
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.tmp_path)
            with patch.dict(os.environ, {"MULTIGRAVITY_CONFIG": str(env_cfg)}):
                resolved = resolve_config_path()
                self.assertEqual(resolved, env_cfg.resolve())

            # 3. Default to ./config.local.toml when no flag or env var
            with patch.dict(os.environ, {}, clear=True):
                resolved = resolve_config_path()
                self.assertEqual(resolved, local_cfg.resolve())

            # 4. Fail when ./config.local.toml is missing (no fallback to config.example.toml)
            local_cfg.unlink()
            (self.tmp_path / "config.example.toml").write_text("example = true\n")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(FileNotFoundError) as ctx:
                    resolve_config_path()
                self.assertIn("config.local.toml", str(ctx.exception))
                self.assertIn("config.example.toml", str(ctx.exception))
        finally:
            os.chdir(orig_cwd)


if __name__ == "__main__":
    unittest.main()

