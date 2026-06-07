"""LinkedIn plugin — publish to a COMPANY PAGE (organization) via the
Community Management API.

Posting to an organization page needs the ``w_organization_social`` scope
(Community Management API product) and the authorizing user to be a Super/Content
Admin of the page. The author URN is ``urn:li:organization:{id}`` (NOT a person).

Auth is a one-time browser OAuth (see ``auth.py``) that writes a token JSON; the
plugin reads it at runtime. Access tokens live ~60 days, so the token file is the
single source of truth and can be refreshed without touching config.

Tools:
  * ``linkedin_post``        — publish a text (+ optional image) post to the page
  * ``linkedin_status``      — verify the token + which org is configured
  * ``linkedin_list_orgs``   — list organizations the authorizing user administers
                               (use it to discover the numeric org id)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from qanot.plugins.base import Plugin, ToolDef

logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent
TOOLS_MD = (_DIR / "TOOLS.md").read_text(encoding="utf-8") if (_DIR / "TOOLS.md").exists() else ""

API_BASE = "https://api.linkedin.com"
_RESTLI = {"X-Restli-Protocol-Version": "2.0.0"}


class LinkedInClient:
    """Thin async client for the LinkedIn UGC/Community Management API."""

    def __init__(self, token: str, org_id: str):
        self._token = token
        self.org_id = str(org_id).strip()
        self._session: aiohttp.ClientSession | None = None

    @property
    def org_urn(self) -> str:
        # Accept either a bare numeric id or a full urn in config.
        if self.org_id.startswith("urn:li:organization:"):
            return self.org_id
        return f"urn:li:organization:{self.org_id}"

    def _headers(self, json_body: bool = True) -> dict:
        h = {"Authorization": f"Bearer {self._token}", **_RESTLI}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def userinfo(self) -> dict:
        """OIDC userinfo — cheap token-validity probe (needs openid/profile)."""
        s = await self._get_session()
        async with s.get(f"{API_BASE}/v2/userinfo", headers=self._headers(False)) as r:
            return {"status": r.status, "body": await self._safe_json(r)}

    async def admin_orgs(self) -> dict:
        """Organizations where the user is an APPROVED ADMINISTRATOR.

        Needs the ``rw_organization_admin`` scope. Returns org ids + names.
        """
        s = await self._get_session()
        url = (f"{API_BASE}/v2/organizationAcls?q=roleAssignee"
               f"&role=ADMINISTRATOR&state=APPROVED")
        async with s.get(url, headers=self._headers(False)) as r:
            data = await self._safe_json(r)
            if r.status != 200:
                return {"status": r.status, "error": data}
        orgs = []
        for el in (data.get("elements") or []):
            org_urn = el.get("organization", "")
            oid = org_urn.rsplit(":", 1)[-1] if org_urn else ""
            name = await self._org_name(oid) if oid else ""
            orgs.append({"org_id": oid, "org_urn": org_urn, "name": name})
        return {"status": 200, "orgs": orgs}

    async def _org_name(self, org_id: str) -> str:
        try:
            s = await self._get_session()
            async with s.get(f"{API_BASE}/v2/organizations/{org_id}",
                             headers=self._headers(False)) as r:
                if r.status == 200:
                    d = await self._safe_json(r)
                    return (d.get("localizedName")
                            or d.get("name", {}).get("localized", {}).get("en_US", "")
                            or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _register_upload(self, owner_urn: str) -> tuple[str, str]:
        """Register an image upload; return (upload_url, asset_urn)."""
        s = await self._get_session()
        body = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
                "owner": owner_urn,
                "serviceRelationships": [{
                    "relationshipType": "OWNER",
                    "identifier": "urn:li:userGeneratedContent",
                }],
            }
        }
        async with s.post(f"{API_BASE}/v2/assets?action=registerUpload",
                          headers=self._headers(), json=body) as r:
            data = await self._safe_json(r)
            if r.status != 200:
                raise RuntimeError(f"registerUpload failed ({r.status}): {data}")
        value = data["value"]
        upload_url = (value["uploadMechanism"]
                      ["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]
                      ["uploadUrl"])
        return upload_url, value["asset"]

    async def _upload_image(self, image_bytes: bytes, owner_urn: str) -> str:
        upload_url, asset = await self._register_upload(owner_urn)
        s = await self._get_session()
        async with s.put(upload_url,
                         headers={"Authorization": f"Bearer {self._token}"},
                         data=image_bytes) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f"image upload failed ({r.status})")
        return asset

    async def post(self, text: str, image_bytes: bytes | None = None,
                   visibility: str = "PUBLIC") -> dict:
        """Publish a UGC post authored by the organization page."""
        author = self.org_urn
        asset = await self._upload_image(image_bytes, author) if image_bytes else None
        if asset:
            share_content = {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "IMAGE",
                "media": [{"status": "READY", "description": {"text": ""},
                           "media": asset, "title": {"text": ""}}],
            }
        else:
            share_content = {"shareCommentary": {"text": text},
                             "shareMediaCategory": "NONE"}
        body = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": visibility},
        }
        s = await self._get_session()
        async with s.post(f"{API_BASE}/v2/ugcPosts",
                          headers=self._headers(), json=body) as r:
            data = await self._safe_json(r)
            post_id = r.headers.get("x-restli-id") or (data or {}).get("id", "")
            return {"status": r.status, "post_id": post_id, "body": data,
                    "with_image": bool(asset)}

    @staticmethod
    async def _safe_json(resp) -> Any:
        try:
            return await resp.json(content_type=None)
        except Exception:  # noqa: BLE001
            try:
                return {"raw": (await resp.text())[:400]}
            except Exception:  # noqa: BLE001
                return {}


class LinkedInPlugin(Plugin):
    """Publish to a LinkedIn company page from the agent."""

    name = "linkedin"
    description = "LinkedIn company-page posting (text + image) via Community Management API"
    version = "0.1.0"
    tools_md = TOOLS_MD

    def __init__(self) -> None:
        self._client: LinkedInClient | None = None
        self._workspace_dir = ""
        self._org_id = ""
        self._token_file: Path | None = None

    async def setup(self, config: dict) -> None:
        self._workspace_dir = config.get("workspace_dir", "") or "."
        self._org_id = str(config.get("org_id", "") or config.get("org_urn", "")).strip()
        # Token: explicit config value wins; otherwise read a token JSON in the
        # workspace (written by auth.py) so it can be refreshed without redeploy.
        token = str(config.get("access_token", "") or "").strip()
        self._token_file = Path(self._workspace_dir) / "linkedin" / "token.json"
        if not token and self._token_file.exists():
            try:
                token = json.loads(self._token_file.read_text(encoding="utf-8")).get("access_token", "")
            except Exception as e:  # noqa: BLE001
                logger.warning("linkedin: could not read token file: %s", e)
        if not token:
            logger.warning("linkedin plugin loaded WITHOUT a token — tools will report not-configured")
            return
        self._client = LinkedInClient(token, self._org_id)
        logger.info("linkedin plugin ready (org=%s, token=%s)",
                    self._org_id or "(unset)", "yes" if token else "no")

    async def teardown(self) -> None:
        if self._client:
            await self._client.close()

    # ── helpers ────────────────────────────────────────────────
    @staticmethod
    def _ok(data: dict) -> str:
        return json.dumps({"status": "ok", **data}, ensure_ascii=False)

    @staticmethod
    def _err(msg: str) -> str:
        return json.dumps({"status": "error", "error": msg}, ensure_ascii=False)

    def _resolve_image(self, image_path: str) -> Path | None:
        if not image_path:
            return None
        p = Path(image_path)
        if not p.is_absolute():
            p = Path(self._workspace_dir) / image_path
        return p if p.exists() and p.is_file() else None

    # ── tools ──────────────────────────────────────────────────
    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="linkedin_post",
                description=(
                    "Publish a post to the connected LinkedIn COMPANY PAGE. "
                    "Pass `text` (the post body) and optionally `image_path` "
                    "(a local image file, e.g. from generate_image). Posts as the "
                    "organization, publicly. Use when the user asks to post/share "
                    "something on LinkedIn."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Post body text."},
                        "image_path": {"type": "string", "description":
                                       "Optional local image path to attach."},
                    },
                    "required": ["text"],
                },
                handler=self._post,
            ),
            ToolDef(
                name="linkedin_status",
                description="Check LinkedIn connection: token validity and which company page (org) is configured.",
                parameters={"type": "object", "properties": {}},
                handler=self._status,
            ),
            ToolDef(
                name="linkedin_list_orgs",
                description=("List LinkedIn organizations (company pages) the authorizing user "
                             "administers, with their numeric org ids. Use to find the org id "
                             "to configure. Needs the rw_organization_admin scope."),
                parameters={"type": "object", "properties": {}},
                handler=self._list_orgs,
            ),
        ]

    async def _post(self, params: dict) -> str:
        if self._client is None:
            return self._err("LinkedIn not configured (no access token). Run auth.py to get a token.")
        if not self._org_id:
            return self._err("No org_id configured. Call linkedin_list_orgs to find it, then set it in config.")
        text = (params.get("text") or "").strip()
        if not text:
            return self._err("text is required.")
        image_bytes = None
        ip = params.get("image_path")
        if ip:
            p = self._resolve_image(ip)
            if p is None:
                return self._err(f"image_path not found: {ip}")
            image_bytes = p.read_bytes()
        try:
            res = await self._client.post(text, image_bytes)
        except Exception as e:  # noqa: BLE001
            logger.error("linkedin_post failed: %s", e)
            return self._err(f"post failed: {e}")
        if res["status"] in (200, 201):
            return self._ok({"post_id": res["post_id"], "with_image": res["with_image"],
                             "page": self._org_id})
        return self._err(f"LinkedIn API {res['status']}: {res['body']}")

    async def _status(self, params: dict) -> str:
        if self._client is None:
            return self._err("Not configured: no access token. Run plugins/linkedin/auth.py.")
        try:
            info = await self._client.userinfo()
        except Exception as e:  # noqa: BLE001
            return self._err(f"token check failed: {e}")
        valid = info["status"] == 200
        return self._ok({
            "token_valid": valid,
            "org_id": self._org_id or "(unset)",
            "name": (info["body"] or {}).get("name", "") if valid else "",
            "hint": "" if self._org_id else "Set org_id via linkedin_list_orgs.",
        })

    async def _list_orgs(self, params: dict) -> str:
        if self._client is None:
            return self._err("Not configured: no access token.")
        try:
            res = await self._client.admin_orgs()
        except Exception as e:  # noqa: BLE001
            return self._err(f"lookup failed: {e}")
        if res["status"] != 200:
            return self._err(f"LinkedIn API {res['status']}: {res.get('error')} "
                             "(needs rw_organization_admin scope)")
        return self._ok({"orgs": res["orgs"], "count": len(res["orgs"])})
