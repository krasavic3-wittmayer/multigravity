"""Isolation primitives: PathGuard, Environment allow-list, profile management, and keyring isolation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class PathGuardError(ValueError):
    """Raised when a profile path violates isolation boundaries."""
    pass


def resolve_path(path: str | Path, home: Path | None = None) -> Path:
    """Resolves path expanding user (~) and environment variables without requiring absolute input."""
    p_str = str(path)
    h = (home or Path.home()).resolve()
    if p_str == "~":
        return h
    if p_str.startswith("~/"):
        return (h / p_str[2:]).resolve()
    expanded = os.path.expanduser(os.path.expandvars(p_str))
    return Path(expanded).resolve()


def format_display_path(path: str | Path) -> str:
    """Formats path relative to CWD or user home (~), avoiding absolute paths in UI output."""
    p = Path(path).resolve()
    cwd = Path.cwd().resolve()
    try:
        rel = p.relative_to(cwd)
        return str(rel)
    except ValueError:
        pass
    home = Path.home().resolve()
    try:
        rel_home = p.relative_to(home)
        return f"~/{rel_home}"
    except ValueError:
        pass
    return str(p)


class PathGuard:
    """Enforces strict path isolation boundaries for worker profile directories."""

    @staticmethod
    def validate_worker_profiles(
        profile_paths: list[str | Path],
        real_home: str | Path | None = None,
    ) -> list[Path]:
        resolved_home = Path(real_home or Path.home()).resolve()
        resolved_paths: list[Path] = []
        repo_root = Path(__file__).resolve().parent.parent

        # Standard sensitive user / agy directories that must never be a profile
        forbidden_locations = {
            resolved_home,
            (resolved_home / ".config").resolve(),
            (resolved_home / ".local").resolve(),
            (resolved_home / ".cache").resolve(),
            (resolved_home / ".gemini").resolve(),
            (resolved_home / ".ssh").resolve(),
            repo_root,
        }

        for p_input in profile_paths:
            p = resolve_path(p_input, home=resolved_home)

            # Rule: Path cannot equal real home or any parent of real home
            if p == resolved_home:
                raise PathGuardError(f"Profile path cannot equal user HOME: {p}")
            if p in resolved_home.parents:
                raise PathGuardError(f"Profile path cannot be a parent of user HOME: {p}")

            # Rule: Path cannot be a sensitive config directory or sit inside ~/.gemini
            if p in forbidden_locations:
                raise PathGuardError(f"Profile path cannot equal protected directory: {p}")

            gemini_dir = (resolved_home / ".gemini").resolve()
            if p == gemini_dir or gemini_dir in p.parents:
                raise PathGuardError(f"Profile path cannot sit inside default agy directory {gemini_dir}: {p}")

            resolved_paths.append(p)

        # Rule: Refuse duplicate paths and parent-child relations among workers
        for i, p1 in enumerate(resolved_paths):
            for j, p2 in enumerate(resolved_paths):
                if i == j:
                    continue
                if p1 == p2:
                    raise PathGuardError(f"Two workers share the exact same profile path: {p1}")
                if p1 in p2.parents:
                    raise PathGuardError(
                        f"Worker profile path '{p2}' is a child of worker profile path '{p1}'"
                    )

        return resolved_paths


class SecurityError(PermissionError):
    """Raised when an environment leaks sensitive credentials or tokens."""
    pass


class EnvironmentAllowList:
    """Builds a strictly filtered environment for worker child processes."""

    ALLOWED_PARENT_VARS = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "TZ",
    }

    FORBIDDEN_PATTERNS = [
        "API_KEY",
        "_KEY",
        "TOKEN",
        "SECRET",
        "AUTH",
        "CREDENTIAL",
        "PASSWORD",
        "GH_",
        "GITHUB_",
        "GEMINI_",
        "ANTHROPIC_",
        "OPENAI_",
        "GOOGLE_",
    ]

    @classmethod
    def filter_environment(
        cls,
        parent_env: dict[str, str] | None = None,
        custom_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Creates a clean child environment from the allow-list, blocking all secrets."""
        source = parent_env if parent_env is not None else os.environ
        clean_env: dict[str, str] = {}

        for k in cls.ALLOWED_PARENT_VARS:
            if k in source:
                clean_env[k] = source[k]

        # Defensive security check: Ensure no sensitive variable leaked in from parent
        for k in clean_env:
            upper_k = k.upper()
            if any(pat in upper_k for pat in cls.FORBIDDEN_PATTERNS):
                raise SecurityError(f"Leaked sensitive environment variable detected: {k}")

        if custom_overrides:
            for k, v in custom_overrides.items():
                upper_k = k.upper()
                # GNOME_KEYRING_CONTROL is a local socket path for private keyring daemon
                if upper_k == "GNOME_KEYRING_CONTROL":
                    clean_env[k] = v
                    continue
                if any(pat in upper_k for pat in cls.FORBIDDEN_PATTERNS):
                    raise SecurityError(f"Attempted to inject sensitive variable: {k}")
                clean_env[k] = v

        return clean_env

    @classmethod
    def build_worker_env(
        cls,
        profile_root: str | Path,
        parent_env: dict[str, str] | None = None,
        extra_vars: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Constructs an isolated execution environment pointing to worker directories."""
        profile_root = resolve_path(profile_root)
        home = profile_root / "home"
        config = profile_root / "config"
        data = profile_root / "data"
        cache = profile_root / "cache"
        state = profile_root / "state"
        runtime = profile_root / "runtime"
        tmp = profile_root / "tmp"

        for d in (home, config, data, cache, state, runtime, tmp, data / "keyrings"):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)

        overrides = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_DATA_HOME": str(data),
            "XDG_CACHE_HOME": str(cache),
            "XDG_STATE_HOME": str(state),
            "XDG_RUNTIME_DIR": str(runtime),
            "TMPDIR": str(tmp),
            "GNOME_KEYRING_CONTROL": str(runtime / "keyring"),
        }
        if extra_vars:
            overrides.update(extra_vars)

        return cls.filter_environment(parent_env=parent_env, custom_overrides=overrides)


def mask_email(email: str) -> str:
    """Masks email address for logs and status board (e.g. us***r@example.com)."""
    if not email or "@" not in email:
        return "[UNKNOWN]"
    user, domain = email.split("@", 1)
    if len(user) <= 2:
        masked_user = user[0] + "***"
    else:
        masked_user = user[:2] + "***" + user[-1]
    return f"{masked_user}@{domain}"


def extract_email_from_token_payload(secret_str: str) -> str | None:
    """Extracts email from OIDC id_token in stored OAuth secret without logging tokens."""
    import base64
    try:
        data = json.loads(secret_str)
        id_token = data.get("id_token")
        if not id_token:
            return None
        parts = id_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload_json = json.loads(decoded)
        return payload_json.get("email")
    except Exception:
        return None


def hash_identity(email: str, salt: str) -> str:
    """Returns SHA-256 salted hash of normalized identity."""
    normalized = email.strip().lower()
    return hashlib.sha256(f"{salt}:{normalized}".encode("utf-8")).hexdigest()


class WorkerProfileManager:
    """Manages worker profile filesystem layout, metadata, and identity hashes."""

    def __init__(self, profile_root: str | Path):
        self.root = resolve_path(profile_root)
        self.metadata_file = self.root / "profile_metadata.json"

    def ensure_structure(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for sub in ("home", "config", "data", "cache", "state", "runtime", "tmp", "data/keyrings"):
            p = self.root / sub
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(p, 0o700)

    def load_metadata(self) -> dict[str, Any]:
        if not self.metadata_file.exists():
            return {
                "state": "NOT_LOGGED_IN",
                "masked_identity": None,
                "identity_hash": None,
                "salt": None,
                "last_verified_at": None,
            }
        try:
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {
                "state": "UNKNOWN",
                "masked_identity": None,
                "identity_hash": None,
                "salt": None,
                "last_verified_at": None,
            }

    def save_verified_identity(self, email: str, salt: str | None = None) -> dict[str, Any]:
        salt = salt or hashlib.sha256(os.urandom(32)).hexdigest()[:16]
        meta = {
            "state": "OK",
            "masked_identity": mask_email(email),
            "identity_hash": hash_identity(email, salt),
            "salt": salt,
            "last_verified_at": time.time(),
        }
        self.metadata_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.chmod(self.metadata_file, 0o600)
        return meta

    def mark_pending(self):
        meta = self.load_metadata()
        meta["state"] = "PENDING"
        self.metadata_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.chmod(self.metadata_file, 0o600)

    def mark_auth_expired(self):
        meta = self.load_metadata()
        meta["state"] = "AUTH_EXPIRED"
        self.metadata_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.chmod(self.metadata_file, 0o600)

    def wipe_profile(self):
        """Wipes the profile directory completely and recreates empty structure."""
        if self.root.exists():
            shutil.rmtree(self.root)
        self.ensure_structure()
