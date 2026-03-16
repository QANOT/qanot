"""Tests for fs_safe — file write safety and path validation."""

from __future__ import annotations

import os
import sys
import pytest

from qanot.fs_safe import (
    SafeWriteError,
    _SYSTEM_DIRS,
    is_path_within_root,
    validate_write_path,
    safe_write_file,
)


# ── validate_write_path ─────────────────────────────────────


class TestValidateWritePath:
    """Test path validation against system dirs, traversal, and symlinks."""

    # -- Block system directories --

    @pytest.mark.parametrize("sys_path", [
        "/usr/bin/python3",
        "/usr/local/evil.txt",
        "/sbin/init",
        "/lib/modules",
        "/System/Library/Frameworks",
        "/Library/Preferences",
    ])
    def test_blocks_system_directories(self, sys_path: str) -> None:
        """Paths under _SYSTEM_DIRS are blocked (using paths that resolve
        to themselves, not via macOS symlinks)."""
        result = validate_write_path(sys_path)
        assert result is not None
        assert "blocked" in result.lower() or "system" in result.lower()

    def test_blocks_all_system_dirs_directly(self) -> None:
        """Every entry in _SYSTEM_DIRS should be blocked when path resolves there."""
        for sys_dir in _SYSTEM_DIRS:
            test_path = os.path.join(sys_dir, "test.txt")
            resolved = os.path.realpath(test_path)
            # Only test if resolved path is still under the same sys_dir
            # (on macOS /etc resolves to /private/etc which is not in _SYSTEM_DIRS)
            if resolved.startswith(sys_dir + os.sep) or resolved == sys_dir:
                result = validate_write_path(test_path)
                assert result is not None, f"Should block {test_path}"

    @pytest.mark.skipif(sys.platform != "linux", reason="macOS resolves /etc to /private/etc")
    @pytest.mark.parametrize("sys_path", ["/etc/passwd", "/etc/shadow", "/var/run/lock"])
    def test_blocks_etc_var_on_linux(self, sys_path: str) -> None:
        result = validate_write_path(sys_path)
        assert result is not None

    def test_blocks_via_root_jail_regardless_of_platform(self, tmp_path) -> None:
        """Even if /etc resolves to /private/etc, root jail still blocks it."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = validate_write_path("/etc/passwd", root=str(workspace))
        assert result is not None

    # -- Block home sensitive dirs --

    @pytest.mark.parametrize("sensitive", [
        os.path.expanduser("~/.ssh/id_rsa"),
        os.path.expanduser("~/.gnupg/pubring.kbx"),
        os.path.expanduser("~/.aws/credentials"),
    ])
    def test_blocks_home_sensitive_with_root_jail(self, sensitive: str, tmp_path) -> None:
        """Sensitive home paths blocked when root jail is set to workspace."""
        result = validate_write_path(sensitive, root=str(tmp_path))
        assert result is not None

    # -- Block path traversal --

    def test_blocks_path_traversal(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        evil_path = str(workspace / ".." / ".." / ".." / "etc" / "passwd")
        result = validate_write_path(evil_path, root=str(workspace))
        assert result is not None

    def test_blocks_traversal_outside_root(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        result = validate_write_path(str(outside), root=str(workspace))
        assert result is not None
        assert "outside" in result.lower()

    # -- Block symlinks --

    def test_blocks_symlink_write(self, tmp_path) -> None:
        target = tmp_path / "real.txt"
        target.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = validate_write_path(str(link))
        assert result is not None
        assert "symlink" in result.lower()

    def test_blocks_symlink_escape(self, tmp_path) -> None:
        """Symlink pointing outside workspace should be blocked."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("escape")
        link = workspace / "escape.txt"
        link.symlink_to(outside)
        result = validate_write_path(str(link), root=str(workspace))
        assert result is not None

    # -- Allow valid paths --

    def test_allows_workspace_path(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "notes.txt"
        result = validate_write_path(str(target), root=str(workspace))
        assert result is None

    def test_allows_nested_workspace_path(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        nested = workspace / "sub" / "deep"
        nested.mkdir(parents=True)
        target = nested / "file.txt"
        result = validate_write_path(str(target), root=str(workspace))
        assert result is None

    def test_allows_path_without_root(self, tmp_path) -> None:
        target = tmp_path / "safe.txt"
        result = validate_write_path(str(target))
        assert result is None

    # -- Empty / None inputs --

    def test_blocks_empty_path(self) -> None:
        result = validate_write_path("")
        assert result is not None
        assert "empty" in result.lower()

    def test_blocks_whitespace_path(self) -> None:
        result = validate_write_path("   ")
        assert result is not None
        assert "empty" in result.lower()

    def test_blocks_null_byte(self) -> None:
        result = validate_write_path("/tmp/evil\x00.txt")
        assert result is not None
        assert "null" in result.lower()

    # -- Unicode paths --

    def test_allows_unicode_path(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "fayl_\u0443\u0437\u0431.txt"
        result = validate_write_path(str(target), root=str(workspace))
        assert result is None


# ── is_path_within_root ──────────────────────────────────────


class TestIsPathWithinRoot:
    def test_inside(self, tmp_path) -> None:
        child = tmp_path / "sub" / "file.txt"
        assert is_path_within_root(str(tmp_path), str(child)) is True

    def test_outside(self, tmp_path) -> None:
        outside = tmp_path / ".." / "nope.txt"
        assert is_path_within_root(str(tmp_path), str(outside)) is False

    def test_root_equals_path(self, tmp_path) -> None:
        assert is_path_within_root(str(tmp_path), str(tmp_path)) is True


# ── safe_write_file ──────────────────────────────────────────


class TestSafeWriteFile:
    def test_writes_file_successfully(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "output.txt"
        result = safe_write_file(str(target), "hello world", root=str(workspace))
        assert os.path.isfile(result)
        assert open(result).read() == "hello world"

    def test_creates_parent_dirs(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "a" / "b" / "c.txt"
        safe_write_file(str(target), "nested", root=str(workspace))
        assert target.read_text() == "nested"

    def test_raises_on_system_dir(self) -> None:
        """Use /usr/evil.txt which resolves to itself on all platforms."""
        with pytest.raises(SafeWriteError) as exc_info:
            safe_write_file("/usr/evil.txt", "pwned")
        assert "system" in str(exc_info.value).lower()

    def test_raises_on_empty_path(self) -> None:
        with pytest.raises(SafeWriteError):
            safe_write_file("", "data")

    def test_raises_on_outside_root(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "escape.txt"
        with pytest.raises(SafeWriteError):
            safe_write_file(str(outside), "data", root=str(workspace))

    def test_atomic_write_overwrites(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target = workspace / "test.txt"
        safe_write_file(str(target), "original", root=str(workspace))
        safe_write_file(str(target), "updated", root=str(workspace))
        assert target.read_text() == "updated"

    def test_raises_on_symlink(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        real = workspace / "real.txt"
        real.write_text("real")
        link = workspace / "link.txt"
        link.symlink_to(real)
        with pytest.raises(SafeWriteError):
            safe_write_file(str(link), "pwned", root=str(workspace))
