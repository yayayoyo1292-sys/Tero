from __future__ import annotations

import logging
import os

import requests
from requests_oauthlib import OAuth1
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from utils.text_filter import sanitize_text

logger = logging.getLogger(__name__)

TWITTER_API_V2 = "https://api.twitter.com/2/tweets"
TWITTER_UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"


class TwitterPublisher:
    """
    ينشر على Twitter/X عبر API v2.

    المتغيرات المطلوبة في .env:
        TWITTER_API_KEY
        TWITTER_API_SECRET
        TWITTER_ACCESS_TOKEN
        TWITTER_ACCESS_TOKEN_SECRET

    خطوات الحصول عليهم:
        1. روح developer.twitter.com → Projects & Apps → New App
        2. من "Keys and Tokens" جيب الـ 4 قيم دول
        3. تأكد إن الـ App عنده "Read and Write" permissions
    """

    def __init__(self) -> None:
        self.api_key              = os.getenv("TWITTER_API_KEY")
        self.api_secret           = os.getenv("TWITTER_API_SECRET")
        self.access_token         = os.getenv("TWITTER_ACCESS_TOKEN")
        self.access_token_secret  = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

        missing = [k for k, v in {
            "TWITTER_API_KEY":             self.api_key,
            "TWITTER_API_SECRET":          self.api_secret,
            "TWITTER_ACCESS_TOKEN":        self.access_token,
            "TWITTER_ACCESS_TOKEN_SECRET": self.access_token_secret,
        }.items() if not v]

        if missing:
            logger.warning(f"⚠️ Twitter: المتغيرات التالية ناقصة: {', '.join(missing)}")

    def _auth(self) -> OAuth1:
        return OAuth1(
            self.api_key,
            self.api_secret,
            self.access_token,
            self.access_token_secret,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=3, max=20),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _upload_media(self, image_url: str) -> str | None:
        """رفع صورة على Twitter وإرجاع الـ media_id"""
        try:
            img_resp = requests.get(image_url, timeout=15)
            img_resp.raise_for_status()

            upload_resp = requests.post(
                TWITTER_UPLOAD,
                auth=self._auth(),
                files={"media": img_resp.content},
                timeout=30,
            )
            if upload_resp.status_code != 200:
                logger.warning(f"Twitter media upload failed: {upload_resp.text[:200]}")
                return None
            return upload_resp.json().get("media_id_string")
        except Exception as exc:
            logger.warning(f"Twitter media upload error: {exc}")
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=3, max=20),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _post_tweet(self, text: str, media_id: str | None) -> dict:
        payload: dict = {"text": text}
        if media_id:
            payload["media"] = {"media_ids": [media_id]}

        resp = requests.post(
            TWITTER_API_V2,
            auth=self._auth(),
            json=payload,
            timeout=30,
        )
        data = resp.json()
        if resp.status_code not in (200, 201):
            err = data.get("detail") or data.get("title") or resp.text[:200]
            raise requests.RequestException(f"Twitter API error: {err}")
        return data

    def publish(self, post: dict) -> bool:
        if not all([self.api_key, self.api_secret, self.access_token, self.access_token_secret]):
            return False

        title     = sanitize_text(post.get("title", ""))
        content   = sanitize_text((post.get("content") or "")[:200])
        url       = post.get("url", "")
        image_url = post.get("image_url")

        tweet_text = f"📰 {title}\n\n{content}\n\n🔗 {url}"[:280]

        try:
            media_id = self._upload_media(image_url) if image_url else None
            result   = self._post_tweet(tweet_text, media_id)
            tweet_id = result.get("data", {}).get("id", "?")
            logger.info(f"✅ Twitter sent | tweet_id={tweet_id} | id={post.get('id')} | {title[:50]}")
            return True
        except Exception as exc:
            logger.error(f"❌ Twitter failed | id={post.get('id')}: {exc}")
            return False
