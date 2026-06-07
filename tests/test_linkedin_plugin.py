"""Tests for the LinkedIn company-page plugin (no network — mocked aiohttp)."""

from __future__ import annotations

import json

import pytest

from plugins.linkedin.plugin import LinkedInClient, LinkedInPlugin


# ── fake aiohttp session ───────────────────────────────────────
class _Resp:
    def __init__(self, status=201, data=None, headers=None):
        self.status = status
        self._data = data or {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._data

    async def text(self):
        return ""


class _Session:
    def __init__(self):
        self.calls = []
        self.closed = False

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        if "registerUpload" in url:
            return _Resp(200, {"value": {
                "asset": "urn:li:digitalmediaAsset:AID",
                "uploadMechanism": {
                    "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest": {
                        "uploadUrl": "https://up.example/abc"}}}})
        return _Resp(201, {}, {"x-restli-id": "urn:li:share:777"})

    def put(self, url, headers=None, data=None):
        self.calls.append(("PUT", url, None))
        return _Resp(201)

    def get(self, url, headers=None):
        self.calls.append(("GET", url, None))
        return _Resp(200, {"name": "Me"})

    async def close(self):
        self.closed = True


def _attach(client: LinkedInClient, sess: _Session):
    async def _fg():
        return sess
    client._get_session = _fg


async def _setup(tmp_path, **cfg):
    p = LinkedInPlugin()
    await p.setup({"workspace_dir": str(tmp_path), **cfg})
    return p


# ── client payload construction ────────────────────────────────
def test_org_urn_formats():
    assert LinkedInClient("t", "12345").org_urn == "urn:li:organization:12345"
    assert LinkedInClient("t", "urn:li:organization:9").org_urn == "urn:li:organization:9"


@pytest.mark.asyncio
async def test_client_text_post_authored_by_org():
    c = LinkedInClient("TOK", "42")
    sess = _Session()
    _attach(c, sess)
    res = await c.post("Hello page")
    assert res["status"] == 201 and res["post_id"] == "urn:li:share:777"
    body = [j for m, u, j in sess.calls if u.endswith("/ugcPosts")][0]
    assert body["author"] == "urn:li:organization:42"
    sc = body["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert sc["shareMediaCategory"] == "NONE"
    assert sc["shareCommentary"]["text"] == "Hello page"


@pytest.mark.asyncio
async def test_client_image_post_uploads_then_attaches():
    c = LinkedInClient("TOK", "42")
    sess = _Session()
    _attach(c, sess)
    res = await c.post("with pic", image_bytes=b"IMGDATA")
    assert res["status"] == 201 and res["with_image"] is True
    reg = [j for m, u, j in sess.calls if "registerUpload" in u][0]
    assert reg["registerUploadRequest"]["owner"] == "urn:li:organization:42"
    assert any(m == "PUT" for m, u, j in sess.calls)
    body = [j for m, u, j in sess.calls if u.endswith("/ugcPosts")][0]
    sc = body["specificContent"]["com.linkedin.ugc.ShareContent"]
    assert sc["shareMediaCategory"] == "IMAGE"
    assert sc["media"][0]["media"] == "urn:li:digitalmediaAsset:AID"


# ── plugin wiring ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_setup_reads_token_file(tmp_path):
    tf = tmp_path / "linkedin" / "token.json"
    tf.parent.mkdir(parents=True)
    tf.write_text(json.dumps({"access_token": "TOK"}))
    p = await _setup(tmp_path, org_id="123")
    assert p._client is not None and p._org_id == "123"


@pytest.mark.asyncio
async def test_setup_no_token_reports_unconfigured(tmp_path):
    p = await _setup(tmp_path, org_id="123")
    assert p._client is None
    out = json.loads(await p._post({"text": "hi"}))
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_tools_registered(tmp_path):
    p = await _setup(tmp_path, access_token="T", org_id="1")
    assert {t.name for t in p.get_tools()} == {
        "linkedin_post", "linkedin_status", "linkedin_list_orgs"}


@pytest.mark.asyncio
async def test_post_text_only_ok(tmp_path):
    p = await _setup(tmp_path, access_token="T", org_id="42")
    _attach(p._client, _Session())
    out = json.loads(await p._post({"text": "Salom LinkedIn"}))
    assert out["status"] == "ok" and out["post_id"] == "urn:li:share:777"


@pytest.mark.asyncio
async def test_post_with_image_reads_file(tmp_path):
    img = tmp_path / "generated" / "x.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"PNGDATA")
    p = await _setup(tmp_path, access_token="T", org_id="42")
    sess = _Session()
    _attach(p._client, sess)
    out = json.loads(await p._post({"text": "hi", "image_path": "generated/x.png"}))
    assert out["status"] == "ok" and out["with_image"] is True
    assert any(m == "PUT" for m, u, j in sess.calls)


@pytest.mark.asyncio
async def test_post_missing_image_errors(tmp_path):
    p = await _setup(tmp_path, access_token="T", org_id="42")
    out = json.loads(await p._post({"text": "hi", "image_path": "nope.png"}))
    assert out["status"] == "error" and "not found" in out["error"]


@pytest.mark.asyncio
async def test_post_without_org_errors(tmp_path):
    p = await _setup(tmp_path, access_token="T")  # no org_id
    out = json.loads(await p._post({"text": "hi"}))
    assert out["status"] == "error" and "org_id" in out["error"]


@pytest.mark.asyncio
async def test_post_api_error_surfaced(tmp_path):
    p = await _setup(tmp_path, access_token="T", org_id="1")

    async def boom(text, image_bytes=None, visibility="PUBLIC"):
        return {"status": 403, "post_id": "", "body": {"message": "forbidden"}, "with_image": False}

    p._client.post = boom
    out = json.loads(await p._post({"text": "hi"}))
    assert out["status"] == "error" and "403" in out["error"]
