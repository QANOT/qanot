"""Thin async wrapper over Telegram Bot API for channel management.

We don't depend on aiogram here — plugins shouldn't pull in the agent's
HTTP framework. Plain aiohttp + the bot_token handles everything we need:

  Metadata:     getChat, getChatMember, getChatMemberCount, getMe
  Text posts:   sendMessage, editMessageText
  Media posts:  sendPhoto, sendVideo, sendDocument, sendAudio, sendAnimation
  Media groups: sendMediaGroup (albums of 2-10 photos/videos)
  Polls:        sendPoll (regular + quiz modes)
  Edit media:   editMessageCaption
  Management:   deleteMessage, pinChatMessage, unpinChatMessage

Supports three ways to reference media:
  - public HTTP(S) URL  → Telegram fetches it (cheapest, fastest)
  - file_id             → previously uploaded media, reusable
  - local file path     → multipart upload from disk (largest overhead)

All methods raise TelegramAPIError on non-ok responses so callers can
map them via engine.errors.map_exception.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp

from tg_engine.errors import TelegramAPIError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60)  # uploads can be slow
MAX_UPLOAD_MB = 50  # Bot API's own limit for sendDocument (most methods)


class TelegramClient:
    """Minimal async Bot API client bound to a single bot token."""

    def __init__(self, bot_token: str, *, api_base: str | None = None) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        self._base = (api_base or "https://api.telegram.org").rstrip("/")
        self._token = bot_token
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self._session

    async def _call(self, method: str, payload: dict) -> dict:
        """POST to /bot<TOKEN>/<method> and unwrap the 'result' field.

        Bot API always returns {ok: bool, result: ..., description: ..., error_code: ...}.
        """
        url = f"{self._base}/bot{self._token}/{method}"
        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                text = await resp.text()
                raise TelegramAPIError(
                    resp.status,
                    f"non-JSON response: {text[:300]}",
                )
        if not isinstance(body, dict):
            raise TelegramAPIError(resp.status, f"unexpected response: {str(body)[:300]}")
        if not body.get("ok"):
            raise TelegramAPIError(
                body.get("error_code") or resp.status,
                body.get("description") or "",
                body.get("parameters") or {},
            )
        return body.get("result") if body.get("result") is not None else {}

    # ── Channel metadata ────────────────────────────────────────

    async def get_chat(self, chat_id: int | str) -> dict:
        """Fetch channel info — verifies the channel exists and bot can see it."""
        return await self._call("getChat", {"chat_id": chat_id})

    async def get_chat_member_count(self, chat_id: int | str) -> int:
        """Return subscriber count. Channels + groups supported."""
        r = await self._call("getChatMemberCount", {"chat_id": chat_id})
        # API returns the number directly as the result
        if isinstance(r, int):
            return r
        if isinstance(r, dict) and "count" in r:
            return int(r["count"])
        return 0

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> dict:
        """Check bot's admin status + permissions in the channel."""
        return await self._call(
            "getChatMember", {"chat_id": chat_id, "user_id": user_id},
        )

    async def get_me(self) -> dict:
        return await self._call("getMe", {})

    # ── Posting ─────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        disable_web_page_preview: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        disable_web_page_preview: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("editMessageText", payload)

    async def delete_message(self, chat_id: int | str, message_id: int) -> dict:
        return await self._call(
            "deleteMessage", {"chat_id": chat_id, "message_id": message_id},
        )

    async def pin_chat_message(
        self, chat_id: int | str, message_id: int,
        *, disable_notification: bool = True,
    ) -> dict:
        return await self._call(
            "pinChatMessage",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": disable_notification,
            },
        )

    async def unpin_chat_message(
        self, chat_id: int | str, message_id: int | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if message_id is not None:
            payload["message_id"] = message_id
        return await self._call("unpinChatMessage", payload)

    # ── Media posting ───────────────────────────────────────────

    async def _call_multipart(
        self,
        method: str,
        form_fields: dict[str, Any],
        files: dict[str, str | Path] | None = None,
    ) -> dict:
        """POST with multipart/form-data — used when uploading local files.

        `form_fields` are scalar fields (dicts/lists get JSON-encoded).
        `files` maps form-field-name → local path.
        """
        url = f"{self._base}/bot{self._token}/{method}"
        session = await self._get_session()

        data = aiohttp.FormData()
        for k, v in form_fields.items():
            if v is None:
                continue
            if isinstance(v, (dict, list)):
                data.add_field(k, json.dumps(v, ensure_ascii=False))
            else:
                data.add_field(k, str(v))

        opened_files: list[Any] = []
        try:
            if files:
                for form_name, path in files.items():
                    p = Path(path)
                    if not p.exists():
                        raise TelegramAPIError(
                            400, f"file not found: {path}",
                        )
                    # Guard against oversized uploads before we stream them
                    size_mb = p.stat().st_size / (1024 * 1024)
                    if size_mb > MAX_UPLOAD_MB:
                        raise TelegramAPIError(
                            400,
                            f"file {p.name} is {size_mb:.1f}MB — exceeds Bot API limit ({MAX_UPLOAD_MB}MB). "
                            "Use a local Bot API server or post via public URL.",
                        )
                    fh = p.open("rb")
                    opened_files.append(fh)
                    data.add_field(form_name, fh, filename=p.name)

            async with session.post(url, data=data) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    raise TelegramAPIError(
                        resp.status, f"non-JSON response: {text[:300]}",
                    )
            if not isinstance(body, dict):
                raise TelegramAPIError(resp.status, f"unexpected response: {str(body)[:300]}")
            if not body.get("ok"):
                raise TelegramAPIError(
                    body.get("error_code") or resp.status,
                    body.get("description") or "",
                    body.get("parameters") or {},
                )
            return body.get("result") if body.get("result") is not None else {}
        finally:
            for fh in opened_files:
                try:
                    fh.close()
                except Exception:
                    pass

    def _media_kind(self, media: str) -> str:
        """Classify a media reference as 'url', 'file_path', or 'file_id'.

        Heuristics:
          - starts with http:// or https:// → url
          - points to an existing file on disk → file_path
          - otherwise → file_id (Telegram-managed reusable handle)
        """
        if not media:
            return "file_id"
        s = str(media)
        if s.lower().startswith(("http://", "https://")):
            return "url"
        if os.path.exists(s):
            return "file_path"
        return "file_id"

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = "HTML",
        has_spoiler: bool = False,
        show_caption_above_media: bool = False,
        protect_content: bool = False,
        disable_notification: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_media_generic(
            "sendPhoto",
            chat_id=chat_id,
            media_field="photo",
            media=photo,
            caption=caption,
            parse_mode=parse_mode,
            has_spoiler=has_spoiler,
            show_caption_above_media=show_caption_above_media,
            protect_content=protect_content,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
        )

    async def send_video(
        self,
        chat_id: int | str,
        video: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = "HTML",
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        supports_streaming: bool = True,
        has_spoiler: bool = False,
        show_caption_above_media: bool = False,
        protect_content: bool = False,
        disable_notification: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_media_generic(
            "sendVideo",
            chat_id=chat_id,
            media_field="video",
            media=video,
            caption=caption,
            parse_mode=parse_mode,
            duration=duration,
            width=width,
            height=height,
            supports_streaming=supports_streaming,
            has_spoiler=has_spoiler,
            show_caption_above_media=show_caption_above_media,
            protect_content=protect_content,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
        )

    async def send_animation(
        self,
        chat_id: int | str,
        animation: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = "HTML",
        has_spoiler: bool = False,
        show_caption_above_media: bool = False,
        protect_content: bool = False,
        disable_notification: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_media_generic(
            "sendAnimation",
            chat_id=chat_id,
            media_field="animation",
            media=animation,
            caption=caption,
            parse_mode=parse_mode,
            has_spoiler=has_spoiler,
            show_caption_above_media=show_caption_above_media,
            protect_content=protect_content,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
        )

    async def send_document(
        self,
        chat_id: int | str,
        document: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = "HTML",
        disable_content_type_detection: bool = False,
        protect_content: bool = False,
        disable_notification: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_media_generic(
            "sendDocument",
            chat_id=chat_id,
            media_field="document",
            media=document,
            caption=caption,
            parse_mode=parse_mode,
            disable_content_type_detection=disable_content_type_detection,
            protect_content=protect_content,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
        )

    async def send_audio(
        self,
        chat_id: int | str,
        audio: str,
        *,
        caption: str | None = None,
        parse_mode: str | None = "HTML",
        duration: int | None = None,
        performer: str | None = None,
        title: str | None = None,
        protect_content: bool = False,
        disable_notification: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_media_generic(
            "sendAudio",
            chat_id=chat_id,
            media_field="audio",
            media=audio,
            caption=caption,
            parse_mode=parse_mode,
            duration=duration,
            performer=performer,
            title=title,
            protect_content=protect_content,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
        )

    async def _send_media_generic(
        self,
        method: str,
        *,
        chat_id: int | str,
        media_field: str,
        media: str,
        **extras: Any,
    ) -> dict:
        """Shared photo/video/doc/audio/animation path.

        Routes to JSON or multipart based on the media reference kind.
        """
        kind = self._media_kind(media)

        # Build scalar fields (everything except the media itself)
        fields: dict[str, Any] = {"chat_id": chat_id}
        for k, v in extras.items():
            if v is None:
                continue
            if isinstance(v, bool) and not v and k not in (
                # preserve False for flags that default True server-side
                "supports_streaming",
            ):
                continue
            fields[k] = v

        if kind == "url" or kind == "file_id":
            fields[media_field] = media
            return await self._call(method, fields)

        # kind == "file_path" — multipart upload
        return await self._call_multipart(
            method, fields, files={media_field: media},
        )

    async def send_media_group(
        self,
        chat_id: int | str,
        media: list[dict],
        *,
        disable_notification: bool = False,
        protect_content: bool = False,
    ) -> list[dict]:
        """Send an album of 2-10 photos/videos.

        `media` is a list of InputMedia* dicts. Each entry:
          {
            "type": "photo" | "video" | "document" | "audio",
            "media": "<url or file_id or attach://<name>>",
            "caption": "optional, only on FIRST item for album caption",
            "parse_mode": "HTML"
          }

        For local file uploads within an album, use "attach://<field>"
        as `media` and pass the file under that field name. This
        implementation handles URL/file_id media only; local files in
        albums would need additional multipart scaffolding — callers
        should upload local files to a public URL first or post
        individually if album uploads from disk are needed.

        Returns a list of Message objects (one per media item).
        """
        if not isinstance(media, list) or not (2 <= len(media) <= 10):
            raise TelegramAPIError(
                400, f"media must be a list of 2-10 items, got {len(media) if isinstance(media, list) else '?'}",
            )

        # Validate each item + verify no local-file references
        for i, item in enumerate(media):
            if not isinstance(item, dict):
                raise TelegramAPIError(400, f"media[{i}] is not a dict")
            if item.get("type") not in ("photo", "video", "document", "audio"):
                raise TelegramAPIError(400, f"media[{i}].type invalid: {item.get('type')}")
            ref = item.get("media", "")
            if self._media_kind(ref) == "file_path":
                raise TelegramAPIError(
                    400,
                    f"media[{i}]: local file uploads in albums not supported in v1. "
                    "Upload via public URL or post individually via send_photo/send_video.",
                )

        payload = {
            "chat_id": chat_id,
            "media": media,
            "disable_notification": disable_notification,
            "protect_content": protect_content,
        }
        result = await self._call("sendMediaGroup", payload)
        # sendMediaGroup returns a list, but _call unwraps {result: [...]} into the list
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and result.get("result"):
            return result["result"]
        return [result] if result else []

    async def send_poll(
        self,
        chat_id: int | str,
        question: str,
        options: list[str],
        *,
        is_anonymous: bool = True,
        poll_type: str = "regular",  # "regular" or "quiz"
        allows_multiple_answers: bool = False,
        correct_option_id: int | None = None,  # quiz only
        explanation: str | None = None,         # quiz only
        explanation_parse_mode: str | None = "HTML",
        open_period: int | None = None,         # auto-close after N seconds
        close_date: int | None = None,           # or unix timestamp
        disable_notification: bool = False,
        protect_content: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        """Send a poll or quiz.

        Quiz requires ``poll_type="quiz"`` + ``correct_option_id`` (index
        into options). Explanation appears after user answers.
        """
        if poll_type not in ("regular", "quiz"):
            raise TelegramAPIError(400, f"poll_type must be 'regular' or 'quiz', got {poll_type!r}")
        if not isinstance(options, list) or not (2 <= len(options) <= 10):
            raise TelegramAPIError(400, f"options must be 2-10 strings, got {len(options) if isinstance(options, list) else '?'}")
        if poll_type == "quiz":
            if correct_option_id is None or not (0 <= correct_option_id < len(options)):
                raise TelegramAPIError(
                    400, "quiz requires correct_option_id (0-based index into options)",
                )
            if allows_multiple_answers:
                raise TelegramAPIError(400, "quizzes cannot allow multiple answers")

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question,
            "options": options,
            "is_anonymous": is_anonymous,
            "type": poll_type,
            "allows_multiple_answers": allows_multiple_answers,
            "disable_notification": disable_notification,
            "protect_content": protect_content,
        }
        if poll_type == "quiz":
            payload["correct_option_id"] = correct_option_id
            if explanation:
                payload["explanation"] = explanation
                if explanation_parse_mode:
                    payload["explanation_parse_mode"] = explanation_parse_mode
        if open_period is not None:
            payload["open_period"] = open_period
        if close_date is not None:
            payload["close_date"] = close_date
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        return await self._call("sendPoll", payload)

    async def edit_message_caption(
        self,
        chat_id: int | str,
        message_id: int,
        caption: str,
        *,
        parse_mode: str | None = "HTML",
        show_caption_above_media: bool = False,
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption,
            "show_caption_above_media": show_caption_above_media,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("editMessageCaption", payload)

    async def stop_poll(self, chat_id: int | str, message_id: int) -> dict:
        """Close an open poll early. Returns the final poll state."""
        return await self._call(
            "stopPoll", {"chat_id": chat_id, "message_id": message_id},
        )


# ── Reply markup helpers ─────────────────────────────────────────


def make_inline_keyboard(buttons: list[list[dict]]) -> dict:
    """Build a reply_markup for an inline keyboard.

    `buttons` is a 2D list of button specs. Each button dict must have
    `text` and one of `url` or `callback_data`. Example:

        [[{"text": "Sotib olish", "url": "https://shop.uz/123"}],
         [{"text": "A'zo bo'lish", "url": "https://t.me/channel"}]]
    """
    rows: list[list[dict]] = []
    for row in buttons or []:
        out_row: list[dict] = []
        if not isinstance(row, list):
            continue
        for btn in row:
            if not isinstance(btn, dict):
                continue
            text = str(btn.get("text") or "").strip()
            if not text:
                continue
            entry: dict[str, Any] = {"text": text}
            if btn.get("url"):
                entry["url"] = str(btn["url"])
            elif btn.get("callback_data"):
                entry["callback_data"] = str(btn["callback_data"])
            else:
                # Button with text only — Telegram requires a url or callback_data;
                # default to a no-op callback so it still renders as a button.
                entry["callback_data"] = "noop"
            out_row.append(entry)
        if out_row:
            rows.append(out_row)
    return {"inline_keyboard": rows}
