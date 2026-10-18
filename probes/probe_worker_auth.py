#!/usr/bin/env python3
"""probe_worker_auth.py - Safe probe to check authentication status of a worker profile.

Rules:
1. NEVER prints, logs, or stores credentials or tokens.
2. Checks exit code and standard error signatures for:
   - Success
   - Session/Auth expiry (AUTH_EXPIRED)
   - Rate limiting / 429 (COOLDOWN)
   - Network or other transient issues.
3. Operates strictly inside the isolated worker profile directory.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def check_worker_profile(profile_dir: Path, timeout_s: int = 15) -> dict:
    profile_dir = profile_dir.resolve()
    if not profile_dir.exists():
        return {"status": "ERROR", "message": f"Profile dir {profile_dir} does not exist"}

    home_dir = profile_dir / "home"
    if not home_dir.exists():
        return {"status": "ERROR", "message": f"Profile home {home_dir} does not exist"}

    agy_path = shutil.which("agy")
    if not agy_path:
        return {"status": "ERROR", "message": "agy executable not found in PATH"}

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    env["XDG_DATA_HOME"] = str(home_dir / ".local/share")
    env["XDG_CACHE_HOME"] = str(home_dir / ".cache")
    env["XDG_STATE_HOME"] = str(home_dir / ".local/state")
    env["XDG_RUNTIME_DIR"] = str(home_dir / ".runtime")

    # Ensure runtime dir exists with 0700
    (home_dir / ".runtime").mkdir(mode=0o700, exist_ok=True)

    # Run a cheap models check: 'agy models' queries the API with the profile's auth
    # without burning generation tokens or creating conversation history.
    try:
        proc = subprocess.run(
            [agy_path, "models"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "message": f"Profile probe timed out after {timeout_s}s"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}

    combined = (proc.stdout + " " + proc.stderr).lower()

    # Analyze output for known signals
    if proc.returncode == 0 and "gemini" in combined:
        return {"status": "AUTHENTICATED", "message": "Worker profile is authenticated and active."}

    # Auth expiry signatures
    auth_signatures = [
        "oauth2: cannot fetch token",
        "keyring error",
        "could not find default credentials",
        "unauthenticated",
        "login required",
        "permissiondenied",
        "authentication failed",
        "token expired",
    ]
    for sig in auth_signatures:
        if sig in combined:
            return {
                "status": "AUTH_EXPIRED",
                "message": f"Detected auth/session expiry signature: '{sig}'",
            }

    # Rate limiting signatures
    rate_signatures = [
        "429",
        "resource_exhausted",
        "quota exceeded",
        "rate limit",
        "too many requests",
    ]
    for sig in rate_signatures:
        if sig in combined:
            return {
                "status": "RATE_LIMITED",
                "message": f"Detected rate limiting signal: '{sig}'",
            }

    return {
        "status": "FAILED",
        "message": f"Command returned exit code {proc.returncode} with unspecified error.",
        "returncode": proc.returncode,
    }


def main():
    parser = argparse.ArgumentParser(description="Probe a worker profile's authentication status safely.")
    parser.add_argument("profile_dir", type=Path, help="Path to worker profile directory")
    parser.add_argument("--timeout", type=int, default=15, help="Probe timeout in seconds")
    args = parser.parse_args()

    result = check_worker_profile(args.profile_dir, timeout_s=args.timeout)
    print(f"Status: {result['status']}")
    print(f"Detail: {result['message']}")
    if result["status"] != "AUTHENTICATED":
        sys.exit(1)


if __name__ == "__main__":
    main()
