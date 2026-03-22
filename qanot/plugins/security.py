"""Plugin security — scanning, verification, and sandboxing.

Prevents OpenClaw-style supply chain attacks:
  - ClawHavoc: malicious install.sh scripts deploying stealers
  - Arbitrary code execution via unverified plugins
  - Dependency confusion via unrestricted pip install

Security layers:
  1. Name sanitization (directory traversal prevention)
  2. Dangerous file blocking (install.sh, .exe, .so, etc.)
  3. Static code scanning (subprocess, eval, exec, os.system, etc.)
  4. SHA256 hash verification in lock file
  5. Pip dependency allowlist
  6. Plugin permission declarations
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Dangerous patterns in Python code ─────────────────────

# Patterns that indicate potentially malicious code
DANGEROUS_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, severity, description)
    (r'\bos\.system\s*\(', "CRITICAL", "os.system() — arbitrary shell execution"),
    (r'\bsubprocess\.(run|call|Popen|check_output|check_call)\s*\(', "HIGH", "subprocess execution"),
    (r'\beval\s*\(', "HIGH", "eval() — arbitrary code execution"),
    (r'\bexec\s*\(', "HIGH", "exec() — arbitrary code execution"),
    (r'\b__import__\s*\(', "HIGH", "__import__() — dynamic import bypass"),
    (r'\bcompile\s*\(.*["\']exec["\']', "HIGH", "compile() with exec mode"),
    (r'\bctypes\b', "HIGH", "ctypes — native code execution"),
    (r'\bkeylog', "CRITICAL", "keylogger reference"),
    (r'\bstealer\b', "CRITICAL", "stealer malware reference"),
    (r'\breverse.?shell\b', "CRITICAL", "reverse shell"),
    (r'\bsocket\.connect\s*\(', "MEDIUM", "outbound socket connection"),
    (r'\bopen\s*\([^)]*["\']/etc/', "HIGH", "reading system files"),
    (r'\bopen\s*\([^)]*["\']~/', "MEDIUM", "reading home directory files"),
    (r'\bos\.(environ|getenv)\s*\[', "MEDIUM", "environment variable access"),
    (r'\bshutil\.(rmtree|move)\s*\(', "MEDIUM", "destructive file operations"),
    (r'\bos\.(remove|unlink|rmdir)\s*\(', "MEDIUM", "file deletion"),
    (r'\burllib\.request\.urlopen\s*\(', "MEDIUM", "HTTP request (data exfiltration risk)"),
    (r'\brequests\.(get|post|put|delete)\s*\(', "MEDIUM", "HTTP request (data exfiltration risk)"),
    (r'\baiohttp\.ClientSession\s*\(', "LOW", "async HTTP client"),
    (r'base64\.(b64decode|decodebytes)\s*\(', "MEDIUM", "base64 decode (obfuscation indicator)"),
    (r'\bPickle\b|\bpickle\.loads?\s*\(', "HIGH", "pickle deserialization (RCE vector)"),
    (r'\bmarshall?\.loads?\s*\(', "HIGH", "marshal deserialization"),
]

# Files that should NEVER exist in a plugin
BLOCKED_FILES = {
    "install.sh", "install.bat", "install.cmd", "setup.sh",
    "postinstall.sh", "preinstall.sh",
    ".env", ".env.local", ".env.production",
}

# File extensions that should NEVER exist in a plugin
BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".msi",
    ".sh", ".bat", ".cmd", ".ps1",  # shell scripts
    ".pyc", ".pyo",  # compiled Python (could hide malicious code)
}

# Allowed pip package prefixes (safe to auto-install)
PIP_ALLOWLIST = {
    "aiohttp", "aiomysql", "aiopg", "aiosqlite",
    "beautifulsoup4", "bs4",
    "cryptography",
    "httpx",
    "jinja2",
    "lxml",
    "openpyxl",
    "pillow", "PIL",
    "pydantic",
    "pymysql",
    "python-docx", "python-pptx",
    "redis",
    "requests",
    "sqlalchemy",
    "ujson", "orjson",
    "xmltodict",
}

# ── Name sanitization ─────────────────────────────────────

# Only allow safe characters in plugin names
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def sanitize_plugin_name(raw: str) -> tuple[str, bool]:
    """Sanitize a plugin name. Returns (name, is_safe).

    Prevents directory traversal and injection attacks.
    """
    # Block traversal attempts BEFORE any stripping
    if ".." in raw or "\x00" in raw:
        return raw, False

    # Check for slashes in the non-URL part
    # If raw is a URL, rsplit is fine; if it's a plain name with '/', block it
    if "/" in raw and not raw.startswith(("https://", "http://", "git@")):
        return raw, False
    if "\\" in raw:
        return raw, False

    # Strip URL path to get last segment
    name = raw.rstrip("/").rsplit("/", 1)[-1]
    name = name.removesuffix(".git")
    for prefix in ("qanot-plugin-", "qanot-", "plugin-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    name = name.replace("-", "_").lower().strip()

    # Final validation after stripping
    if ".." in name or "/" in name or "\\" in name or "\x00" in name:
        return name, False

    if not _SAFE_NAME_RE.match(name):
        return name, False

    return name, True


# ── File scanning ─────────────────────────────────────────


def scan_plugin_files(plugin_dir: Path) -> list[dict]:
    """Scan a plugin directory for dangerous files.

    Returns list of findings: [{"file": str, "severity": str, "issue": str}]
    """
    findings: list[dict] = []

    for fpath in plugin_dir.rglob("*"):
        if not fpath.is_file():
            continue

        rel = str(fpath.relative_to(plugin_dir))

        # Check blocked files
        if fpath.name.lower() in BLOCKED_FILES:
            findings.append({
                "file": rel,
                "severity": "CRITICAL",
                "issue": f"Blocked file: {fpath.name} (potential install script attack)",
            })

        # Check blocked extensions
        if fpath.suffix.lower() in BLOCKED_EXTENSIONS:
            findings.append({
                "file": rel,
                "severity": "HIGH",
                "issue": f"Blocked extension: {fpath.suffix} (executable/script)",
            })

    return findings


def scan_plugin_code(plugin_dir: Path) -> list[dict]:
    """Scan Python files for dangerous code patterns.

    Returns list of findings with file, line, severity, and description.
    """
    findings: list[dict] = []

    for py_file in plugin_dir.rglob("*.py"):
        rel = str(py_file.relative_to(plugin_dir))

        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # Skip benign comments (but still scan CRITICAL patterns
            # like keylogger/stealer/reverse_shell even in comments)
            is_comment = stripped.startswith("#")

            for pattern, severity, description in DANGEROUS_PATTERNS:
                # Skip non-critical patterns in comments
                if is_comment and severity not in ("CRITICAL",):
                    continue
                if re.search(pattern, line):
                    findings.append({
                        "file": rel,
                        "line": line_num,
                        "severity": severity,
                        "issue": description,
                        "code": stripped[:120],
                    })

    return findings


def scan_plugin(plugin_dir: Path) -> list[dict]:
    """Full security scan of a plugin directory.

    Returns all findings sorted by severity.
    """
    findings = scan_plugin_files(plugin_dir) + scan_plugin_code(plugin_dir)

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    findings.sort(key=lambda f: severity_order.get(f["severity"], 99))

    return findings


def has_critical_findings(findings: list[dict]) -> bool:
    """Check if any findings are CRITICAL severity."""
    return any(f["severity"] == "CRITICAL" for f in findings)


def has_high_findings(findings: list[dict]) -> bool:
    """Check if any findings are HIGH or CRITICAL severity."""
    return any(f["severity"] in ("CRITICAL", "HIGH") for f in findings)


# ── Hash verification ─────────────────────────────────────


def compute_plugin_hash(plugin_dir: Path) -> str:
    """Compute SHA256 hash of all plugin files (deterministic).

    Hashes all files in sorted order to produce a stable checksum.
    """
    hasher = hashlib.sha256()

    files = sorted(
        f for f in plugin_dir.rglob("*")
        if f.is_file() and ".git" not in f.parts
    )

    for fpath in files:
        rel = str(fpath.relative_to(plugin_dir))
        hasher.update(rel.encode("utf-8"))
        hasher.update(fpath.read_bytes())

    return hasher.hexdigest()


def verify_plugin_hash(plugin_dir: Path, expected_hash: str) -> bool:
    """Verify a plugin directory matches its expected hash."""
    actual = compute_plugin_hash(plugin_dir)
    return actual == expected_hash


# ── Dependency validation ─────────────────────────────────


def validate_dependencies(deps: list[str]) -> tuple[list[str], list[str]]:
    """Validate pip dependencies against allowlist.

    Returns (allowed, blocked) lists.
    """
    allowed: list[str] = []
    blocked: list[str] = []

    for dep in deps:
        if not isinstance(dep, str):
            blocked.append(str(dep))
            continue

        # Extract base package name (before version specifier)
        pkg = re.split(r"[>=<!\[]", dep)[0].strip().lower()

        if pkg in PIP_ALLOWLIST:
            allowed.append(dep)
        else:
            blocked.append(dep)

    return allowed, blocked


# ── Permission declarations ───────────────────────────────

# Valid permission values for plugin.json "permissions" field
VALID_PERMISSIONS = {
    "network",      # Can make HTTP requests
    "filesystem",   # Can read/write files beyond workspace
    "subprocess",   # Can spawn subprocesses
    "env_vars",     # Can read environment variables
    "database",     # Can connect to databases
}


def validate_permissions(permissions: list[str]) -> tuple[list[str], list[str]]:
    """Validate permission declarations.

    Returns (valid, invalid) lists.
    """
    valid = [p for p in permissions if p in VALID_PERMISSIONS]
    invalid = [p for p in permissions if p not in VALID_PERMISSIONS]
    return valid, invalid


# ── Combined security check ───────────────────────────────


def security_check(
    plugin_dir: Path,
    auto_block_critical: bool = True,
) -> tuple[bool, list[dict], str]:
    """Run full security check on a plugin.

    Args:
        plugin_dir: Path to plugin directory
        auto_block_critical: If True, automatically block plugins with CRITICAL findings

    Returns:
        (is_safe, findings, summary)
    """
    findings = scan_plugin(plugin_dir)

    if not findings:
        return True, [], "No security issues found"

    critical = sum(1 for f in findings if f["severity"] == "CRITICAL")
    high = sum(1 for f in findings if f["severity"] == "HIGH")
    medium = sum(1 for f in findings if f["severity"] == "MEDIUM")
    low = sum(1 for f in findings if f["severity"] == "LOW")

    summary_parts = []
    if critical:
        summary_parts.append(f"{critical} CRITICAL")
    if high:
        summary_parts.append(f"{high} HIGH")
    if medium:
        summary_parts.append(f"{medium} MEDIUM")
    if low:
        summary_parts.append(f"{low} LOW")

    summary = f"Security scan: {', '.join(summary_parts)}"

    if auto_block_critical and critical > 0:
        return False, findings, summary + " — BLOCKED (critical issues found)"

    if high > 0:
        return False, findings, summary + " — requires manual review"

    return True, findings, summary
