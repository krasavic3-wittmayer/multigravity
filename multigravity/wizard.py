"""Sequential login wizard with process isolation, duplicate detection, and clean cancellation."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from multigravity.config import SystemConfig, WorkerConfig
from multigravity.identity import (
    is_main_account_match,
    load_main_identity,
)
from multigravity.isolation import (
    EnvironmentAllowList,
    PathGuard,
    PathGuardError,
    WorkerProfileManager,
    hash_identity,
    mask_email,
)
from multigravity.preflight import ARCH_PACKAGE_MAP, PreflightError, verify_dependencies_or_raise
from multigravity.probes import get_isolated_runner_prefix, probe_worker_status

WORKER_ACCOUNT_MAP = {
    "worker-1": "Account 2",
    "worker-2": "Account 3",
    "worker-3": "Account 4",
}


def format_worker_display(worker_id: str, label: str | None = None) -> str:
    """Formats worker ID and Account label, e.g. worker-1 (Account 2)."""
    acc = WORKER_ACCOUNT_MAP.get(worker_id)
    if acc:
        if label and label != acc:
            return f"{worker_id} ({acc} - {label})"
        return f"{worker_id} ({acc})"
    return f"{worker_id} ({label})" if label else worker_id


class WizardLockError(RuntimeError):
    pass


class WizardLock:
    """PID-based lock file to ensure only one login wizard runs at a time."""

    def __init__(self, lock_path: str | Path | None = None):
        if lock_path:
            self.lock_path = Path(lock_path)
        else:
            xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
            if xdg_runtime:
                self.lock_path = Path(xdg_runtime) / "multigravity_wizard.lock"
            else:
                self.lock_path = Path("/tmp") / f"multigravity_wizard_{os.getuid()}.lock"
        self._acquired = False

    def acquire(self):
        if self.lock_path.exists():
            try:
                content = self.lock_path.read_text().strip()
                pid = int(content)
                # Check if process is still running
                os.kill(pid, 0)
                raise WizardLockError(f"Another wizard is already running with PID {pid}")
            except (OSError, ValueError):
                # Stale lock file
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass

        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path.write_text(str(os.getpid()))
        self._acquired = True
        atexit.register(self.release)

    def release(self):
        if self._acquired and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self._acquired = False


class LoginWizard:
    """Guides the user through sequential, isolated worker account logins."""

    def __init__(
        self,
        config: SystemConfig,
        agy_binary: str | None = None,
        lock_path: str | Path | None = None,
        main_account_email: str | None = None,
        state_dir: Path | None = None,
        skip_main_check: bool = False,
        config_path: str | Path | None = None,
    ):
        self.config = config
        self.agy_binary = agy_binary or "/usr/sbin/agy"
        self.lock = WizardLock(lock_path)
        self.main_account_email = main_account_email
        self.state_dir = state_dir
        self.skip_main_check = skip_main_check
        self.config_path = Path(config_path).resolve() if config_path else None

    def preflight(self):
        """Runs preflight checks: agy binary, system isolation tools, valid config, worker count, path guard."""
        verify_dependencies_or_raise(self.agy_binary)

        enabled = self.config.enabled_workers
        if not enabled or len(enabled) < 1 or len(enabled) > 3:
            raise ValueError(
                f"Configuration must define between 1 and 3 enabled workers (Accounts 2-4), found {len(enabled)}"
            )

        # Enforce PathGuard on all configured worker profile paths
        paths = [w.profile_dir for w in self.config.workers]
        PathGuard.validate_worker_profiles(paths)

    def get_status_board(self) -> list[dict[str, Any]]:
        """Returns current status board data for all configured workers."""
        board = []
        for w in self.config.workers:
            if not w.enabled:
                board.append({
                    "id": w.id,
                    "label": w.label,
                    "display_name": format_worker_display(w.id, w.label),
                    "profile_dir": w.profile_dir,
                    "state": "DISABLED",
                    "masked_identity": "-",
                    "last_verified_at": None,
                    "detail": "Worker is disabled in configuration",
                })
                continue

            mgr = WorkerProfileManager(w.profile_dir)
            meta = mgr.load_metadata()

            state, detail, detected_email = probe_worker_status(
                Path(w.profile_dir),
                agy_binary=self.agy_binary,
                timeout_s=5.0,
            )

            # If metadata says OK but probe says unauthenticated or expired, update state
            display_state = state if state in ("OK", "AUTH_EXPIRED", "RATE_LIMITED") else meta.get("state", state)
            if state == "NOT_LOGGED_IN" and meta.get("state") == "OK":
                display_state = "AUTH_EXPIRED"

            board.append({
                "id": w.id,
                "label": w.label,
                "display_name": format_worker_display(w.id, w.label),
                "profile_dir": w.profile_dir,
                "state": display_state,
                "masked_identity": meta.get("masked_identity"),
                "last_verified_at": meta.get("last_verified_at"),
                "detail": detail,
            })
        return board

    def print_status_board(self):
        board = self.get_status_board()
        print("\n" + "=" * 78)
        print("                        MULTIGRAVITY WORKER STATUS")
        if self.config_path:
            print(f" Config: {self.config_path}")
        print("=" * 78)
        print(f"{'WORKER (ACCOUNT)':<26} {'STATE':<14} {'IDENTITY':<18} {'VERIFIED'}")
        print("-" * 78)
        for item in board:
            v_time = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(item["last_verified_at"]))
                if item["last_verified_at"]
                else "-"
            )
            acc = item["masked_identity"] or "-"
            print(f"{item['display_name']:<26} {item['state']:<14} {acc:<18} {v_time}")
        print("=" * 78 + "\n")

    def run_wizard(self, target_worker_id: str | None = None) -> bool:
        """Runs the interactive sequential login wizard."""
        self.preflight()

        if target_worker_id:
            target_worker = next((w for w in self.config.workers if w.id == target_worker_id), None)
            if target_worker and not target_worker.enabled:
                print(f"[!] Worker '{target_worker_id}' is disabled in configuration.")
                return False

        # Check Account 1 identity to ensure collision prevention
        main_id = load_main_identity(self.state_dir)
        if main_id is None and not self.main_account_email and not self.skip_main_check:
            print("\n" + "!" * 78)
            print("WARNING: Main Account (Account 1) identity is NOT set.")
            print("Without Account 1 identity, the wizard cannot prevent collisions with")
            print("your primary interactive account.")
            print("To register Account 1 securely, run:")
            print("    multigravity set-main-identity")
            print("!" * 78)
            try:
                resp = input("Proceed anyway and skip Account 1 collision check? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                return False
            if resp not in ("y", "yes"):
                print("[!] Login wizard aborted to protect Account 1.")
                return False
            self.skip_main_check = True

        self.lock.acquire()
        try:
            print("\nStarting Multigravity Login Wizard...")
            if self.config_path:
                print(f"Config: {self.config_path}")
            self.print_status_board()

            workers_to_login = []
            for w in self.config.workers:
                if not w.enabled:
                    continue
                if target_worker_id and w.id != target_worker_id:
                    continue
                mgr = WorkerProfileManager(w.profile_dir)
                meta = mgr.load_metadata()
                probe_state, _, _ = probe_worker_status(Path(w.profile_dir), agy_binary=self.agy_binary, timeout_s=5.0)
                if probe_state == "OK" and meta.get("state") == "OK" and not target_worker_id:
                    print(f"✓ Worker {format_worker_display(w.id, w.label)} is already authenticated ({meta.get('masked_identity')}). Skipping.")
                    continue
                workers_to_login.append(w)

            if not workers_to_login:
                print("All configured enabled workers are verified and ready!")
                return True

            for w in workers_to_login:
                success = self._login_single_worker(w)
                if not success:
                    print(f"\n[!] Login for worker {format_worker_display(w.id, w.label)} was not completed.")
                    return False

            self.print_status_board()
            return True
        finally:
            self.lock.release()

    def _login_single_worker(self, worker: WorkerConfig) -> bool:
        mgr = WorkerProfileManager(worker.profile_dir)
        mgr.ensure_structure()
        mgr.mark_pending()

        w_name = format_worker_display(worker.id, worker.label)
        target_account = WORKER_ACCOUNT_MAP.get(worker.id, worker.label)

        print("\n" + "#" * 78)
        print(f"Now logging in: {w_name}")
        print(f"Target Google Account: {target_account} (NOT your main account)")
        print("#" * 78)
        print("NOTICE:")
        print("• If your browser is already signed into your main account, Google will often auto-select it.")
        print("• When the browser window opens, look closely at the active profile or select 'Use another account'.")
        print("• The login URL will be displayed below so you can also copy-paste it into a Private/Incognito window.\n")

        print("=" * 78)
        print("ANTIGRAVITY TUI LOGIN INSTRUCTIONS:")
        print("1. The Antigravity TUI will launch now and open the Google sign-in page.")
        print("2. Complete Google login in your browser for this worker account.")
        print("3. DO NOT enter any prompts in the Antigravity TUI.")
        print("4. Once signed in, exit the TUI immediately: type '/quit' or press [Ctrl-D].")
        print("5. After you exit the TUI, this wizard will automatically verify the login")
        print("   and proceed to the next step.")
        print("=" * 78 + "\n")

        input("Press [Enter] to begin login for this worker...")

        # Setup isolated environment
        browser_helper = Path(__file__).parent.parent / "scripts" / "multigravity-browser-helper"
        extra_vars = {}
        if browser_helper.exists():
            extra_vars["BROWSER"] = str(browser_helper.resolve())

        env = EnvironmentAllowList.build_worker_env(Path(worker.profile_dir), extra_vars=extra_vars)
        runtime_dir = Path(worker.profile_dir) / "runtime"
        prefix = get_isolated_runner_prefix(runtime_dir)

        # Run agy in interactive login mode
        cmd = prefix + [self.agy_binary]

        child_proc: subprocess.Popen | None = None

        def sigint_handler(sig, frame):
            print("\n[!] Ctrl-C detected. Terminating worker login process group...")
            if child_proc and child_proc.poll() is None:
                try:
                    pgid = os.getpgid(child_proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    time.sleep(0.5)
                    if child_proc.poll() is None:
                        os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass
            mgr.mark_pending()
            print("[✓] Process group reaped cleanly. Profile remains marked PENDING.")
            sys.exit(130)

        old_handler = signal.signal(signal.SIGINT, sigint_handler)
        try:
            child_proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=str(Path(worker.profile_dir) / "home"),
                preexec_fn=os.setsid,
            )
            child_proc.wait()
        finally:
            signal.signal(signal.SIGINT, old_handler)

        print("\nVerifying worker authentication...")
        state, detail, detected_email = probe_worker_status(
            Path(worker.profile_dir),
            agy_binary=self.agy_binary,
            timeout_s=15.0,
        )

        if state != "OK":
            print(f"✗ Verification failed for {w_name}: {detail}")
            mgr.mark_pending()
            return False

        # Identify account email
        account_email = detected_email
        if not account_email:
            try:
                account_email = input(
                    f"Please enter the Google email address you just signed into for {w_name}: "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                mgr.mark_pending()
                return False

        if not account_email or "@" not in account_email:
            print("✗ Invalid email address provided. Profile remains PENDING.")
            mgr.mark_pending()
            return False

        # Duplicate check against all other workers and main account
        is_duplicate, duplicate_target = self._check_duplicate(worker.id, account_email)
        if is_duplicate:
            print("\n" + "!" * 78)
            print(f"COLLISION ERROR: The account '{mask_email(account_email)}' is already assigned to {duplicate_target}!")
            print("Explanation: Each worker account must be separate. Your browser likely auto-selected an existing account.")
            print("!" * 78)
            confirm = input(f"Wipe profile for {w_name} and retry? [y/N]: ").strip().lower()
            if confirm == "y":
                mgr.wipe_profile()
                print(f"Wiped profile for {w_name}. Please re-run login with the correct account.")
            else:
                mgr.mark_pending()
            return False

        # Save verified identity
        saved_meta = mgr.save_verified_identity(account_email)
        print(f"✓ Worker {w_name} verified OK as {saved_meta['masked_identity']}.")
        return True

    def _check_duplicate(self, current_worker_id: str, new_email: str) -> tuple[bool, str]:
        """Checks if new_email is already used by another worker or the main account."""
        norm_email = new_email.strip().lower()

        # Check against main account if known via explicit email or state directory salted hash
        if self.main_account_email and norm_email == self.main_account_email.strip().lower():
            return True, "Main Account (Account 1)"

        main_id = load_main_identity(self.state_dir)
        if main_id and is_main_account_match(norm_email, main_id):
            return True, "Main Account (Account 1)"

        # Check against all other enabled workers
        for w in self.config.workers:
            if w.id == current_worker_id or not w.enabled:
                continue
            mgr = WorkerProfileManager(w.profile_dir)
            meta = mgr.load_metadata()
            salt = meta.get("salt")
            stored_hash = meta.get("identity_hash")
            if salt and stored_hash:
                test_hash = hash_identity(norm_email, salt)
                if test_hash == stored_hash:
                    return True, f"worker '{w.id}' ({w.label})"

        return False, ""
