"""
services/scheduler.py — Tiroo edition
=======================================
Background publishing worker.
Retries posts that were rate-limited during instant publish.
All 4 platforms are always targeted — no priority routing.
"""
from __future__ import annotations

import re
import time

import psycopg2

from services.queue_manager import QueueManager
from services.publish_pipeline import PublishPipeline, ALL_PLATFORMS
from DB.db import db_execute
from utils.logger import logger

_queue    = QueueManager()
_pipeline = PublishPipeline()

# Statuses that mean "done, never retry"
_FINAL_DONE = frozenset({
    "sent",
    "skipped:already_published",
    "skipped:fb_disabled",
    "skipped:fb_outside_date_window",
    "skipped:unknown_platform",
    "failed",  # permanent failures (auth/credits) — no retry
})


def _is_done(status: str) -> bool:
    return status in _FINAL_DONE


def _needs_retry(status: str) -> bool:
    return status.startswith("rate_limited")


def _get_retry_delay(statuses: dict) -> int:
    """Extract the smallest cooldown from rate_limited status strings."""
    min_delay = 60
    for s in statuses.values():
        if s.startswith("rate_limited"):
            m = re.search(r"(\d+)s", s)
            if m:
                min_delay = min(min_delay, int(m.group(1)) + 2)
    return max(min_delay, 10)


def _publish_one(post: dict) -> None:
    post_id    = post["id"]
    title_snip = (post.get("title") or "")[:60]

    current = {
        "telegram":  post.get("telegram_status")  or "pending",
        "instagram": post.get("instagram_status") or "pending",
        "facebook":  post.get("facebook_status")  or "pending",
        "twitter":   post.get("twitter_status")   or "pending",
    }

    # Platforms that still need work
    pending = [p for p in ALL_PLATFORMS if not _is_done(current[p])]

    if not pending:
        db_execute(
            """
            UPDATE news_queue
            SET status = 'published', published_at = NOW(), last_updated = NOW()
            WHERE id = %s AND status = 'processing'
            """,
            (post_id,),
        )
        logger.debug(f"✅ All platforms done (scheduler skip) | id={post_id}")
        return

    # Skip telegram if already sent
    if _is_done(current["telegram"]):
        post["_skip_telegram"] = True

    results = _pipeline.publish(post)

    # Merge results with existing status
    new_statuses: dict[str, str] = {}
    for p in ALL_PLATFORMS:
        if p in results:
            new_statuses[p] = results[p]
        elif _is_done(current[p]):
            new_statuses[p] = current[p]
        else:
            new_statuses[p] = current[p]

    # Still needs retry?
    needs_retry = any(_needs_retry(new_statuses[p]) for p in ALL_PLATFORMS)

    if needs_retry:
        retry_count = int(post.get("retry_count") or 0) + 1
        MAX_RETRIES = 5

        if retry_count > MAX_RETRIES:
            logger.warning(
                f"⛔ Max retries ({MAX_RETRIES}) reached | id={post_id} | marking published"
            )
            db_execute(
                """
                UPDATE news_queue
                SET status='published', published_at=NOW(), last_updated=NOW(),
                    telegram_status=%s, instagram_status=%s,
                    facebook_status=%s, twitter_status=%s
                WHERE id=%s AND status='processing'
                """,
                (
                    new_statuses["telegram"],
                    new_statuses["instagram"],
                    new_statuses["facebook"],
                    new_statuses["twitter"],
                    post_id,
                ),
            )
            return

        retry_delay = _get_retry_delay(new_statuses)
        db_execute(
            """
            UPDATE news_queue
            SET
                status           = 'pending',
                telegram_status  = %s,
                instagram_status = %s,
                facebook_status  = %s,
                twitter_status   = %s,
                retry_after      = NOW() + (%s || ' seconds')::interval,
                retry_count      = %s,
                last_updated     = NOW()
            WHERE id = %s AND status = 'processing'
            """,
            (
                new_statuses["telegram"],
                new_statuses["instagram"],
                new_statuses["facebook"],
                new_statuses["twitter"],
                retry_delay,
                retry_count,
                post_id,
            ),
        )
        logger.info(
            f"♻️  Retry queued | id={post_id} | attempt {retry_count}/{MAX_RETRIES} "
            f"| retry in {retry_delay}s"
        )
    else:
        db_execute(
            """
            UPDATE news_queue
            SET
                status           = 'published',
                telegram_status  = %s,
                instagram_status = %s,
                facebook_status  = %s,
                twitter_status   = %s,
                published_at     = NOW(),
                last_updated     = NOW()
            WHERE id = %s AND status = 'processing'
            """,
            (
                new_statuses["telegram"],
                new_statuses["instagram"],
                new_statuses["facebook"],
                new_statuses["twitter"],
                post_id,
            ),
        )
        logger.info(
            f"✅ Scheduler published | id={post_id} "
            f"tg={new_statuses['telegram']} "
            f"ig={new_statuses['instagram']} "
            f"tw={new_statuses['twitter']} "
            f"fb={new_statuses['facebook']}"
        )


def publishing_worker() -> None:
    logger.info("🚀 Publishing worker started")
    consecutive_errors = 0

    while True:
        try:
            recovered = _queue.fail_stale_processing(max_minutes=10)
            if recovered:
                logger.warning(f"♻️  Stale recovery: {recovered} rows reset to pending")

            post = _queue.get_next_post()
            if post:
                _publish_one(post)
                consecutive_errors = 0
                continue

        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            consecutive_errors += 1
            wait = min(consecutive_errors * 3, 30)
            logger.error(f"⚠️ Publishing worker DB error (#{consecutive_errors}): {exc}")
            time.sleep(wait)
            continue

        except Exception as exc:
            consecutive_errors += 1
            wait = min(consecutive_errors * 3, 30)
            logger.error(
                f"⚠️ Publishing worker error (#{consecutive_errors}): {exc}",
                exc_info=True,
            )
            time.sleep(wait)
            continue

        else:
            consecutive_errors = 0

        time.sleep(3)
