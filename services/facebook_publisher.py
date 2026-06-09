from __future__ import annotations

import logging
import os

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from utils.text_filter import sanitize_text

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v19.0"


class FacebookPublisher:
    """
    ينشر مباشرة على Facebook Page عبر Graph API.
    المتغيرات المطلوبة في .env:
        FACEBOOK_PAGE_ID
        FACEBOOK_PAGE_ACCESS_TOKEN
    """

    def __init__(self) -> None:
        self.page_id    = os.getenv("FACEBOOK_PAGE_ID")
        self.page_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
        if not self.page_id or not self.page_token:
            logger.warning("⚠️ FACEBOOK_PAGE_ID أو FACEBOOK_PAGE_ACCESS_TOKEN غير موجود")

    def _enabled(self) -> bool:
        from config.settings import ENABLE_FACEBOOK_POSTING, FACEBOOK_START_DATE, FACEBOOK_END_DATE
        from datetime import datetime, timezone
        if not ENABLE_FACEBOOK_POSTING:
            return False
        today = datetime.now(timezone.utc).date()
        return FACEBOOK_START_DATE.date() <= today <= FACEBOOK_END_DATE.date()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _call_graph(self, endpoint: str, payload: dict) -> dict:
        url  = f"{GRAPH_API}/{endpoint}"
        resp = requests.post(url, data=payload, timeout=30)
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            err = data.get("error", {}).get("message", resp.text[:200])
            logger.error(f"Facebook Graph API error: {err}")
            raise requests.RequestException(err)
        return data

    def publish(self, post: dict) -> bool:
        if not self._enabled():
            logger.info("Facebook publishing disabled أو خارج النطاق الزمني")
            return False
        if not self.page_id or not self.page_token:
            return False

        title    = sanitize_text(post.get("title", ""))
        content  = sanitize_text((post.get("content") or "")[:500])
        category = post.get("source_label") or post.get("category") or ""
        priority = post.get("priority_score", 0)
        url      = post.get("url", "")
        image_url = post.get("image_url")

        urgent   = "🔴 عاجل\n\n" if priority >= 9 else ""
        cat_line = f"📂 {category}\n\n" if category else ""
        message  = f"{urgent}📰 {title}\n\n{cat_line}{content}\n\n🔗 {url}"

        try:
            if image_url:
                # نشر صورة مع caption
                self._call_graph(f"{self.page_id}/photos", {
                    "url":          image_url,
                    "caption":      message,
                    "access_token": self.page_token,
                })
            else:
                # نشر نص + رابط
                self._call_graph(f"{self.page_id}/feed", {
                    "message":      message,
                    "link":         url,
                    "access_token": self.page_token,
                })
            logger.info(f"✅ Facebook sent | id={post.get('id')} | {title[:50]}")
            return True
        except Exception as exc:
            logger.error(f"❌ Facebook failed | id={post.get('id')}: {exc}")
            return False
