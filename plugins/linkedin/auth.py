#!/usr/bin/env python3
"""LinkedIn OAuth token getter for the company-page (organization) plugin.

Run ONCE to authorize and write ``token.json``. Two modes:

  python auth.py                # local-server mode (opens a browser, captures
                                # the redirect on http://localhost:8080/callback)
  python auth.py --manual       # headless: prints the auth URL, you log in,
                                # then paste the FULL redirected URL back

Requires the LinkedIn app to have the Community Management API product (so the
``w_organization_social`` scope is granted) and your account to be a Super/Content
Admin of the page. Set CLIENT_ID / CLIENT_SECRET below or via env vars.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

import requests

CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("LINKEDIN_REDIRECT_URI", "http://localhost:8080/callback")
# w_organization_social → post as org; rw_organization_admin → look up org ids;
# openid/profile → userinfo token probe.
SCOPE = "openid profile w_organization_social rw_organization_admin"
TOKEN_FILE = Path(os.environ.get("LINKEDIN_TOKEN_FILE",
                                 str(Path(__file__).parent / "token.json")))


def auth_url() -> str:
    return ("https://www.linkedin.com/oauth/v2/authorization"
            "?response_type=code"
            f"&client_id={CLIENT_ID}"
            f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
            f"&scope={urllib.parse.quote(SCOPE)}"
            "&state=qanot")


def exchange(code: str) -> dict:
    r = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
    )
    return r.json()


def save(data: dict) -> None:
    if "access_token" not in data:
        print(f"❌ Token error: {data}")
        sys.exit(1)
    out = {
        "access_token": data["access_token"],
        "expires_in": data.get("expires_in"),
        "refresh_token": data.get("refresh_token"),
        "scope": data.get("scope"),
    }
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(out, indent=2))
    days = round((data.get("expires_in") or 0) / 86400, 1)
    print(f"✅ Token saved → {TOKEN_FILE}  (expires in ~{days} days)")
    print("   Now set org_id in the plugin config (run linkedin_list_orgs to find it).")


def manual() -> None:
    print(f"\n🔗 Open this URL, log in, click Allow:\n\n{auth_url()}\n")
    print("After 'Allow', the browser goes to a localhost page that won't load —")
    print("copy the FULL URL from the address bar (it contains ?code=...).\n")
    pasted = input("Paste the redirected URL here: ").strip()
    qs = urllib.parse.urlparse(pasted).query
    code = urllib.parse.parse_qs(qs).get("code", [None])[0]
    if not code:
        print("❌ No ?code= found in that URL.")
        sys.exit(1)
    save(exchange(code))


def local_server() -> None:
    import http.server
    import threading
    import webbrowser

    holder: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            holder["code"] = urllib.parse.parse_qs(q).get("code", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>Done. You can close this tab.</h1>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", 8080), H)
    t = threading.Thread(target=server.handle_request)
    t.start()
    print(f"\n🔗 Opening browser…\n{auth_url()}\n")
    webbrowser.open(auth_url())
    t.join(timeout=180)
    if not holder.get("code"):
        print("❌ Timeout / no code. Try: python auth.py --manual")
        sys.exit(1)
    save(exchange(holder["code"]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--manual", action="store_true", help="paste-the-URL mode (headless)")
    args = ap.parse_args()
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET (env vars) first.")
        sys.exit(1)
    (manual if args.manual else local_server)()
