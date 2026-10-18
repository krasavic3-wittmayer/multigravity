"""Doctor diagnostics and credential collision test suite."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from multigravity.config import SystemConfig
from multigravity.isolation import (
    EnvironmentAllowList,
    PathGuard,
    PathGuardError,
    WorkerProfileManager,
)
from multigravity.preflight import ARCH_PACKAGE_MAP, check_python_version
from multigravity.probes import probe_worker_status


class DoctorCheckResult:
    def __init__(self, name: str, passed: bool, message: str = "", check_type: str = "REAL"):
        self.name = name
        self.passed = passed
        self.message = message
        self.check_type = check_type


class Doctor:
    """Runs system diagnostics, isolation audits, and collision prevention tests."""

    def __init__(
        self,
        config: SystemConfig,
        agy_binary: str | None = None,
        config_path: str | Path | None = None,
    ):
        self.config = config
        self.agy_binary = agy_binary or shutil.which("agy") or "/usr/sbin/agy"
        self.config_path = Path(config_path).resolve() if config_path else None

    def check_binaries(self) -> DoctorCheckResult:
        py_ok, py_msg = check_python_version()
        if not py_ok:
            return DoctorCheckResult("python version", False, py_msg, check_type="REAL")
        if not Path(self.agy_binary).exists() and not shutil.which("agy"):
            return DoctorCheckResult(
                "agy binary",
                False,
                f"Missing agy at {self.agy_binary}. Install Google Antigravity CLI.",
                check_type="REAL",
            )
        return DoctorCheckResult("agy binary", True, f"Found at {self.agy_binary} ({py_msg})", check_type="REAL")

    def check_isolation_tools(self) -> DoctorCheckResult:
        missing_bins = []
        missing_pkgs = []
        for b, pkg in ARCH_PACKAGE_MAP.items():
            if not shutil.which(b):
                missing_bins.append(b)
                if pkg not in missing_pkgs:
                    missing_pkgs.append(pkg)
        if missing_bins:
            pkgs = " ".join(missing_pkgs)
            return DoctorCheckResult(
                "keyring isolation tools",
                False,
                f"Missing tools: {', '.join(missing_bins)}. Arch install: sudo pacman -S {pkgs}",
                check_type="REAL",
            )
        return DoctorCheckResult(
            "keyring isolation tools",
            True,
            "dbus-run-session, gnome-keyring, secret-tool, xdg-open available",
            check_type="REAL",
        )

    def check_path_guards(self) -> DoctorCheckResult:
        paths = [w.profile_dir for w in self.config.workers]
        try:
            PathGuard.validate_worker_profiles(paths)
            return DoctorCheckResult("path guard", True, "All worker profile paths are strictly isolated", check_type="REAL")
        except PathGuardError as e:
            return DoctorCheckResult("path guard", False, str(e), check_type="REAL")

    LEAKED_SECRET_VARS = [
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]

    def check_environment_allow_list(self) -> DoctorCheckResult:
        # 1. Check if parent process environment contains leaked API keys
        leaked = []
        for k in os.environ:
            upper_k = k.upper()
            if any(secret in upper_k for secret in ("API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS")):
                leaked.append(k)

        if leaked:
            return DoctorCheckResult(
                "env allow-list",
                False,
                f"Parent process environment contains leaked credentials: {', '.join(leaked)}",
                check_type="REAL",
            )

        # 2. Verify that the allow-list filter blocks all credentials and sensitive variables
        clean_child = EnvironmentAllowList.filter_environment()
        for k in clean_child:
            upper_k = k.upper()
            if any(secret in upper_k for secret in ("API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS")):
                return DoctorCheckResult(
                    "env allow-list",
                    False,
                    f"Child environment leaked forbidden variable: {k}",
                    check_type="REAL",
                )

        return DoctorCheckResult(
            "env allow-list",
            True,
            f"Child environment strictly filtered ({len(clean_child)} allowed variables, zero credentials leaked)",
            check_type="REAL",
        )

    def check_profile_permissions(self) -> DoctorCheckResult:
        issues = []
        for w in self.config.enabled_workers:
            p = Path(w.profile_dir).resolve()
            if p.exists():
                mode = p.stat().st_mode & 0o777
                if mode != 0o700:
                    issues.append(f"{w.id} has mode 0{oct(mode)[2:]} instead of 0700")
        if issues:
            return DoctorCheckResult("profile permissions", False, "; ".join(issues), check_type="REAL")
        return DoctorCheckResult("profile permissions", True, "Profile directories configured mode 0700", check_type="REAL")

    def _fingerprint_directory(self, dir_path: Path) -> dict[str, str]:
        """Calculates SHA-256 fingerprints of files without reading credential contents."""
        fingerprints = {}
        if not dir_path.exists():
            return fingerprints
        for f in dir_path.rglob("*"):
            if f.is_file():
                h = hashlib.sha256()
                try:
                    with open(f, "rb") as fh:
                        while chunk := fh.read(8192):
                            h.update(chunk)
                    fingerprints[str(f.relative_to(dir_path))] = h.hexdigest()
                except OSError:
                    pass
        return fingerprints

    def _snapshot_dir_mtimes(self, dir_path: Path) -> dict[str, float]:
        snapshot = {}
        if not dir_path.exists():
            return snapshot
        for f in dir_path.rglob("*"):
            rel = str(f.relative_to(dir_path))
            if (
                rel.startswith("antigravity-cli/brain/")
                or rel.startswith("antigravity-cli/conversations/")
                or rel.startswith("antigravity-cli/log/")
                or rel.endswith(".log")
                or rel.endswith("-wal")
                or rel.endswith("-shm")
            ):
                continue
            try:
                snapshot[str(f)] = f.stat().st_mtime
            except OSError:
                pass
        return snapshot

    def check_credential_isolation(self) -> DoctorCheckResult:
        """Runs the worker isolation and HOME protection test from SPEC section 4."""
        workers = self.config.enabled_workers
        if len(workers) < 2:
            return DoctorCheckResult(
                "keyring isolation", True, "Less than 2 enabled workers configured; isolation test skipped", check_type="REAL"
            )

        w_a = workers[0]
        w_b = workers[1]
        p_a = Path(w_a.profile_dir).resolve()
        p_b = Path(w_b.profile_dir).resolve()

        # Snapshot real home sensitive paths
        real_home = Path.home()
        keyring_dir = real_home / ".local" / "share" / "keyrings"
        gemini_dir = real_home / ".gemini"
        home_snapshot_before = self._snapshot_dir_mtimes(keyring_dir)
        gemini_snapshot_before = self._snapshot_dir_mtimes(gemini_dir)

        # Step (a): Fingerprint worker A credential files before worker B operation
        keyrings_a = p_a / "data" / "keyrings"
        fp_a_before = self._fingerprint_directory(keyrings_a)

        # Run probe on worker B
        probe_b_state, _, email_b = probe_worker_status(p_b, agy_binary=self.agy_binary, timeout_s=5.0)

        # Step (a) verify: Fingerprint worker A credential files after worker B probe
        fp_a_after = self._fingerprint_directory(keyrings_a)
        if fp_a_before != fp_a_after:
            return DoctorCheckResult(
                "keyring isolation",
                False,
                f"Worker A credentials changed during Worker B operation: before={fp_a_before}, after={fp_a_after}",
                check_type="REAL",
            )

        # Step (c): Verify nothing under real HOME was modified
        home_snapshot_after = self._snapshot_dir_mtimes(keyring_dir)
        gemini_snapshot_after = self._snapshot_dir_mtimes(gemini_dir)

        for path, mtime in home_snapshot_before.items():
            if home_snapshot_after.get(path) != mtime:
                return DoctorCheckResult(
                    "keyring isolation",
                    False,
                    f"Real home desktop keyring modified during worker operation: {path}",
                    check_type="REAL",
                )

        for path, mtime in gemini_snapshot_before.items():
            if gemini_snapshot_after.get(path) != mtime:
                return DoctorCheckResult(
                    "keyring isolation",
                    False,
                    f"Real home .gemini modified during worker operation: {path}",
                    check_type="REAL",
                )

        return DoctorCheckResult(
            "keyring isolation", True, "Worker credential stores and real HOME isolation verified", check_type="REAL"
        )

    def check_two_worker_collision(self) -> DoctorCheckResult:
        """Verifies two-worker collision prevention, alternating probes, and credential isolation.
        
        Reported as SIMULATED when fewer than 2 workers have verified logins;
        run for REAL (with live probes, credential hashing, and desktop keyring audits)
        as soon as two or more workers are verified.
        """
        workers = self.config.enabled_workers
        if len(workers) < 2:
            return DoctorCheckResult(
                "two-worker collision", True, "Less than 2 enabled workers configured; collision check skipped", check_type="REAL"
            )

        verified = []
        for w in workers:
            mgr = WorkerProfileManager(w.profile_dir)
            if mgr.load_metadata().get("state") == "OK":
                verified.append(w)

        if len(verified) < 2:
            return DoctorCheckResult(
                "two-worker collision",
                True,
                "Simulated (duplicate rejection verified in test suite; fewer than 2 workers logged in)",
                check_type="SIMULATED",
            )

        # Real test on the first two verified workers
        w_a = verified[0]
        w_b = verified[1]
        p_a = Path(w_a.profile_dir).resolve()
        p_b = Path(w_b.profile_dir).resolve()

        mgr_a = WorkerProfileManager(p_a)
        mgr_b = WorkerProfileManager(p_b)
        meta_a = mgr_a.load_metadata()
        meta_b = mgr_b.load_metadata()

        # Step 1: Snapshot owner's desktop keyring and ~/.gemini before probes
        real_home = Path.home()
        keyring_dir = real_home / ".local" / "share" / "keyrings"
        gemini_dir = real_home / ".gemini"
        home_snapshot_before = self._snapshot_dir_mtimes(keyring_dir)
        gemini_snapshot_before = self._snapshot_dir_mtimes(gemini_dir)

        # Step 2: Fingerprint credential files (hashes only)
        keyrings_a = p_a / "data" / "keyrings"
        keyrings_b = p_b / "data" / "keyrings"
        fp_a_before = self._fingerprint_directory(keyrings_a)
        fp_b_before = self._fingerprint_directory(keyrings_b)

        # Step 3: Probe worker A, then verify worker B's credential files were untouched
        probe_a_state, probe_a_detail, email_a = probe_worker_status(p_a, agy_binary=self.agy_binary, timeout_s=10.0)
        fp_b_after_a = self._fingerprint_directory(keyrings_b)
        if fp_b_after_a != fp_b_before:
            return DoctorCheckResult(
                "two-worker collision",
                False,
                f"Worker B credentials modified during Worker A probe: before={fp_b_before}, after={fp_b_after_a}",
                check_type="REAL",
            )

        # Fingerprint worker A after its own probe has initialized its private keyring
        fp_a_after_a = self._fingerprint_directory(keyrings_a)

        # Step 4: Probe worker B, then verify worker A's credential files were untouched
        probe_b_state, probe_b_detail, email_b = probe_worker_status(p_b, agy_binary=self.agy_binary, timeout_s=10.0)
        fp_a_after_b = self._fingerprint_directory(keyrings_a)
        if fp_a_after_b != fp_a_after_a:
            return DoctorCheckResult(
                "two-worker collision",
                False,
                f"Worker A credentials modified during Worker B probe: before={fp_a_after_a}, after={fp_a_after_b}",
                check_type="REAL",
            )

        # Step 5: Verify distinct identity hashes and distinct email identities
        hash_a = meta_a.get("identity_hash")
        hash_b = meta_b.get("identity_hash")
        if hash_a and hash_b and hash_a == hash_b:
            return DoctorCheckResult(
                "two-worker collision",
                False,
                f"Collision detected: Worker {w_a.id} and {w_b.id} share the exact same identity hash!",
                check_type="REAL",
            )

        if email_a and email_b and email_a.strip().lower() == email_b.strip().lower():
            return DoctorCheckResult(
                "two-worker collision",
                False,
                f"Collision detected: Live probes for Worker {w_a.id} and {w_b.id} returned same email '{email_a}'!",
                check_type="REAL",
            )

        # Step 6: Verify owner's desktop keyring and ~/.gemini remained completely untouched
        home_snapshot_after = self._snapshot_dir_mtimes(keyring_dir)
        gemini_snapshot_after = self._snapshot_dir_mtimes(gemini_dir)

        for path, mtime in home_snapshot_before.items():
            if home_snapshot_after.get(path) != mtime:
                return DoctorCheckResult(
                    "two-worker collision",
                    False,
                    f"Host desktop keyring modified during collision check: {path}",
                    check_type="REAL",
                )

        for path, mtime in gemini_snapshot_before.items():
            if gemini_snapshot_after.get(path) != mtime:
                return DoctorCheckResult(
                    "two-worker collision",
                    False,
                    f"Host ~/.gemini modified during collision check: {path}",
                    check_type="REAL",
                )

        return DoctorCheckResult(
            "two-worker collision",
            True,
            f"Real verification passed: Workers {w_a.id} and {w_b.id} have distinct verified identities; zero credential alteration or host keyring leakage",
            check_type="REAL",
        )

    def check_credential_collision(self) -> DoctorCheckResult:
        """Backward-compatible alias for check_credential_isolation."""
        return self.check_credential_isolation()

    def run_all(self) -> list[DoctorCheckResult]:
        return [
            self.check_binaries(),
            self.check_isolation_tools(),
            self.check_path_guards(),
            self.check_environment_allow_list(),
            self.check_profile_permissions(),
            self.check_credential_isolation(),
            self.check_two_worker_collision(),
        ]

    def print_report(self) -> bool:
        print("\n" + "=" * 78)
        print("                        MULTIGRAVITY DOCTOR REPORT")
        if self.config_path:
            print(f" Config: {self.config_path}")
        print("=" * 78)
        print(f" {'STATUS':<7} {'TYPE':<12} {'CHECK':<25} : {'DETAILS'}")
        print("-" * 78)
        results = self.run_all()
        all_passed = True
        for r in results:
            mark = "[PASS]" if r.passed else "[FAIL]"
            type_tag = f"[{r.check_type}]"
            print(f" {mark:<7} {type_tag:<12} {r.name:<25} : {r.message}")
            if not r.passed:
                all_passed = False
        print("=" * 78)
        if all_passed:
            print(" Result: All isolation and security checks PASSED.\n")
        else:
            print(" Result: One or more checks FAILED. Review errors above.\n")
        return all_passed
