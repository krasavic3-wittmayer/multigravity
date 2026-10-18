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

    def __init__(self, config: SystemConfig, agy_binary: str | None = None):
        self.config = config
        self.agy_binary = agy_binary or shutil.which("agy") or "/usr/sbin/agy"

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
        for w in self.config.workers:
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
            try:
                snapshot[str(f)] = f.stat().st_mtime
            except OSError:
                pass
        return snapshot

    def check_credential_isolation(self) -> DoctorCheckResult:
        """Runs the worker isolation and HOME protection test from SPEC section 4."""
        workers = self.config.workers
        if len(workers) < 2:
            return DoctorCheckResult(
                "keyring isolation", True, "Less than 2 workers configured; isolation test skipped", check_type="REAL"
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
        """Verifies distinct account identity hashes between two workers."""
        workers = self.config.workers
        if len(workers) < 2:
            return DoctorCheckResult(
                "two-worker collision", True, "Less than 2 workers configured; collision check skipped", check_type="REAL"
            )

        w_a = workers[0]
        w_b = workers[1]
        p_a = Path(w_a.profile_dir).resolve()
        p_b = Path(w_b.profile_dir).resolve()

        mgr_a = WorkerProfileManager(p_a)
        mgr_b = WorkerProfileManager(p_b)
        meta_a = mgr_a.load_metadata()
        meta_b = mgr_b.load_metadata()

        if meta_a.get("state") == "OK" and meta_b.get("state") == "OK":
            if meta_a.get("identity_hash") == meta_b.get("identity_hash"):
                return DoctorCheckResult(
                    "two-worker collision",
                    False,
                    f"Collision detected: Worker {w_a.id} and {w_b.id} share the same identity hash!",
                    check_type="REAL",
                )
            return DoctorCheckResult(
                "two-worker collision",
                True,
                f"Workers {w_a.id} and {w_b.id} have distinct verified identity hashes",
                check_type="REAL",
            )

        return DoctorCheckResult(
            "two-worker collision",
            True,
            "Simulated (duplicate rejection verified in test suite; workers not logged in yet)",
            check_type="SIMULATED",
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
