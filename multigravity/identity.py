"""Main Account (Account 1) identity management and salted hash persistence."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from multigravity.isolation import hash_identity, mask_email

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MainIdentityError(ValueError):
    """Raised when email validation or main identity operations fail."""
    pass


def get_default_state_dir() -> Path:
    """Returns state directory path outside repository, respecting env override."""
    env_dir = os.environ.get("MULTIGRAVITY_STATE_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    return (Path.home() / ".multigravity" / "state").resolve()


def get_main_identity_path(state_dir: Path | None = None) -> Path:
    d = state_dir or get_default_state_dir()
    return d / "main_identity.json"


def load_main_identity(state_dir: Path | None = None) -> dict[str, Any] | None:
    """Loads Account 1 salted hash metadata if present."""
    path = get_main_identity_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "salt" in data and "identity_hash" in data:
            return data
    except Exception:
        pass
    return None


def save_main_identity(email: str, state_dir: Path | None = None) -> dict[str, Any]:
    """Saves Account 1 email as a salted SHA-256 hash in state dir (mode 0600).
    
    The plain email is NEVER stored in the file, logged, or returned.
    """
    clean_email = email.strip()
    if not EMAIL_REGEX.match(clean_email):
        raise MainIdentityError("Invalid email address format")

    salt = secrets.token_hex(16)
    identity_hash = hash_identity(clean_email, salt)
    masked = mask_email(clean_email)
    del clean_email

    metadata = {
        "salt": salt,
        "identity_hash": identity_hash,
        "masked_identity": masked,
        "updated_at": time.time(),
    }

    target_dir = state_dir or get_default_state_dir()
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target_path = target_dir / "main_identity.json"

    # Write securely with mode 0600 using atomic replace
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(target_dir),
        delete=False,
        encoding="utf-8",
    ) as tf:
        os.chmod(tf.name, 0o600)
        json.dump(metadata, tf, indent=2)
        temp_name = tf.name

    os.replace(temp_name, target_path)
    os.chmod(target_path, 0o600)
    return metadata


def is_main_account_match(email: str, main_identity: dict[str, Any] | None) -> bool:
    """Checks if email matches Account 1 without revealing plain identity."""
    if not main_identity:
        return False
    salt = main_identity.get("salt")
    expected_hash = main_identity.get("identity_hash")
    if not salt or not expected_hash:
        return False
    test_hash = hash_identity(email.strip().lower(), salt)
    return test_hash == expected_hash
