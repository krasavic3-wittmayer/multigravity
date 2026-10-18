"""Worker authentication and status probe logic."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from multigravity.isolation import (
    EnvironmentAllowList,
    extract_email_from_token_payload,
    mask_email,
)


def get_isolated_runner_prefix(runtime_dir: Path) -> list[str]:
    """Returns dbus-run-session and keyring unlock prefix if binaries exist."""
    dbus_bin = shutil.which("dbus-run-session")
    keyring_bin = shutil.which("gnome-keyring-daemon")
    if dbus_bin and keyring_bin:
        keyring_ctrl = runtime_dir / "keyring"
        cmd_str = (
            f'eval $(echo "" | {keyring_bin} --unlock --components=secrets '
            f'--control-directory="{keyring_ctrl}" 2>/dev/null); exec "$@"'
        )
        return [dbus_bin, "--", "bash", "-c", cmd_str, "--"]
    return []


def probe_worker_status(
    profile_dir: Path,
    agy_binary: str | None = None,
    timeout_s: float = 15.0,
) -> tuple[str, str, str | None]:
    """Probes a worker profile.
    
    Returns:
        (state, detail_message, detected_masked_email_or_None)
    """
    profile_dir = Path(profile_dir).resolve()
    if not profile_dir.exists():
        return "NOT_LOGGED_IN", f"Profile directory {profile_dir} does not exist", None

    agy_path = agy_binary or shutil.which("agy") or "/usr/sbin/agy"
    if not Path(agy_path).exists():
        return "UNKNOWN", f"agy binary not found at {agy_path}", None

    runtime_dir = profile_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    env = EnvironmentAllowList.build_worker_env(profile_dir)
    prefix = get_isolated_runner_prefix(runtime_dir)

    # 1. Probe using agy models
    full_cmd = prefix + [agy_path, "models"]
    try:
        proc = subprocess.run(
            full_cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return "UNKNOWN", f"Probe timed out after {timeout_s}s", None
    except Exception as e:
        return "UNKNOWN", str(e), None

    combined = (proc.stdout + " " + proc.stderr).lower()

    # Try extracting email from isolated keyring
    detected_email = None
    secret_tool_bin = shutil.which("secret-tool")
    if secret_tool_bin and prefix:
        try:
            sec_cmd = prefix + [secret_tool_bin, "lookup", "service", "gemini", "username", "antigravity"]
            sec_proc = subprocess.run(sec_cmd, env=env, capture_output=True, text=True, timeout=5)
            if sec_proc.returncode == 0 and sec_proc.stdout:
                raw_email = extract_email_from_token_payload(sec_proc.stdout)
                if raw_email:
                    detected_email = mask_email(raw_email)
                del raw_email
        except Exception:
            pass

    # Success check
    if proc.returncode == 0 and "gemini" in combined:
        return "OK", "Worker profile is authenticated and active", detected_email

    if "you are not logged into antigravity" in combined or "not logged in" in combined:
        return "NOT_LOGGED_IN", "Profile is not logged into Antigravity", detected_email

    # Rate limiting
    if "429" in combined or "resource_exhausted" in combined or "rate limit" in combined or "quota exceeded" in combined:
        return "RATE_LIMITED", "Worker is currently rate-limited (429)", detected_email

    # Auth expiry
    auth_sigs = [
        "oauth2: cannot fetch token",
        "keyring error",
        "could not find default credentials",
        "unauthenticated",
        "authentication failed",
        "token expired",
    ]
    if any(sig in combined for sig in auth_sigs):
        return "AUTH_EXPIRED", "Authentication session has expired", detected_email

    return "UNKNOWN", f"Process exited with code {proc.returncode}: {proc.stderr[:200]}", detected_email
