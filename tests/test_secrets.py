"""Tests for secrets — SecretRef resolution from env vars and files."""

from __future__ import annotations

import os
import stat
import pytest

from qanot.secrets import resolve_secret, resolve_config_secrets, _read_secret_file


# ── resolve_secret ───────────────────────────────────────────


class TestResolveSecret:
    """Test secret resolution from various sources."""

    # -- Plain string passthrough --

    def test_plain_string_passthrough(self) -> None:
        assert resolve_secret("sk-ant-12345") == "sk-ant-12345"

    def test_empty_string_passthrough(self) -> None:
        assert resolve_secret("") == ""

    # -- Env var resolution --

    def test_resolve_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_SECRET_KEY", "my-secret-value")
        result = resolve_secret({"env": "TEST_SECRET_KEY"})
        assert result == "my-secret-value"

    def test_missing_env_var_returns_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("NONEXISTENT_SECRET_VAR", raising=False)
        result = resolve_secret({"env": "NONEXISTENT_SECRET_VAR"})
        assert result == ""

    def test_env_var_empty_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_secret({"env": ""})

    def test_env_var_whitespace_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_secret({"env": "   "})

    def test_env_var_non_string_name_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_secret({"env": 42})

    # -- File resolution --

    def test_resolve_file_secret(self, tmp_path) -> None:
        secret_file = tmp_path / "api_key.txt"
        secret_file.write_text("file-secret-value\n")
        result = resolve_secret({"file": str(secret_file)})
        assert result == "file-secret-value"

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_secret({"file": "/tmp/nonexistent_secret_file_12345.txt"})

    def test_file_empty_path_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_secret({"file": ""})

    def test_file_symlink_blocked(self, tmp_path) -> None:
        real = tmp_path / "real_secret.txt"
        real.write_text("secret")
        link = tmp_path / "link_secret.txt"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="symlink"):
            resolve_secret({"file": str(link)})

    # -- Unknown dict format --

    def test_unknown_dict_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown secret format"):
            resolve_secret({"url": "https://vault.example.com"})

    # -- Non-string, non-dict fallback --

    def test_integer_fallback(self) -> None:
        assert resolve_secret(42) == "42"

    def test_none_returns_empty(self) -> None:
        assert resolve_secret(None) == ""

    def test_bool_fallback(self) -> None:
        assert resolve_secret(True) == "True"


# ── _read_secret_file ────────────────────────────────────────


class TestReadSecretFile:
    def test_reads_and_strips(self, tmp_path) -> None:
        f = tmp_path / "key.txt"
        f.write_text("  my-key  \n")
        assert _read_secret_file(str(f)) == "my-key"

    def test_directory_raises(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="not a regular file"):
            _read_secret_file(str(tmp_path))

    def test_too_large_raises(self, tmp_path) -> None:
        f = tmp_path / "big.txt"
        f.write_bytes(b"x" * (65 * 1024))  # > 64 KB
        with pytest.raises(ValueError, match="too large"):
            _read_secret_file(str(f))

    def test_empty_file_returns_empty(self, tmp_path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("")
        assert _read_secret_file(str(f)) == ""


# ── resolve_config_secrets ───────────────────────────────────


class TestResolveConfigSecrets:
    def test_resolves_known_fields(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_API_KEY", "resolved-key")
        monkeypatch.setenv("TEST_BOT_TOKEN", "resolved-token")
        raw = {
            "api_key": {"env": "TEST_API_KEY"},
            "bot_token": {"env": "TEST_BOT_TOKEN"},
            "model": "claude-sonnet",  # plain string, untouched
        }
        result = resolve_config_secrets(raw)
        assert result["api_key"] == "resolved-key"
        assert result["bot_token"] == "resolved-token"
        assert result["model"] == "claude-sonnet"

    def test_mixed_plain_and_secretref(self, monkeypatch) -> None:
        monkeypatch.setenv("TEST_BRAVE_KEY", "brave-val")
        raw = {
            "api_key": "plain-key",
            "brave_api_key": {"env": "TEST_BRAVE_KEY"},
        }
        result = resolve_config_secrets(raw)
        assert result["api_key"] == "plain-key"
        assert result["brave_api_key"] == "brave-val"

    def test_resolves_provider_api_keys(self, monkeypatch) -> None:
        monkeypatch.setenv("PROV_KEY", "provider-secret")
        raw = {
            "providers": [
                {"name": "openai", "api_key": {"env": "PROV_KEY"}},
                {"name": "groq", "api_key": "plain-groq-key"},
            ],
        }
        result = resolve_config_secrets(raw)
        assert result["providers"][0]["api_key"] == "provider-secret"
        assert result["providers"][1]["api_key"] == "plain-groq-key"

    def test_resolves_voice_api_keys(self, monkeypatch) -> None:
        monkeypatch.setenv("VOICE_KEY", "voice-secret")
        raw = {
            "voice_api_keys": {
                "muxlisa": {"env": "VOICE_KEY"},
                "whisper": "plain-whisper",
            },
        }
        result = resolve_config_secrets(raw)
        assert result["voice_api_keys"]["muxlisa"] == "voice-secret"
        assert result["voice_api_keys"]["whisper"] == "plain-whisper"

    def test_failed_resolution_logs_warning(self, monkeypatch, caplog) -> None:
        """Failed resolution should warn, not crash."""
        raw = {
            "api_key": {"file": "/nonexistent/path/12345.txt"},
        }
        import logging
        with caplog.at_level(logging.WARNING):
            result = resolve_config_secrets(raw)
        # api_key should remain as-is (dict) since resolution failed
        assert "Failed to resolve" in caplog.text

    def test_empty_config(self) -> None:
        result = resolve_config_secrets({})
        assert result == {}

    def test_no_secret_fields(self) -> None:
        raw = {"model": "opus", "temperature": 0.7}
        result = resolve_config_secrets(raw)
        assert result == {"model": "opus", "temperature": 0.7}
