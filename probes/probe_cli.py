#!/usr/bin/env python3
"""probe_cli.py - Non-invasive probe for agy CLI capabilities and environment isolation.

This script does NOT invoke model generation or burn tokens.
It checks:
1. agy binary availability and version info.
2. CLI flags and subcommands (print mode, JSON output, MCP support, permissions).
3. Environment isolation behavior (custom HOME, XDG directories).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run_cmd(args: list[str], env: dict[str] | None = None) -> tuple[int, str, str]:
    res = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env or os.environ,
    )
    return res.returncode, res.stdout, res.stderr


def probe_cli_binary() -> dict:
    agy_path = shutil.which("agy")
    if not agy_path:
        return {"found": False, "path": None}
    
    code, stdout, stderr = run_cmd([agy_path, "--help"])
    help_text = stdout + stderr

    has_print = "-p" in help_text or "--print" in help_text
    has_output_format = "--output-format" in help_text
    has_skip_perms = "--dangerously-skip-permissions" in help_text
    has_json_schema = "--json-schema" in help_text
    has_mcp = "mcp" in help_text

    return {
        "found": True,
        "path": agy_path,
        "help_exit_code": code,
        "features": {
            "print_mode": has_print,
            "output_format": has_output_format,
            "skip_permissions": has_skip_perms,
            "json_schema": has_json_schema,
            "mcp_subcommand": has_mcp,
        },
    }


def probe_mcp_support(agy_path: str) -> dict:
    code, stdout, stderr = run_cmd([agy_path, "mcp", "add", "--help"])
    text = stdout + stderr
    supports_stdio = "stdio" in text
    supports_http = "http" in text
    return {
        "mcp_add_available": code == 0,
        "supports_stdio": supports_stdio,
        "supports_http": supports_http,
    }


def probe_env_isolation(agy_path: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="multigravity_probe_") as tmp_dir:
        profile_home = Path(tmp_dir) / "home"
        profile_home.mkdir(mode=0o700, parents=True)
        isolated_env = os.environ.copy()
        isolated_env["HOME"] = str(profile_home)
        isolated_env["XDG_CONFIG_HOME"] = str(profile_home / ".config")
        isolated_env["XDG_DATA_HOME"] = str(profile_home / ".local/share")
        isolated_env["XDG_CACHE_HOME"] = str(profile_home / ".cache")
        isolated_env["XDG_STATE_HOME"] = str(profile_home / ".local/state")

        code, stdout, stderr = run_cmd([agy_path, "--help"], env=isolated_env)
        # Verify if agy runs cleanly under a custom HOME without crashing
        return {
            "exit_code": code,
            "ran_cleanly": code == 0,
            "created_dirs": [str(p.relative_to(profile_home)) for p in profile_home.rglob("*")],
        }


def main():
    print("=== Multigravity Phase 0 CLI Probe ===")
    cli_info = probe_cli_binary()
    print(f"CLI Binary: {cli_info}")

    if not cli_info["found"]:
        print("ERROR: agy binary not found in PATH.")
        sys.exit(1)

    mcp_info = probe_mcp_support(cli_info["path"])
    print(f"MCP Support: {mcp_info}")

    iso_info = probe_env_isolation(cli_info["path"])
    print(f"Environment Isolation: {iso_info}")

    print("\nSummary:")
    print("✓ agy CLI detected and functional")
    print(f"✓ Headless print mode: {cli_info['features']['print_mode']}")
    print(f"✓ Permission bypass: {cli_info['features']['skip_permissions']}")
    print(f"✓ MCP stdio transport: {mcp_info['supports_stdio']}")
    print(f"✓ MCP http transport: {mcp_info['supports_http']}")
    print(f"✓ Environment isolation clean: {iso_info['ran_cleanly']}")


if __name__ == "__main__":
    main()
