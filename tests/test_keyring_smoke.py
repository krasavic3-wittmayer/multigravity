"""Real keyring smoke test verifying private D-Bus and gnome-keyring isolation."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from multigravity.isolation import EnvironmentAllowList, WorkerProfileManager
from multigravity.preflight import check_dependencies
from multigravity.probes import get_isolated_runner_prefix


def snapshot_dir_metadata(dir_path: Path) -> dict[str, tuple[int, float]]:
    """Captures file sizes and modification times without reading contents."""
    snapshot = {}
    if not dir_path.exists():
        return snapshot
    for p in dir_path.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(dir_path))
            if (
                rel.startswith("antigravity-cli/brain/")
                or rel.startswith("antigravity-cli/conversations/")
                or rel.startswith("antigravity-cli/log/")
                or rel.endswith(".log")
                or rel.endswith("-wal")
                or rel.endswith("-shm")
                or rel.endswith(".pb")
                or rel.endswith(".db")
                or rel.endswith("history.jsonl")
            ):
                continue
            try:
                st = p.stat()
                snapshot[rel] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    return snapshot


def get_active_daemon_pids() -> set[int]:
    """Returns currently running PIDs for gnome-keyring-daemon and dbus-daemon."""
    pids = set()
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "gnome-keyring-daemon|dbus-daemon"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.strip().splitlines():
            line = line.strip()
            if line:
                pids.add(int(line))
    except (subprocess.CalledProcessError, ValueError):
        pass
    return pids


class TestRealKeyringSmoke(unittest.TestCase):
    """Proves private keyring isolation against the real host environment."""

    def test_isolated_keyring_lifecycle_and_zero_host_leakage(self):
        # 1. Dependency preflight
        ok, missing_bins, _, advice = check_dependencies()
        if not ok and any(b in missing_bins for b in ("dbus-run-session", "gnome-keyring-daemon", "secret-tool")):
            self.skipTest(f"Missing required host binaries: {advice}")

        real_home = Path.home()
        desktop_keyring_dir = real_home / ".local" / "share" / "keyrings"
        gemini_dir = real_home / ".gemini"

        # 2. Snapshot real host metadata & running processes before test
        desktop_snapshot_before = snapshot_dir_metadata(desktop_keyring_dir)
        gemini_snapshot_before = snapshot_dir_metadata(gemini_dir)
        pids_before = get_active_daemon_pids()

        # 3. Create throwaway profile directory in temp location
        throwaway_dir = Path(tempfile.mkdtemp(prefix="multigravity_keyring_smoke_")).resolve()
        try:
            mgr = WorkerProfileManager(throwaway_dir)
            mgr.ensure_structure()

            runtime_dir = throwaway_dir / "runtime"
            data_dir = throwaway_dir / "data"
            env = EnvironmentAllowList.build_worker_env(throwaway_dir)
            prefix = get_isolated_runner_prefix(runtime_dir)

            self.assertTrue(len(prefix) > 0, "Prefix must not be empty when tools are available")
            self.assertIn("dbus-run-session", prefix[0])

            dummy_secret = "multigravity_smoke_test_dummy_secret_value_xyz789"

            # 4. Store dummy secret through isolated runner
            store_cmd = prefix + [
                "secret-tool", "store",
                "--label=multigravity_smoke_test",
                "service", "gemini",
                "username", "antigravity",
            ]
            store_proc = subprocess.run(
                store_cmd,
                input=f"{dummy_secret}\n",
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(
                store_proc.returncode, 0,
                f"Failed to store secret in isolated keyring: {store_proc.stderr}",
            )

            # 5. Read back dummy secret through isolated runner
            lookup_cmd = prefix + [
                "secret-tool", "lookup",
                "service", "gemini",
                "username", "antigravity",
            ]
            lookup_proc = subprocess.run(
                lookup_cmd,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(
                lookup_proc.returncode, 0,
                f"Failed to lookup secret from isolated keyring: {lookup_proc.stderr}",
            )
            self.assertEqual(lookup_proc.stdout.strip(), dummy_secret)

            # 6. Verify that keyring file was created INSIDE throwaway directory
            worker_keyrings = list((data_dir / "keyrings").glob("*.keyring"))
            self.assertTrue(
                len(worker_keyrings) > 0,
                f"Expected keyring file in {data_dir / 'keyrings'}, found none",
            )
        finally:
            # 7. Tear everything down and remove throwaway directory
            if throwaway_dir.exists():
                shutil.rmtree(throwaway_dir)

        # Allow child processes a brief moment to finish teardown
        time.sleep(0.3)

        # 8. Assert throwaway directory is completely gone
        self.assertFalse(throwaway_dir.exists(), "Throwaway directory was not removed")

        # 9. Prove host desktop keyring (~/.local/share/keyrings) was not modified
        desktop_snapshot_after = snapshot_dir_metadata(desktop_keyring_dir)
        self.assertEqual(
            desktop_snapshot_before, desktop_snapshot_after,
            f"Host desktop keyring was modified! Diff: before={desktop_snapshot_before}, after={desktop_snapshot_after}",
        )

        # 10. Prove host ~/.gemini directory was not modified
        gemini_snapshot_after = snapshot_dir_metadata(gemini_dir)
        self.assertEqual(
            gemini_snapshot_before, gemini_snapshot_after,
            f"Host ~/.gemini was modified! Diff: before={gemini_snapshot_before}, after={gemini_snapshot_after}",
        )

        # 11. Prove no orphan processes remain
        pids_after = get_active_daemon_pids()
        orphan_pids = pids_after - pids_before
        self.assertEqual(
            len(orphan_pids), 0,
            f"Orphan daemon processes detected: {orphan_pids}",
        )


if __name__ == "__main__":
    unittest.main()
