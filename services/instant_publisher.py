"""
services/instant_publisher.py — Tiroo edition
===============================================
Every article is published immediately after image generation.
No priority gate — all posts go straight to all 4 platforms.
"""
from __future__ import annotations

import os

import psycopg2

from DB.db import db_execute
from services.publish_pipeline import PublishPipeline
from utils.logger import logger

_pipeline = PublishPipeline()


def _claim_queue_row(queue_id: int) -> bool:
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE news_queue
                SET status        = 'processing',
                    processing_at = NOW(),
                    last_updated  = NOW()
                WHERE id = %s AND status = 'pending'
                """,
                (queue_id,),
            )
            claimed = cur.rowcount == 1
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def instant_publish(post: dict) -> None:
    """Claim the queue row and immediately publish to all platforms."""
    queue_id   = post["id"]
    title_snip = (post.get("title") or "")[:80]

    if not _claim_queue_row(queue_id):
        logger.warning(
            f"🚨 INSTANT PUBLISH SKIPPED — row already claimed "
            f"| queue_id={queue_id} | '{title_snip}'"
        )
        return

    logger.info(f"🚨 INSTANT PUBLISH START | queue_id={queue_id} | '{title_snip}'")

    results = _pipeline.publish(post)

    tg_status = results.get("telegram",  "skipped")
    ig_status = results.get("instagram", "skipped")
    fb_status = results.get("facebook",  "skipped")
    tw_status = results.get("twitter",   "skipped")

    db_execute(
        """
        UPDATE news_queue
        SET
            status           = 'published',
            telegram_status  = %s,
            instagram_status = %s,
            twitter_status   = %s,
            facebook_status  = %s,
            published_at     = NOW(),
            last_updated     = NOW()
        WHERE id = %s AND status = 'processing'
        """,
        (tg_status, ig_status, tw_status, fb_status, queue_id),
    )

    logger.info(
        f"🚨 INSTANT PUBLISH COMPLETE | queue_id={queue_id} "
        f"| tg={tg_status} ig={ig_status} "
        f"tw={tw_status} fb={fb_status}"
    )
