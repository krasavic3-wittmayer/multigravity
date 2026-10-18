"""System dependency preflight and package guidance for multigravity."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


# Mapping of command binary to Arch Linux package name
ARCH_PACKAGE_MAP = {
    "dbus-run-session": "dbus",
    "gnome-keyring-daemon": "gnome-keyring",
    "secret-tool": "libsecret",
    "xdg-open": "xdg-utils",
    "bash": "bash",
}

MIN_PYTHON_VERSION = (3, 10)


class PreflightError(RuntimeError):
    """Raised when one or more system dependencies are missing."""
    pass


def check_python_version() -> tuple[bool, str]:
    current = sys.version_info[:2]
    if current < MIN_PYTHON_VERSION:
        return (
            False,
            f"Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+ required, but found "
            f"Python {current[0]}.{current[1]} ({sys.executable}). "
            "Please update python: sudo pacman -S python",
        )
    return True, f"Python {current[0]}.{current[1]} OK"


def check_dependencies(
    agy_binary: str | None = None,
) -> tuple[bool, list[str], list[str], str]:
    """Checks all required system dependencies.
    
    Returns:
        (is_ok, missing_binaries, missing_packages, advice_message)
    """
    missing_bins: list[str] = []
    missing_pkgs: list[str] = []

    # 1. Python check
    py_ok, py_msg = check_python_version()
    if not py_ok:
        missing_bins.append(f"python>={MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}")
        missing_pkgs.append("python")

    # 2. agy binary check
    agy_path = agy_binary or shutil.which("agy") or "/usr/sbin/agy"
    if not Path(agy_path).exists() and not shutil.which("agy"):
        missing_bins.append("agy")

    # 3. System isolation tools check
    for binary, pkg in ARCH_PACKAGE_MAP.items():
        if not shutil.which(binary):
            missing_bins.append(binary)
            if pkg not in missing_pkgs:
                missing_pkgs.append(pkg)

    if not missing_bins:
        return True, [], [], "All required system dependencies are installed."

    lines = ["[!] MULTIGRAVITY PREFLIGHT ERROR: Missing system dependencies."]
    if "agy" in missing_bins:
        lines.append("  • 'agy' (Google Antigravity CLI) was not found in PATH or at /usr/sbin/agy.")
        lines.append("    Please verify your Antigravity installation.")

    other_missing = [b for b in missing_bins if b != "agy" and not b.startswith("python")]
    if other_missing:
        lines.append(f"  • Missing system binaries: {', '.join(other_missing)}")
    if missing_pkgs:
        pkg_list = " ".join(missing_pkgs)
        lines.append("\nTo install the missing packages on Arch Linux, run:")
        lines.append(f"    sudo pacman -S {pkg_list}")

    return False, missing_bins, missing_pkgs, "\n".join(lines)


def verify_dependencies_or_raise(agy_binary: str | None = None) -> None:
    """Raises PreflightError if any required dependency is missing."""
    ok, missing_bins, missing_pkgs, msg = check_dependencies(agy_binary)
    if not ok:
        raise PreflightError(msg)
