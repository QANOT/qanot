# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 2.0.x   | Yes                |
| 1.x     | No                 |

## Reporting a Vulnerability

If you discover a security vulnerability in Qanot AI, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, email **hello@sirli.ai** with:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge your report within 48 hours and aim to release a fix within 7 days for critical issues.

## Security Architecture

Qanot AI includes multiple security layers:

- **File system jailing**: `read_file`, `write_file`, `list_files`, `send_file` block system directories
- **SSRF protection**: `web_fetch` blocks private IPs, internal hostnames, and validates post-redirect URLs
- **Command execution**: Default `exec_security` is `"cautious"` with command blocklists
- **Rate limiting**: Per-user sliding window to prevent abuse
- **Safe file writes**: Symlink checks, null byte rejection, system directory blocking
- **Secret management**: `SecretRef` for env vars and file-based secrets (no hardcoded keys)
- **Input validation**: Config field validation, control character rejection

## Security Configuration

The `exec_security` config field controls command execution:

- `"strict"` — Only allowlisted commands (recommended for public bots)
- `"cautious"` — Dangerous commands blocked (default)
- `"open"` — All commands allowed (use only in trusted environments)

For public-facing bots, we recommend:

```json
{
  "exec_security": "strict",
  "exec_allowlist": ["ls", "cat", "python3"],
  "allowed_users": [123456789]
}
```
