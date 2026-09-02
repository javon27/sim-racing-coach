"""Anthropic API key storage in the OS keyring (Windows Credential Manager,
per-user DPAPI). Entry stays in the app's own UI; keyring is silent storage.
The ANTHROPIC_API_KEY env var overrides the stored key for developers."""
from __future__ import annotations

import os

import keyring
import keyring.errors

SERVICE = "SimCoach"
KEY_USER = "anthropic_api_key"


def get_api_key() -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env and env.strip():
        return env.strip()
    try:
        stored = keyring.get_password(SERVICE, KEY_USER)
    except Exception:
        # No keyring backend (e.g. headless CI) -> behave as "no key set"
        return None
    return stored.strip() if stored and stored.strip() else None


def set_api_key(key: str) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("API key is empty")
    keyring.set_password(SERVICE, KEY_USER, key)


def has_api_key() -> bool:
    return get_api_key() is not None


def clear_api_key() -> None:
    try:
        keyring.delete_password(SERVICE, KEY_USER)
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception:
        pass
