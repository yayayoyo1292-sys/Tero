from __future__ import annotations

import logging
import os
import time

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


class InstagramPublisher:
    """
    ينشر على Instagram Business Account عبر Facebook Graph API.

    المتغيرات المطلوبة في .env:
        INSTAGRAM_ACCOUNT_ID      (رقم الـ Instagram Business Account)
        INSTAGRAM_PAGE_ACCESS_TOKEN  (نفس الـ token بتاع الـ Facebook Page المربوطة)

    خطوات الحصول عليهم:
        1. روح Graph API Explorer
        2. اختار الـ Page المربوطة بالـ Instagram Account
        3. اطلب permission: instagram_basic, instagram_content_publish, pages_read_engagement
        4. GET /{page-id}?fields=instagram_business_account  → جيب الـ INSTAGRAM_ACCOUNT_ID
        5. استخدم الـ Page Access Token كـ INSTAGRAM_PAGE_ACCESS_TOKEN
    """

    def __init__(self) -> None:
        self.ig_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
        self.access_token  = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")
        if not self.ig_account_id or not self.access_token:
            logger.warning("⚠️ INSTAGRAM_ACCOUNT_ID أو INSTAGRAM_PAGE_ACCESS_TOKEN غير موجود")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _call_graph(self, endpoint: str, payload: dict) -> dict:
        url  = f"{GRAPH_API}/{endpoint}"
        resp = requests.post(url, params=payload, timeout=30)
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            err = data.get("error", {}).get("message", resp.text[:200])
            logger.error(f"Instagram Graph API error: {err}")
            raise requests.RequestException(err)
        return data

    def _create_media_container(self, image_url: str, caption: str) -> str:
        """الخطوة 1: إنشاء media container وإرجاع الـ creation_id"""
        data = self._call_graph(f"{self.ig_account_id}/media", {
            "image_url":    image_url,
            "caption":      caption,
            "access_token": self.access_token,
        })
        return data["id"]

    def _publish_container(self, creation_id: str) -> str:
        """الخطوة 2: نشر الـ container وإرجاع الـ post id"""
        data = self._call_graph(f"{self.ig_account_id}/media_publish", {
            "creation_id":  creation_id,
            "access_token": self.access_token,
        })
        return data["id"]

    def publish(self, post: dict) -> bool:
        if not self.ig_account_id or not self.access_token:
            return False

        image_url = post.get("image_url")
        if not image_url:
            logger.warning(f"Instagram يتطلب صورة — خبر بدون صورة تم تخطيه | id={post.get('id')}")
            return False

        title   = sanitize_text(post.get("title", ""))
        content = sanitize_text((post.get("content") or "")[:300])
        url     = post.get("url", "")

        # Instagram caption: عنوان + محتوى + رابط
        caption = f"📰 {title}\n\n{content}\n\n🔗 {url}"[:2200]

        try:
            # خطوتين: إنشاء container ثم نشره
            creation_id = self._create_media_container(image_url, caption)

            # Instagram بيحتاج ثانية أو اتنين بين الخطوتين
            time.sleep(2)

            post_id = self._publish_container(creation_id)
            logger.info(f"✅ Instagram sent | post_id={post_id} | id={post.get('id')} | {title[:50]}")
            return True

        except Exception as exc:
            logger.error(f"❌ Instagram failed | id={post.get('id')}: {exc}")
            return False
