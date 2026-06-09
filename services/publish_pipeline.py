"""
services/publish_pipeline.py — Tiroo edition
==============================================
Publishing rule: EVERY post goes to Telegram + Instagram + Facebook + Twitter
simultaneously. There is NO priority-based routing.

Rate limiting and idempotency are still enforced per-platform.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Optional

from config.settings import (
    ENABLE_FACEBOOK_POSTING,
    FACEBOOK_START_DATE,
    FACEBOOK_END_DATE,
    INSTAGRAM_MIN_INTERVAL_SECONDS,
    TWITTER_MIN_INTERVAL_SECONDS,
    FACEBOOK_MIN_INTERVAL_SECONDS,
    TELEGRAM_MIN_INTERVAL_SECONDS,
    INSTAGRAM_MAX_PER_HOUR,
    TWITTER_MAX_PER_HOUR,
    FACEBOOK_MAX_PER_HOUR,
    TELEGRAM_MAX_PER_HOUR,
)
from DB.db import db_execute
from utils.text_filter import sanitize_text
from utils.logger import logger
from services.facebook_publisher  import FacebookPublisher
from services.instagram_publisher import InstagramPublisher
from services.twitter_publisher   import TwitterPublisher
from services.telegram_publisher  import PriorityTelegramPublisher

_fb  = FacebookPublisher()
_ig  = InstagramPublisher()
_tw  = TwitterPublisher()
_tg  = PriorityTelegramPublisher()

# All four platforms Tiroo publishes to
ALL_PLATFORMS = ("telegram", "instagram", "facebook", "twitter")


# ── Idempotency ───────────────────────────────────────────────────────────────

def _event_fingerprint(article_id: int, platform: str) -> str:
    raw = f"{article_id}:{platform}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _is_already_published(article_id: Optional[int], platform: str) -> bool:
    if article_id is None:
        return False
    try:
        row = db_execute(
            """
            SELECT id FROM publish_log
            WHERE article_id = %s AND platform = %s AND status = 'sent'
            LIMIT 1
            """,
            (article_id, platform),
            fetch=True,
        )
        return bool(row)
    except Exception:
        return False


def _record_publish_event(
    article_id: Optional[int],
    queue_id: Optional[int],
    platform: str,
    status: str,
    error_msg: Optional[str] = None,
) -> None:
    if article_id is None:
        return
    try:
        db_execute(
            """
            INSERT INTO publish_log
              (article_id, queue_id, platform, status, fingerprint, error_msg)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO UPDATE
              SET status    = EXCLUDED.status,
                  error_msg = EXCLUDED.error_msg,
                  updated_at = NOW()
            """,
            (
                article_id,
                queue_id,
                platform,
                status,
                _event_fingerprint(article_id, platform),
                error_msg,
            ),
        )
    except Exception as exc:
        logger.warning(f"Publish log write failed for {platform}/{article_id}: {exc}")


# ── Rate limiting ─────────────────────────────────────────────────────────────

_RATE_CFG = {
    "instagram": (INSTAGRAM_MIN_INTERVAL_SECONDS, INSTAGRAM_MAX_PER_HOUR),
    "twitter":   (TWITTER_MIN_INTERVAL_SECONDS,   TWITTER_MAX_PER_HOUR),
    "facebook":  (FACEBOOK_MIN_INTERVAL_SECONDS,  FACEBOOK_MAX_PER_HOUR),
    "telegram":  (TELEGRAM_MIN_INTERVAL_SECONDS,  TELEGRAM_MAX_PER_HOUR),
}


def _can_post_now(platform: str) -> tuple[bool, str]:
    cfg = _RATE_CFG.get(platform)
    if not cfg:
        return True, ""
    min_interval, max_per_hour = cfg

    try:
        row = db_execute(
            """
            SELECT EXTRACT(EPOCH FROM NOW()) - EXTRACT(EPOCH FROM sent_at) AS secs
            FROM social_rate_log
            WHERE platform = %s
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            (platform,),
            fetch=True,
        )
        if row and float(row["secs"]) < min_interval:
            wait = min_interval - float(row["secs"])
            return False, f"cooldown {wait:.0f}s remaining"
    except Exception:
        pass

    try:
        row = db_execute(
            """
            SELECT COUNT(*) AS cnt
            FROM social_rate_log
            WHERE platform = %s
              AND sent_at > NOW() - INTERVAL '1 hour'
            """,
            (platform,),
            fetch=True,
        )
        if row and int(row["cnt"]) >= max_per_hour:
            return False, f"hourly cap reached ({max_per_hour}/hr)"
    except Exception:
        pass

    return True, ""


def _record_rate_event(
    platform: str,
    article_id: Optional[int],
    queue_id: Optional[int],
) -> None:
    try:
        db_execute(
            "INSERT INTO social_rate_log (platform, article_id, queue_id) VALUES (%s,%s,%s)",
            (platform, article_id, queue_id),
        )
    except Exception as exc:
        logger.warning(f"Rate log write failed: {exc}")


# ── Per-platform publish ──────────────────────────────────────────────────────

def _publish_to_platform(post: dict, platform: str) -> str:
    _raw_id    = post.get("article_id") or post.get("id")
    article_id: Optional[int] = int(_raw_id) if _raw_id is not None else None
    queue_id   = post.get("id")

    if _is_already_published(article_id, platform):
        logger.debug(f"⏭  Idempotency block | {platform} | article_id={article_id}")
        return "skipped:already_published"

    allowed, reason = _can_post_now(platform)
    if not allowed:
        logger.info(f"⏳ Rate limited | {platform} | article_id={article_id} | {reason}")
        return f"rate_limited:{reason}"

    # Facebook date-window check
    if platform == "facebook":
        if not ENABLE_FACEBOOK_POSTING:
            return "skipped:fb_disabled"
        today = datetime.now(timezone.utc).date()
        if not (FACEBOOK_START_DATE.date() <= today <= FACEBOOK_END_DATE.date()):
            return "skipped:fb_outside_date_window"

    publisher = {
        "instagram": _ig,
        "facebook":  _fb,
        "twitter":   _tw,
    }.get(platform)

    try:
        if platform == "telegram":
            ok = _tg.publish(post)
        elif publisher:
            ok = publisher.publish(post)
        else:
            return "skipped:unknown_platform"

        status = "sent" if ok else "failed"
        _record_rate_event(platform, article_id, queue_id)
        _record_publish_event(article_id, queue_id, platform, status)
        return status

    except Exception as exc:
        err = str(exc)[:200]
        _record_publish_event(article_id, queue_id, platform, "failed", err)
        logger.error(f"❌ {platform.capitalize()} failed | article_id={article_id} | {err}")
        return "failed"


# ── PublishPipeline ───────────────────────────────────────────────────────────

def _print_publish_summary(post: dict, results: dict[str, str]) -> None:
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    title = (post.get("title") or "")[:60]
    aid   = post.get("article_id") or post.get("id")

    def _icon(status: str) -> str:
        if "sent" in status:         return "✅"
        if "failed" in status:       return "❌"
        if "rate_limited" in status: return "⏳"
        return "⏭ "

    sep = "╔" + "═" * 64 + "╗"
    end = "╚" + "═" * 64 + "╝"
    lines = [
        sep,
        f"║  📰 PUBLISHED  [{ts}]  article_id={aid}",
        f"║  Title : {title}",
    ]
    for platform in ALL_PLATFORMS:
        status = results.get(platform, "unknown")
        icon   = _icon(status)
        lines.append(f"║  {icon} {platform:<10}: {status}")
    lines.append(end)
    logger.info("\n".join(lines))


class PublishPipeline:
    """Publish one post to ALL four platforms simultaneously."""

    def publish(self, post: dict, **_kwargs) -> dict[str, str]:
        article_id = post.get("article_id") or post.get("id")
        title_snip = (post.get("title") or "")[:80]

        logger.info(
            f"📤 PublishPipeline.publish | article_id={article_id} | "
            f"ALL platforms | {title_snip}"
        )

        results: dict[str, str] = {}

        # Skip telegram if already sent in a previous attempt
        if post.get("_skip_telegram"):
            results["telegram"] = "sent"
        else:
            results["telegram"] = _publish_to_platform(post, "telegram")

        results["instagram"] = _publish_to_platform(post, "instagram")
        results["facebook"]  = _publish_to_platform(post, "facebook")
        results["twitter"]   = _publish_to_platform(post, "twitter")

        _print_publish_summary(post, results)
        return results

    def publish_platform_only(self, post: dict, platform: str) -> str:
        return _publish_to_platform(post, platform)
