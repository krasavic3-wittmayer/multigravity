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


def resolve_config_path(config_arg: str | None = None) -> Path:
    """Resolves config path: --config flag > MULTIGRAVITY_CONFIG env > ./config.local.toml.
    
    Fails if no local config is found, never falling back to config.example.toml.
    """
    if config_arg:
        p = Path(config_arg)
        if not p.exists():
            raise FileNotFoundError(f"Configuration file specified by --config not found: {p}")
        return p.resolve()

    env_config = os.environ.get("MULTIGRAVITY_CONFIG")
    if env_config:
        p = Path(env_config)
        if not p.exists():
            raise FileNotFoundError(f"Configuration file specified by MULTIGRAVITY_CONFIG not found: {p}")
        return p.resolve()

    local_path = Path("config.local.toml")
    if local_path.exists():
        return local_path.resolve()

    raise FileNotFoundError(
        "No configuration file found. Please create 'config.local.toml' (e.g. by copying config.example.toml):\n"
        "    cp config.example.toml config.local.toml\n"
        "Or specify a config file via --config <path> or MULTIGRAVITY_CONFIG=<path>."
    )


def load_system_config(config_arg: str | None = None) -> tuple[SystemConfig, Path]:
    p = resolve_config_path(config_arg)
    return SystemConfig.from_file(p), p


def get_default_config_path() -> Path:
    """Returns resolved config path or falls back to ./config.local.toml."""
    try:
        return resolve_config_path()
    except FileNotFoundError:
        return Path("config.local.toml").resolve()


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


def cmd_status(config: SystemConfig, config_path: Path):
    wizard = LoginWizard(config=config, config_path=config_path)
    wizard.print_status_board()


def cmd_doctor(config: SystemConfig, config_path: Path) -> int:
    doc = Doctor(config=config, config_path=config_path)
    success = doc.print_report()
    return 0 if success else 1


def cmd_set_main_identity(config: SystemConfig, config_path: Path) -> int:
    print(f"Using configuration: {config_path}")
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


def cmd_login(config: SystemConfig, config_path: Path, worker_id: str) -> int:
    worker = next((w for w in config.workers if w.id == worker_id), None)
    if not worker:
        worker_displays = [format_worker_display(w.id, w.label) for w in config.workers]
        print(f"Error: Unknown worker ID '{worker_id}'. Configured workers: {', '.join(worker_displays)}")
        return 1

    if not worker.enabled:
        print(f"Error: Worker '{worker_id}' is disabled in configuration ({config_path}).")
        print(f"To log in this worker, set 'enabled = true' in {config_path}.")
        return 1

    try:
        wizard = LoginWizard(config=config, config_path=config_path)
        success = wizard.run_wizard(target_worker_id=worker_id)
        return 0 if success else 1
    except PreflightError as e:
        print(str(e))
        return 1


def cmd_stop(config: SystemConfig, timeout_s: float = 4.0) -> int:
    from multigravity.cleanup import cleanup_multigravity

    print(f"Stopping multigravity daemon and worker agents (timeout: {timeout_s:.1f}s)...")
    res = cleanup_multigravity(config=config, timeout_s=timeout_s)
    return 0 if res.get("success") else 1


def cmd_default_flow(config: SystemConfig, config_path: Path) -> int:
    """Preflight -> Login Wizard for unverified workers -> Start daemon -> Status board."""
    try:
        wizard = LoginWizard(config=config, config_path=config_path)

        # 1. Preflight & Wizard
        print("=== Multigravity Setup & Launch ===")
        print(f"Configuration: {config_path}")
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
                [sys.executable, "-m", "multigravity.daemon", str(config_path)],
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
    p_stop = subparsers.add_parser("stop", help="Stop running core daemon and worker processes cleanly")
    p_stop.add_argument(
        "--timeout", "-t", type=float, default=4.0, help="Grace timeout in seconds before SIGKILL escalation (default: 4.0)"
    )

    args = parser.parse_args()
    try:
        config, config_path = load_system_config(args.config)
    except FileNotFoundError as e:
        print(f"[!] Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.subcommand is None:
        sys.exit(cmd_default_flow(config, config_path))
    elif args.subcommand == "status":
        cmd_status(config, config_path)
    elif args.subcommand == "doctor":
        sys.exit(cmd_doctor(config, config_path))
    elif args.subcommand == "login":
        sys.exit(cmd_login(config, config_path, args.worker_id))
    elif args.subcommand == "set-main-identity":
        sys.exit(cmd_set_main_identity(config, config_path))
    elif args.subcommand == "stop":
        sys.exit(cmd_stop(config, timeout_s=getattr(args, "timeout", 4.0)))


if __name__ == "__main__":
    main()
