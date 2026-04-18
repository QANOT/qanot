"""Meta Graph API publisher — Instagram Reels + Facebook Reels auto-post.

Flow (Instagram Reels via Graph API):
  1. POST /{ig-user-id}/media with media_type=REELS, video_url, caption, share_to_feed
     → returns creation_id (container)
  2. Poll GET /{creation_id}?fields=status_code until status_code=FINISHED
  3. POST /{ig-user-id}/media_publish with creation_id
     → returns the posted media ID

Requires:
  - META_GRAPH_ACCESS_TOKEN (long-lived page access token)
  - META_IG_USER_ID (Instagram Business account ID linked to a Facebook Page)
  - Public HTTPS URL where the clip is hosted (Graph API downloads it)

Reference: https://developers.facebook.com/docs/instagram-platform/content-publishing
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 600  # 10 min max for container to finish


@dataclass
class PublishResult:
    """Outcome of a publish attempt."""
    ok: bool
    platform: str
    media_id: str | None = None
    permalink: str | None = None
    error: str | None = None
    container_id: str | None = None


async def _post_json(session: aiohttp.ClientSession, url: str, data: dict) -> dict:
    async with session.post(url, data=data) as resp:
        body = await resp.json(content_type=None)
        if resp.status >= 400:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            raise RuntimeError(
                f"Graph API {resp.status}: {err.get('message', 'unknown error')} "
                f"(code {err.get('code')})"
            )
        return body


async def _get_json(session: aiohttp.ClientSession, url: str, params: dict) -> dict:
    async with session.get(url, params=params) as resp:
        body = await resp.json(content_type=None)
        if resp.status >= 400:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            raise RuntimeError(
                f"Graph API {resp.status}: {err.get('message', 'unknown error')}"
            )
        return body


async def publish_instagram_reel(
    video_url: str,
    caption: str,
    *,
    access_token: str,
    ig_user_id: str,
    share_to_feed: bool = True,
    thumb_offset_ms: int | None = None,
) -> PublishResult:
    """Publish a Reel to Instagram via Graph API.

    Args:
        video_url: Publicly-accessible HTTPS URL of the MP4 (Graph API pulls it).
                   Must be reachable from Meta servers — localhost won't work.
                   Use S3/R2/Bunny CDN or a temporary tunnel (ngrok) for testing.
        caption: IG caption including hashtags.
        thumb_offset_ms: Time in ms for the thumbnail frame. None = IG default.
    """
    timeout = aiohttp.ClientTimeout(total=POLL_TIMEOUT_S + 60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Step 1: create container
        create_url = f"{GRAPH_API_BASE}/{ig_user_id}/media"
        create_params = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption[:2200],  # IG caption limit
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token": access_token,
        }
        if thumb_offset_ms is not None:
            create_params["thumb_offset"] = str(thumb_offset_ms)

        try:
            create_resp = await _post_json(session, create_url, create_params)
        except Exception as e:
            return PublishResult(ok=False, platform="instagram", error=f"container create: {e}")

        container_id = create_resp.get("id")
        if not container_id:
            return PublishResult(
                ok=False, platform="instagram",
                error=f"no container id in response: {create_resp}",
            )

        logger.info("IG Reel container created: %s — polling for FINISHED...", container_id)

        # Step 2: poll until ready
        status_url = f"{GRAPH_API_BASE}/{container_id}"
        elapsed = 0
        last_status = ""
        while elapsed < POLL_TIMEOUT_S:
            await asyncio.sleep(POLL_INTERVAL_S)
            elapsed += POLL_INTERVAL_S
            try:
                status = await _get_json(session, status_url, {
                    "fields": "status_code,status",
                    "access_token": access_token,
                })
            except Exception as e:
                logger.warning("Status poll failed (retrying): %s", e)
                continue

            code = status.get("status_code")
            last_status = status.get("status", "")
            logger.debug("IG container %s status: %s", container_id, code)
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                return PublishResult(
                    ok=False, platform="instagram",
                    container_id=container_id,
                    error=f"container {code}: {last_status}",
                )
        else:
            return PublishResult(
                ok=False, platform="instagram",
                container_id=container_id,
                error=f"timeout after {POLL_TIMEOUT_S}s — last status: {last_status}",
            )

        # Step 3: publish
        publish_url = f"{GRAPH_API_BASE}/{ig_user_id}/media_publish"
        try:
            publish_resp = await _post_json(session, publish_url, {
                "creation_id": container_id,
                "access_token": access_token,
            })
        except Exception as e:
            return PublishResult(
                ok=False, platform="instagram",
                container_id=container_id,
                error=f"publish: {e}",
            )

        media_id = publish_resp.get("id")
        # Fetch permalink
        permalink = None
        try:
            meta = await _get_json(session, f"{GRAPH_API_BASE}/{media_id}", {
                "fields": "permalink",
                "access_token": access_token,
            })
            permalink = meta.get("permalink")
        except Exception as e:
            logger.debug("Permalink fetch failed (non-fatal): %s", e)

        return PublishResult(
            ok=True, platform="instagram",
            media_id=media_id, container_id=container_id, permalink=permalink,
        )


async def publish_facebook_reel(
    video_url: str,
    description: str,
    *,
    access_token: str,
    fb_page_id: str,
) -> PublishResult:
    """Publish a Reel to Facebook Page via Graph API.

    Reference: https://developers.facebook.com/docs/video-api/guides/reels-publishing
    """
    timeout = aiohttp.ClientTimeout(total=POLL_TIMEOUT_S + 60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Step 1: start upload session
        start_url = f"{GRAPH_API_BASE}/{fb_page_id}/video_reels"
        try:
            start_resp = await _post_json(session, start_url, {
                "upload_phase": "start",
                "access_token": access_token,
            })
        except Exception as e:
            return PublishResult(ok=False, platform="facebook", error=f"start upload: {e}")

        video_id = start_resp.get("video_id")
        if not video_id:
            return PublishResult(
                ok=False, platform="facebook",
                error=f"no video_id in start response: {start_resp}",
            )

        # Step 2: upload via hosted-URL (simpler than chunked upload)
        # Meta docs: POST to rupload.facebook.com/video-upload with Authorization + file_url
        upload_url = f"https://rupload.facebook.com/video-upload/v21.0/{video_id}"
        headers = {
            "Authorization": f"OAuth {access_token}",
            "file_url": video_url,
        }
        try:
            async with session.post(upload_url, headers=headers) as resp:
                if resp.status >= 400:
                    return PublishResult(
                        ok=False, platform="facebook",
                        error=f"upload: HTTP {resp.status}: {(await resp.text())[:300]}",
                    )
        except Exception as e:
            return PublishResult(ok=False, platform="facebook", error=f"upload: {e}")

        # Step 3: publish
        publish_url = f"{GRAPH_API_BASE}/{fb_page_id}/video_reels"
        try:
            pub_resp = await _post_json(session, publish_url, {
                "access_token": access_token,
                "video_id": video_id,
                "upload_phase": "finish",
                "video_state": "PUBLISHED",
                "description": description[:63200],
            })
        except Exception as e:
            return PublishResult(
                ok=False, platform="facebook",
                error=f"publish: {e}",
            )

        success = pub_resp.get("success", False)
        return PublishResult(
            ok=bool(success),
            platform="facebook",
            media_id=video_id,
            permalink=f"https://facebook.com/{fb_page_id}/videos/{video_id}" if success else None,
            error=None if success else "publish returned success=false",
        )


def build_caption(moment_hook: str, moment_title: str, hashtags: list[str], lang: str = "uz") -> str:
    """Compose an IG/FB caption from moment metadata."""
    parts: list[str] = []
    if moment_hook:
        parts.append(moment_hook.strip())
    if moment_title and moment_title.strip().lower() != moment_hook.strip().lower():
        parts.append("")
        parts.append(moment_title.strip())

    if hashtags:
        parts.append("")
        parts.append(" ".join(f"#{t}" for t in hashtags[:15]))

    return "\n".join(parts)


async def publish_clip(
    clip_path: Path,
    moment_hook: str,
    moment_title: str,
    hashtags: list[str],
    *,
    public_url_base: str | None = None,
    access_token: str | None = None,
    ig_user_id: str | None = None,
    fb_page_id: str | None = None,
    platforms: tuple[str, ...] = ("instagram",),
    uploader_callback=None,
) -> dict[str, PublishResult]:
    """Publish a clip to one or more Meta platforms.

    Args:
        clip_path: Local MP4 file.
        public_url_base: e.g. "https://cdn.example.com/clips". We'll form URL
                        as `{public_url_base}/{clip_path.name}`. If None, uploader_callback
                        is called to upload the file and return a URL.
        uploader_callback: async callable(clip_path) -> str (public URL).
                          Used when public_url_base is None.
        platforms: subset of {"instagram", "facebook"}
    """
    access_token = access_token or os.environ.get("META_GRAPH_ACCESS_TOKEN")
    ig_user_id = ig_user_id or os.environ.get("META_IG_USER_ID")
    fb_page_id = fb_page_id or os.environ.get("META_FB_PAGE_ID")

    if not access_token:
        raise RuntimeError("META_GRAPH_ACCESS_TOKEN required")

    # Resolve public URL for the clip
    if public_url_base:
        video_url = f"{public_url_base.rstrip('/')}/{clip_path.name}"
    elif uploader_callback is not None:
        video_url = await uploader_callback(clip_path)
    else:
        raise RuntimeError(
            "Must provide either public_url_base or uploader_callback — "
            "Meta Graph API requires an HTTPS URL, not a local file."
        )

    caption = build_caption(moment_hook, moment_title, hashtags)
    results: dict[str, PublishResult] = {}

    if "instagram" in platforms:
        if not ig_user_id:
            results["instagram"] = PublishResult(
                ok=False, platform="instagram",
                error="META_IG_USER_ID required for instagram platform",
            )
        else:
            results["instagram"] = await publish_instagram_reel(
                video_url=video_url,
                caption=caption,
                access_token=access_token,
                ig_user_id=ig_user_id,
            )

    if "facebook" in platforms:
        if not fb_page_id:
            results["facebook"] = PublishResult(
                ok=False, platform="facebook",
                error="META_FB_PAGE_ID required for facebook platform",
            )
        else:
            results["facebook"] = await publish_facebook_reel(
                video_url=video_url,
                description=caption,
                access_token=access_token,
                fb_page_id=fb_page_id,
            )

    return results
