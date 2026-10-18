"""Tests for two-worker configs, disabled workers, removed workers with stale state, and daemon startup."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multigravity.config import SystemConfig, WorkerConfig
from multigravity.daemon import CoreDaemon
from multigravity.doctor import Doctor
from multigravity.isolation import WorkerProfileManager
from multigravity.models import WorkerState
from multigravity.wizard import LoginWizard


class TestWorkerScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.workers_dir = self.tmp_path / "workers"
        self.workers_dir.mkdir(parents=True)

        self.w1_dir = self.workers_dir / "worker-1"
        self.w2_dir = self.workers_dir / "worker-2"
        self.w3_dir = self.workers_dir / "worker-3"

        self.lock_path = self.tmp_path / "test_wizard.lock"

        # Mock agy script
        self.mock_agy = self.tmp_path / "mock_agy.sh"
        self.mock_agy.write_text(
            """#!/bin/sh
if [ "$1" = "models" ]; then
    if [ -f "$HOME/.authenticated" ]; then
        echo "gemini-3.8-flash-high"
        exit 0
    else
        echo "You are not logged into Antigravity."
        exit 1
    fi
fi
touch "$HOME/.authenticated"
exit 0
"""
        )
        self.mock_agy.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_with_two_workers(self):
        """A 2-worker configuration (Accounts 2 & 3) passes preflight, doctor, and status."""
        config_2w = SystemConfig(
            log_dir=str(self.tmp_path / "logs"),
            journal_dir=str(self.tmp_path / "journals"),
            socket_path=str(self.tmp_path / "test.sock"),
            workers=[
                WorkerConfig(id="worker-1", label="Account 2", profile_dir=str(self.w1_dir), enabled=True),
                WorkerConfig(id="worker-2", label="Account 3", profile_dir=str(self.w2_dir), enabled=True),
            ],
        )

        self.assertEqual(len(config_2w.workers), 2)
        self.assertEqual(len(config_2w.enabled_workers), 2)

        # Wizard preflight
        wizard = LoginWizard(
            config=config_2w,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            state_dir=self.tmp_path / "state",
        )
        wizard.preflight()

        # Doctor check
        doc = Doctor(config=config_2w, agy_binary=str(self.mock_agy))
        results = doc.run_all()
        for r in results:
            self.assertTrue(r.passed, f"Check '{r.name}' failed: {r.message}")

        # Status board only lists 2 workers
        board = wizard.get_status_board()
        self.assertEqual(len(board), 2)
        self.assertEqual([w["id"] for w in board], ["worker-1", "worker-2"])

        # When both workers are verified, Doctor runs two-worker collision check as REAL
        m1 = WorkerProfileManager(self.w1_dir)
        m1.ensure_structure()
        (self.w1_dir / "home" / ".authenticated").touch()
        m1.save_verified_identity("worker1@example.com")

        m2 = WorkerProfileManager(self.w2_dir)
        m2.ensure_structure()
        (self.w2_dir / "home" / ".authenticated").touch()
        m2.save_verified_identity("worker2@example.com")

        collision_res = doc.check_two_worker_collision()
        self.assertEqual(collision_res.check_type, "REAL")
        self.assertTrue(collision_res.passed)
        self.assertIn("distinct verified identities", collision_res.message)

    def test_disabled_worker_behavior(self):
        """A disabled worker appears as DISABLED, is never probed, never offered for login, and never blocks."""
        config_3w = SystemConfig(
            log_dir=str(self.tmp_path / "logs"),
            journal_dir=str(self.tmp_path / "journals"),
            socket_path=str(self.tmp_path / "test.sock"),
            workers=[
                WorkerConfig(id="worker-1", label="Account 2", profile_dir=str(self.w1_dir), enabled=True),
                WorkerConfig(id="worker-2", label="Account 3", profile_dir=str(self.w2_dir), enabled=True),
                WorkerConfig(id="worker-3", label="Account 4", profile_dir=str(self.w3_dir), enabled=False),
            ],
        )

        wizard = LoginWizard(
            config=config_3w,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            state_dir=self.tmp_path / "state",
            skip_main_check=True,
        )

        # 1. Status board shows worker-3 as DISABLED without probing
        board = wizard.get_status_board()
        self.assertEqual(len(board), 3)
        w3_status = next(w for w in board if w["id"] == "worker-3")
        self.assertEqual(w3_status["state"], "DISABLED")
        self.assertEqual(w3_status["masked_identity"], "-")
        self.assertIsNone(w3_status["last_verified_at"])

        # Pre-verify worker-1 and worker-2
        m1 = WorkerProfileManager(self.w1_dir)
        m1.ensure_structure()
        (self.w1_dir / "home" / ".authenticated").touch()
        m1.save_verified_identity("account2@example.com")

        m2 = WorkerProfileManager(self.w2_dir)
        m2.ensure_structure()
        (self.w2_dir / "home" / ".authenticated").touch()
        m2.save_verified_identity("account3@example.com")

        # 2. Wizard finishes immediately without prompting for worker-3
        success = wizard.run_wizard()
        self.assertTrue(success, "Wizard should succeed when all enabled workers are OK, ignoring disabled worker-3")

        # 3. Direct login attempt on disabled worker is rejected
        rejected = wizard.run_wizard(target_worker_id="worker-3")
        self.assertFalse(rejected, "Direct login on disabled worker must be rejected")

        # 4. Doctor collision test runs as REAL between the 2 enabled workers
        doc = Doctor(config=config_3w, agy_binary=str(self.mock_agy))
        collision_res = doc.check_two_worker_collision()
        self.assertEqual(collision_res.check_type, "REAL")
        self.assertTrue(collision_res.passed)

        # 5. Daemon registers worker-3 as DISABLED and never dispatches to it
        daemon = CoreDaemon(config_3w)
        w3_rec = daemon.registry.get_worker("worker-3")
        self.assertIsNotNone(w3_rec)
        self.assertEqual(w3_rec.state, WorkerState.DISABLED)
        self.assertFalse(w3_rec.enabled)

        # LRU idle selection never returns worker-3
        idle = daemon.registry.get_least_recently_used_idle()
        self.assertIn(idle.id, ["worker-1", "worker-2"])

    def test_removed_worker_with_stale_state_ignored(self):
        """A removed worker with stale files on disk/state is completely ignored by wizard, status, doctor, daemon."""
        # Create stale state for worker-3 on disk
        m3 = WorkerProfileManager(self.w3_dir)
        m3.ensure_structure()
        (self.w3_dir / "home" / ".authenticated").touch()
        m3.save_verified_identity("stale.account@example.com")

        # But active config ONLY declares worker-1 and worker-2
        active_config = SystemConfig(
            log_dir=str(self.tmp_path / "logs"),
            journal_dir=str(self.tmp_path / "journals"),
            socket_path=str(self.tmp_path / "test.sock"),
            workers=[
                WorkerConfig(id="worker-1", label="Account 2", profile_dir=str(self.w1_dir), enabled=True),
                WorkerConfig(id="worker-2", label="Account 3", profile_dir=str(self.w2_dir), enabled=True),
            ],
        )

        wizard = LoginWizard(
            config=active_config,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            state_dir=self.tmp_path / "state",
            skip_main_check=True,
        )

        # worker-3 does not appear in status board
        board = wizard.get_status_board()
        worker_ids = [w["id"] for w in board]
        self.assertNotIn("worker-3", worker_ids)
        self.assertEqual(worker_ids, ["worker-1", "worker-2"])

        # Daemon does not register worker-3
        daemon = CoreDaemon(active_config)
        self.assertIsNone(daemon.registry.get_worker("worker-3"))
        registered_ids = [w["id"] for w in daemon.registry.list_workers()]
        self.assertNotIn("worker-3", registered_ids)

        # Doctor ignores worker-3
        doc = Doctor(config=active_config, agy_binary=str(self.mock_agy))
        doc_res = doc.run_all()
        for r in doc_res:
            self.assertTrue(r.passed, f"Doctor check {r.name} failed: {r.message}")

    def test_wizard_completion_and_daemon_startup_with_two_workers(self):
        """Wizard runs sequentially for two unverified workers and completes, allowing daemon to start."""
        config_2w = SystemConfig(
            log_dir=str(self.tmp_path / "logs"),
            journal_dir=str(self.tmp_path / "journals"),
            socket_path=str(self.tmp_path / "test.sock"),
            workers=[
                WorkerConfig(id="worker-1", label="Account 2", profile_dir=str(self.w1_dir), enabled=True),
                WorkerConfig(id="worker-2", label="Account 3", profile_dir=str(self.w2_dir), enabled=True),
            ],
        )

        wizard = LoginWizard(
            config=config_2w,
            agy_binary=str(self.mock_agy),
            lock_path=self.lock_path,
            state_dir=self.tmp_path / "state",
            skip_main_check=True,
        )

        # Simulate user providing emails during interactive login
        inputs = ["", "account2@example.com", "", "account3@example.com"]
        with patch("builtins.input", side_effect=inputs):
            success = wizard.run_wizard()
            self.assertTrue(success, "Wizard should complete successfully for 2 workers")

        # Both workers should now be verified OK
        m1 = WorkerProfileManager(self.w1_dir)
        self.assertEqual(m1.load_metadata()["state"], "OK")

        m2 = WorkerProfileManager(self.w2_dir)
        self.assertEqual(m2.load_metadata()["state"], "OK")

        # Daemon initializes cleanly with both workers
        daemon = CoreDaemon(config_2w)
        w1 = daemon.registry.get_worker("worker-1")
        w2 = daemon.registry.get_worker("worker-2")
        self.assertEqual(w1.state, WorkerState.IDLE)
        self.assertEqual(w2.state, WorkerState.IDLE)


if __name__ == "__main__":
    unittest.main()
