"""Comprehensive cleanup and process lifecycle management for multigravity.

Provides graceful termination (SIGTERM) of the core daemon, worker agents,
subagents, and background helper processes, followed by escalation to SIGKILL
if any process persists beyond the grace timeout.
"""

from __future__ import annotations

import argparse
import glob
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from multigravity.config import SystemConfig


def is_process_alive(pid: int) -> bool:
    """Checks whether a process is alive and not in a zombie (Z) state."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    # On Linux, inspect /proc/<pid>/stat to check if it has transitioned to Zombie
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            fields = f.read().split()
            if len(fields) >= 3 and fields[2] == "Z":
                return False
    except OSError:
        return False

    return True


def get_process_cmdline(pid: int) -> str:
    """Reads /proc/<pid>/cmdline with null bytes replaced by spaces."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except Exception:
        return ""


def get_process_environ(pid: int) -> str:
    """Reads /proc/<pid>/environ as string."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def get_process_ppid(pid: int) -> int | None:
    """Reads PPID from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            fields = f.read().split()
            if len(fields) >= 4:
                return int(fields[3])
    except Exception:
        pass
    return None


def get_daemon_pid_path() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "multigravity_daemon.pid"
    return Path("/tmp") / f"multigravity_daemon_{os.getuid()}.pid"


def find_multigravity_processes(
    config: SystemConfig | None = None,
    parent_pid: int | None = None,
    exclude_pids: set[int] | None = None,
) -> list[int]:
    """Discovers PIDs of all running multigravity-related processes.
    
    Includes:
    - Direct children of parent_pid (e.g. main agy process launched by launcher)
    - Multigravity daemon process and all its child/grandchild processes
    - Multigravity MCP shim processes
    - Processes with HOME, ANTIGRAVITY_DATA_DIR, or keyring paths matching worker profiles
    """
    exclude = set(exclude_pids or [])
    exclude.add(os.getpid())
    exclude.add(os.getppid())
    if parent_pid:
        exclude.add(parent_pid)

    my_uid = os.getuid()
    matched: set[int] = set()

    # 1. Direct children of parent_pid if given
    if parent_pid:
        for p in glob.glob("/proc/[0-9]*"):
            try:
                pid = int(p.split("/")[-1])
                if pid in exclude:
                    continue
                if os.stat(p).st_uid != my_uid:
                    continue
                if get_process_ppid(pid) == parent_pid:
                    matched.add(pid)
            except Exception:
                pass

    # 2. Worker profile directories to scan for
    worker_paths: set[str] = set()
    if config and config.workers:
        for w in config.workers:
            try:
                worker_paths.add(str(Path(w.profile_dir).resolve()))
            except Exception:
                pass
    home_workers = Path.home() / ".multigravity" / "workers"
    if home_workers.exists():
        for d in home_workers.iterdir():
            if d.is_dir():
                worker_paths.add(str(d.resolve()))

    # 3. Known daemon PID from PID file
    pid_file = get_daemon_pid_path()
    if pid_file.exists():
        try:
            d_pid = int(pid_file.read_text().strip())
            if d_pid not in exclude and is_process_alive(d_pid):
                matched.add(d_pid)
        except Exception:
            pass

    # 4. Scan /proc for daemon, shims, worker environments, and keyrings
    for p in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(p.split("/")[-1])
            if pid in exclude or pid in matched:
                continue
            if os.stat(p).st_uid != my_uid:
                continue

            cmd = get_process_cmdline(pid)
            if not cmd:
                continue

            # Skip this cleanup process itself or test runners
            if "multigravity.cleanup" in cmd:
                continue

            # Core daemon
            if "multigravity.daemon" in cmd:
                matched.add(pid)
                continue

            # Orphaned MCP shim (ppid == 1 or parent not alive)
            if "multigravity.mcp_shim" in cmd:
                ppid = get_process_ppid(pid)
                if ppid == 1 or (ppid is not None and not is_process_alive(ppid)):
                    matched.add(pid)
                continue

            # Match worker profile paths in cmdline
            matched_wp = False
            for wp in worker_paths:
                if wp in cmd:
                    matched.add(pid)
                    matched_wp = True
                    break
            if matched_wp:
                continue

            # Match worker profile environment (HOME, ANTIGRAVITY_DATA_DIR, MULTIGRAVITY_PROFILE)
            env_str = get_process_environ(pid)
            if env_str:
                for wp in worker_paths:
                    if (
                        f"HOME={wp}" in env_str
                        or f"ANTIGRAVITY_DATA_DIR={wp}" in env_str
                        or f"MULTIGRAVITY_PROFILE={wp}" in env_str
                        or f"control-directory={wp}" in cmd
                    ):
                        matched.add(pid)
                        break

        except Exception:
            pass

    # 5. Recursively find any children/grandchildren of matched PIDs
    added = True
    while added:
        added = False
        for p in glob.glob("/proc/[0-9]*"):
            try:
                pid = int(p.split("/")[-1])
                if pid in matched or pid in exclude:
                    continue
                if os.stat(p).st_uid != my_uid:
                    continue
                ppid = get_process_ppid(pid)
                if ppid and ppid in matched:
                    matched.add(pid)
                    added = True
            except Exception:
                pass

    return sorted(matched)


def clean_kill_and_hard_kill(
    pids: list[int],
    timeout_s: float = 4.0,
    print_fn: Any = print,
) -> tuple[list[int], list[int]]:
    """Clean kills (SIGTERM) target PIDs, monitors for timeout_s, and hard kills (SIGKILL) any persisting processes.
    
    Returns:
        (clean_killed_pids, hard_killed_pids)
    """
    alive = [p for p in pids if is_process_alive(p)]
    if not alive:
        return [], []

    # Phase 1: Clean kill (SIGTERM to process groups and individual PIDs)
    for p in alive:
        try:
            pgid = os.getpgid(p)
            if pgid not in (os.getpgrp(), os.getpid()):
                os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass

    # Phase 2: Monitor and wait for clean exit
    start = time.monotonic()
    persisting: list[int] = []
    while time.monotonic() - start < max(0.5, timeout_s):
        still_alive = [p for p in alive if is_process_alive(p)]
        if not still_alive:
            break
        time.sleep(0.1)

    persisting = [p for p in alive if is_process_alive(p)]

    # Phase 3: Hard kill (SIGKILL) for anything persisting
    hard_killed: list[int] = []
    if persisting:
        for p in persisting:
            print_fn(f"[!] Process PID {p} persisted for >{timeout_s:.1f}s. Hard killing (SIGKILL)...")
            try:
                pgid = os.getpgid(p)
                if pgid not in (os.getpgrp(), os.getpid()):
                    os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
            hard_killed.append(p)

        # Brief pause to let kernel reap processes
        time.sleep(0.2)

    clean_killed = [p for p in alive if p not in hard_killed and not is_process_alive(p)]
    return sorted(clean_killed), sorted(hard_killed)


def cleanup_multigravity(
    config: SystemConfig | None = None,
    config_path: Path | None = None,
    timeout_s: float = 4.0,
    parent_pid: int | None = None,
    print_fn: Any = print,
) -> dict[str, Any]:
    """Performs full cleanup: terminates all daemon/worker processes and cleans sockets and locks."""
    # 1. Discover all active multigravity processes
    target_pids = find_multigravity_processes(config=config, parent_pid=parent_pid)

    # 2. Clean kill and escalate to hard kill if persisting
    clean_killed, hard_killed = clean_kill_and_hard_kill(
        target_pids,
        timeout_s=timeout_s,
        print_fn=print_fn,
    )

    # 3. Clean up runtime files (PID file, socket, locks)
    pid_file = get_daemon_pid_path()
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass

    if config and config.socket_path:
        try:
            Path(config.socket_path).unlink(missing_ok=True)
        except OSError:
            pass
    else:
        # Default socket path cleanup
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
        sock_path = (
            Path(xdg_runtime) / "multigravity.sock"
            if xdg_runtime
            else Path("/tmp") / f"multigravity_{os.getuid()}.sock"
        )
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass

    # Wizard lock file cleanup
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    lock_path = (
        Path(xdg_runtime) / "multigravity_wizard.lock"
        if xdg_runtime
        else Path("/tmp") / f"multigravity_wizard_{os.getuid()}.lock"
    )
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass

    # 4. Summary reporting
    if not clean_killed and not hard_killed:
        print_fn("✓ No running multigravity processes detected.")
    else:
        if clean_killed:
            print_fn(f"✓ Cleanly stopped {len(clean_killed)} process(es) (PIDs: {clean_killed}).")
        if hard_killed:
            print_fn(f"✓ Hard-killed {len(hard_killed)} persisting process(es) (PIDs: {hard_killed}).")

    return {
        "clean_killed": clean_killed,
        "hard_killed": hard_killed,
        "success": True,
    }


def main():
    parser = argparse.ArgumentParser(
        prog="multigravity-cleanup",
        description="Clean kill and hard kill escalation for Multigravity fleet processes.",
    )
    parser.add_argument("--config", "-c", type=str, help="Path to config.toml")
    parser.add_argument(
        "--timeout", "-t", type=float, default=4.0, help="Grace timeout in seconds before SIGKILL (default: 4.0)"
    )
    parser.add_argument(
        "--parent-pid", "-p", type=int, help="Optional parent launcher PID to reap direct children"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress status output"
    )

    args = parser.parse_args()
    print_fn = (lambda *a, **kw: None) if args.quiet else print

    config = None
    if args.config:
        p = Path(args.config)
        if p.exists():
            config = SystemConfig.from_file(p)
    elif Path("config.local.toml").exists():
        config = SystemConfig.from_file("config.local.toml")

    cleanup_multigravity(
        config=config,
        timeout_s=args.timeout,
        parent_pid=args.parent_pid,
        print_fn=print_fn,
    )


if __name__ == "__main__":
    main()
