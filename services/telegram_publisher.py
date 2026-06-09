
from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from dotenv import load_dotenv
from utils.text_filter import sanitize_text

load_dotenv()
logger = logging.getLogger(__name__)


_TOKEN   = os.getenv("PRIORITY_TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
_CHAT_ID = os.getenv("PRIORITY_TELEGRAM_CHAT_ID")  or os.getenv("CHAT_ID")

_CAPTION_LIMIT = 1024
_SEPARATOR     = "━━━━━━━━━━"


class PriorityTelegramPublisher:

    def __init__(self) -> None:
        if not _TOKEN or not _CHAT_ID:
            raise RuntimeError(
                "Telegram bot token and chat ID are not set.\n"
                "Set PRIORITY_TELEGRAM_BOT_TOKEN + PRIORITY_TELEGRAM_CHAT_ID\n"
                "or fall back to TOKEN + CHAT_ID in your .env file."
            )

    def _caption(self, post: dict) -> str:
        
        title    = sanitize_text(post.get("title", ""))
        content  = sanitize_text((post.get("content") or "")[:500])
        category = post.get("source_label") or post.get("category") or ""
        url      = post.get("url", "")

        parts = [f"📰 {title}"]


        if category:
            parts.append(f"🏷 {category}")


        parts.append(_SEPARATOR)


        if content:
            parts.append(content)


        if url:
            parts.append(f"🔗 {url}")

        return "\n\n".join(parts)[:_CAPTION_LIMIT]

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _send(self, endpoint: str, **kwargs) -> requests.Response:
        resp = requests.post(
            f"https://api.telegram.org/bot{_TOKEN}/{endpoint}",
            timeout=30,
            **kwargs,
        )
        if resp.status_code != 200:
            raise requests.RequestException(
                f"Telegram {endpoint} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    def publish(self, post: dict) -> bool:

        caption   = self._caption(post)
        image_url = post.get("image_url")

        try:
            if image_url:
                self._send(
                    "sendPhoto",
                    data={
                        "chat_id": _CHAT_ID,
                        "photo":   image_url,
                        "caption": caption,
                    },
                )
            else:
                self._send(
                    "sendMessage",
                    data={
                        "chat_id": _CHAT_ID,
                        "text":    caption,
                    },
                )
            logger.info(f"✅ Priority TG sent | {post.get('title', '')[:60]}")
            return True

        except Exception as exc:
            logger.error(f"❌ Priority TG error: {exc}")
            return False