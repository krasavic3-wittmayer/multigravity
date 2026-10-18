"""Launcher preflight, health-check, auto-healing, and prompt generation for mgy."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from multigravity.config import SystemConfig
from multigravity.cli import (
    get_daemon_pid_path,
    is_daemon_running,
    load_system_config,
    resolve_config_path,
)
from multigravity.isolation import PathGuard
from multigravity.preflight import PreflightError, verify_dependencies_or_raise
from multigravity.probes import probe_worker_status
from multigravity.prompts import generate_main_agent_prompt
from multigravity.wizard import LoginWizard, format_worker_display


def update_mcp_instructions(prompt_text: str):
    """Updates instructions.md in ~/.gemini/antigravity-cli/mcp/multigravity if present."""
    mcp_dir = Path.home() / ".gemini" / "antigravity-cli" / "mcp" / "multigravity"
    if mcp_dir.exists() and mcp_dir.is_dir():
        inst_file = mcp_dir / "instructions.md"
        try:
            inst_file.write_text(prompt_text, encoding="utf-8")
        except OSError:
            pass


def auto_heal_stale_processes(config: SystemConfig):
    """Cleans up stale socket and PID files if the daemon process is no longer running."""
    pid_file = get_daemon_pid_path()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            # Process is not running; remove stale pid file
            try:
                pid_file.unlink()
            except OSError:
                pass
            if Path(config.socket_path).exists():
                try:
                    Path(config.socket_path).unlink()
                except OSError:
                    pass
        except PermissionError:
            pass

    # Clean stale wizard locks if dead
    wizard_lock = Path.home() / ".multigravity" / "state" / "multigravity_wizard.lock"
    if wizard_lock.exists():
        try:
            lock_pid = int(wizard_lock.read_text().strip())
            os.kill(lock_pid, 0)
        except (ValueError, ProcessLookupError):
            try:
                wizard_lock.unlink()
            except OSError:
                pass
        except PermissionError:
            pass


def fix_profile_permissions(config: SystemConfig):
    """Ensures worker profile directories are set to mode 0700."""
    for w in config.workers:
        p = Path(w.profile_dir)
        if p.exists():
            try:
                os.chmod(p, 0o700)
                for sub in ("home", "config", "data", "cache", "state", "runtime", "tmp"):
                    sub_p = p / sub
                    if sub_p.exists():
                        os.chmod(sub_p, 0o700)
            except OSError:
                pass


def ensure_workers_authenticated(config: SystemConfig, config_path: Path) -> bool:
    """Checks worker authentication states and prompts user to log in if needed."""
    enabled_workers = [w for w in config.workers if w.enabled]
    for w in enabled_workers:
        state, detail, _ = probe_worker_status(Path(w.profile_dir), timeout_s=5.0)
        if state not in ("OK", "RATE_LIMITED"):
            print("\n" + "=" * 70)
            print(f"[!] Worker {format_worker_display(w.id, w.label)} requires authentication ({state}: {detail}).")
            print(f"Launching interactive login wizard for {w.id}...")
            print("=" * 70 + "\n")
            wizard = LoginWizard(config=config, config_path=config_path)
            ok = wizard.run_wizard(target_worker_id=w.id)
            if not ok:
                print(f"[!] Failed to authenticate worker {w.id}.")
                return False
    return True


def ensure_daemon_running(config: SystemConfig, config_path: Path) -> bool:
    """Ensures core daemon is running on the configured socket path."""
    running = asyncio.run(is_daemon_running(config.socket_path))
    if running:
        return True

    print(f"Starting Multigravity core daemon in background ({config.socket_path})...")
    pid_file = get_daemon_pid_path()
    proc = subprocess.Popen(
        [sys.executable, "-m", "multigravity.daemon", str(config_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid))

    # Wait up to 3 seconds for socket to become ready
    for _ in range(30):
        time.sleep(0.1)
        if asyncio.run(is_daemon_running(config.socket_path)):
            print(f"[✓] Core daemon active (PID {proc.pid}).")
            return True

    print("[!] Timed out waiting for core daemon to bind socket.")
    return False


def run_preflight_and_prepare() -> tuple[bool, str]:
    """Runs all preflight checks, heals environment, verifies auth, starts daemon, and returns prompt."""
    try:
        verify_dependencies_or_raise()
    except PreflightError as e:
        print(f"[!] Preflight check failed:\n{e}", file=sys.stderr)
        return False, ""

    try:
        config, config_path = load_system_config()
    except Exception as e:
        print(f"[!] Configuration error:\n{e}", file=sys.stderr)
        return False, ""

    # 1. Auto-heal stale locks & fix permissions
    auto_heal_stale_processes(config)
    fix_profile_permissions(config)

    # 2. Check and ensure workers are authenticated
    if not ensure_workers_authenticated(config, config_path):
        return False, ""

    # 3. Ensure background daemon is running
    if not ensure_daemon_running(config, config_path):
        return False, ""

    # 4. Generate fleet prompt & update MCP instructions
    prompt = generate_main_agent_prompt(config, config_path)
    update_mcp_instructions(prompt)

    return True, prompt


def main():
    ok, prompt = run_preflight_and_prepare()
    if not ok:
        sys.exit(1)
    print(prompt)


if __name__ == "__main__":
    main()
