"""Main multigravity CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from multigravity.config import SystemConfig
from multigravity.doctor import Doctor
from multigravity.identity import MainIdentityError, save_main_identity
from multigravity.ipc import send_ipc_request
from multigravity.preflight import PreflightError, verify_dependencies_or_raise
from multigravity.wizard import LoginWizard, format_worker_display


def get_default_config_path() -> Path:
    candidates = [
        Path("config.local.toml"),
        Path("config.toml"),
        Path.home() / ".config" / "multigravity" / "config.toml",
        Path("config.example.toml"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return Path("config.local.toml")


def load_system_config(config_arg: str | None = None) -> SystemConfig:
    p = Path(config_arg) if config_arg else get_default_config_path()
    if p.exists():
        return SystemConfig.from_file(p)
    return SystemConfig()


def get_daemon_pid_path() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "multigravity_daemon.pid"
    return Path("/tmp") / f"multigravity_daemon_{os.getuid()}.pid"


async def is_daemon_running(socket_path: str) -> bool:
    if not Path(socket_path).exists():
        return False
    try:
        await send_ipc_request(socket_path, "list_workers", {}, timeout_s=1.0)
        return True
    except Exception:
        return False


def cmd_status(config: SystemConfig):
    wizard = LoginWizard(config=config)
    wizard.print_status_board()


def cmd_doctor(config: SystemConfig) -> int:
    doc = Doctor(config=config)
    success = doc.print_report()
    return 0 if success else 1


def cmd_set_main_identity(config: SystemConfig) -> int:
    print("=== Multigravity: Register Main Account (Account 1) Identity ===")
    print("Enter the Google email address for your interactive Main Account (Account 1).")
    print("NOTE: The email itself is NEVER saved, logged, or sent anywhere.")
    print("Only a salted SHA-256 hash will be stored in ~/.multigravity/state/main_identity.json (mode 0600).\n")
    try:
        email = input("Account 1 Google email address: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1

    try:
        meta = save_main_identity(email)
        print(f"\n[✓] Main Account (Account 1) identity recorded: {meta['masked_identity']}")
        print("    Salted hash stored securely in state directory (mode 0600).")
        print("    Collision prevention against Account 1 is now active.")
        return 0
    except MainIdentityError as e:
        print(f"\n[!] Error: {e}")
        return 1


def cmd_login(config: SystemConfig, worker_id: str) -> int:
    valid_ids = [w.id for w in config.workers]
    worker_displays = [format_worker_display(w.id, w.label) for w in config.workers]
    if worker_id not in valid_ids:
        print(f"Error: Unknown worker ID '{worker_id}'. Configured workers: {', '.join(worker_displays)}")
        return 1

    try:
        wizard = LoginWizard(config=config)
        success = wizard.run_wizard(target_worker_id=worker_id)
        return 0 if success else 1
    except PreflightError as e:
        print(str(e))
        return 1


def cmd_stop(config: SystemConfig) -> int:
    pid_file = get_daemon_pid_path()
    stopped = False

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon process (PID {pid}). Waiting for clean shutdown...")
            for _ in range(30):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    stopped = True
                    break
            if not stopped:
                os.kill(pid, signal.SIGKILL)
                stopped = True
            try:
                pid_file.unlink()
            except OSError:
                pass
        except (ValueError, OSError):
            pass

    # Check socket as well
    if Path(config.socket_path).exists():
        try:
            Path(config.socket_path).unlink()
        except OSError:
            pass

    if stopped:
        print("[✓] multigravity daemon stopped cleanly.")
    else:
        print("Daemon is not currently running.")
    return 0


def cmd_default_flow(config: SystemConfig) -> int:
    """Preflight -> Login Wizard for unverified workers -> Start daemon -> Status board."""
    try:
        wizard = LoginWizard(config=config)

        # 1. Preflight & Wizard
        print("=== Multigravity Setup & Launch ===")
        wizard_ok = wizard.run_wizard()
        if not wizard_ok:
            print("[!] Login wizard did not complete. Exiting.")
            return 1

        # 2. Start Daemon or verify running
        daemon_running = asyncio.run(is_daemon_running(config.socket_path))
        if daemon_running:
            print(f"[✓] Core daemon is already running at {config.socket_path}.")
        else:
            print(f"Starting core daemon in background on {config.socket_path}...")
            pid_file = get_daemon_pid_path()
            proc = subprocess.Popen(
                [sys.executable, "-m", "multigravity.daemon", str(get_default_config_path())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            pid_file.write_text(str(proc.pid))
            time.sleep(0.5)
            print(f"[✓] Core daemon started with PID {proc.pid}.")

        # 3. Print final status board
        wizard.print_status_board()
        print("Next step: The interactive main agent (Account 1) can now submit tasks to sub-agents via MCP!")
        return 0
    except PreflightError as e:
        print(str(e))
        return 1


def main():
    parser = argparse.ArgumentParser(
        prog="multigravity",
        description="Lightweight local orchestration layer for Google AI Pro accounts.",
    )
    parser.add_argument("--config", "-c", type=str, help="Path to config.toml")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # login <worker-id>
    p_login = subparsers.add_parser("login", help="Log in or re-authenticate a single worker")
    p_login.add_argument("worker_id", type=str, help="Worker ID (e.g. worker-1)")

    # status
    subparsers.add_parser("status", help="Print status board of all workers")

    # doctor
    subparsers.add_parser("doctor", help="Run isolation audits and credential collision tests")

    # set-main-identity
    subparsers.add_parser(
        "set-main-identity",
        help="Register Main Account (Account 1) email as a salted hash to prevent collisions",
    )

    # stop
    subparsers.add_parser("stop", help="Stop running core daemon cleanly")

    args = parser.parse_args()
    config = load_system_config(args.config)

    if args.subcommand is None:
        sys.exit(cmd_default_flow(config))
    elif args.subcommand == "status":
        cmd_status(config)
    elif args.subcommand == "doctor":
        sys.exit(cmd_doctor(config))
    elif args.subcommand == "login":
        sys.exit(cmd_login(config, args.worker_id))
    elif args.subcommand == "set-main-identity":
        sys.exit(cmd_set_main_identity(config))
    elif args.subcommand == "stop":
        sys.exit(cmd_stop(config))


if __name__ == "__main__":
    main()
