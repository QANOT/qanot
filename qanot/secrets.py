"""Secret resolution — load sensitive values from env/file, not plain config.

OpenClaw-inspired: SecretRef pattern. Config references secrets by source,
runtime resolves them. No secrets in config.json.

Usage in config.json:
    "api_key": {"env": "ANTHROPIC_API_KEY"}
    "api_key": {"file": "/run/secrets/api_key"}
    "api_key": "sk-ant-..."  (legacy: plain text still works)
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_secret(value) -> str:
    """Resolve a secret value from config.

    Accepts:
        str → returned as-is (plain text, legacy mode)
        dict → resolved from source:
            {"env": "VAR_NAME"} → os.environ["VAR_NAME"]
            {"file": "/path/to/secret"} → read file content
    """
    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        # Environment variable
        if "env" in value:
            var_name = value["env"]
            if not isinstance(var_name, str) or not var_name.strip():
                raise ValueError("Secret env var name must be a non-empty string")
            result = os.environ.get(var_name, "")
            if not result:
                logger.warning("Secret env var %s is empty or not set", var_name)
            return result

        # File-based secret
        if "file" in value:
            file_path = value["file"]
            if not isinstance(file_path, str) or not file_path.strip():
                raise ValueError("Secret file path must be a non-empty string")
            return _read_secret_file(file_path)

        raise ValueError(
            f"Unknown secret format: expected 'env' or 'file' key, got {list(value.keys())!r}"
        )

    # Fallback: convert to string
    return str(value) if value else ""


def _read_secret_file(path: str) -> str:
    """Read a secret from a file with security checks.

    Validates:
    - File exists and is a regular file
    - Not a symlink (prevent escape)
    - Readable by current user
    - Not world-readable (warn only)
    """
    p = Path(path)

    if p.is_symlink():
        raise ValueError(f"Secret file is a symlink (security risk): {path}")

    if not p.exists():
        raise FileNotFoundError(f"Secret file not found: {path}")

    if not p.is_file():
        raise ValueError(f"Secret path is not a regular file: {path}")

    # Check permissions on Unix
    try:
        if p.stat().st_mode & stat.S_IROTH:
            logger.warning(
                "Secret file %s is world-readable (chmod 600 recommended)", path
            )
    except OSError:
        pass

    content = p.read_text(encoding="utf-8").strip()
    if not content:
        logger.warning("Secret file %s is empty", path)

    return content


def resolve_config_secrets(raw: dict) -> dict:
    """Resolve all SecretRef values in a config dict.

    Processes known secret fields: api_key, bot_token, brave_api_key,
    voice_api_key, image_api_key, and provider api_keys.
    """
    secret_fields = {
        "api_key", "bot_token", "brave_api_key",
        "voice_api_key", "image_api_key",
    }

    for field in secret_fields:
        if field in raw:
            try:
                raw[field] = resolve_secret(raw[field])
            except Exception as e:
                logger.warning("Failed to resolve secret %s: %s", field, e)

    # Resolve provider api_keys
    for provider in raw.get("providers", []):
        if isinstance(provider, dict) and "api_key" in provider:
            try:
                provider["api_key"] = resolve_secret(provider["api_key"])
            except Exception as e:
                logger.warning(
                    "Failed to resolve secret for provider %s: %s",
                    provider.get("name", "?"), e,
                )

    # Resolve voice_api_keys dict
    voice_keys = raw.get("voice_api_keys", {})
    if isinstance(voice_keys, dict):
        for name, val in voice_keys.items():
            try:
                voice_keys[name] = resolve_secret(val)
            except Exception as e:
                logger.warning("Failed to resolve voice secret %s: %s", name, e)

    return raw
