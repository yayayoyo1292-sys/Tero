"""
services/queue_manager.py — Tiroo edition
==========================================
Simplified queue manager — no priority scoring, no AI score.
All posts are equal; queue is FIFO by queued_at.
"""
from __future__ import annotations

import logging
from typing import Optional

from config.settings import MAX_QUEUE_AGE_HOURS
from DB.db import db_execute

logger = logging.getLogger(__name__)


class QueueManager:

    def add_to_queue(self, article: dict) -> None:
        """Insert a new item into the queue (FIFO — no scoring)."""
        db_execute(
            """
            INSERT INTO news_queue (
                article_id, title, url, content, image_url,
                created_at, detected_at, scraped_at, queued_at,
                priority_score, status, last_updated,
                category, template_key, source_label
            )
            VALUES (
                %s,%s,%s,%s,%s,
                %s,%s,%s,%s,
                0,'pending',NOW(),
                %s,%s,%s
            )
            ON CONFLICT (url) DO UPDATE SET
                image_url    = EXCLUDED.image_url,
                category     = EXCLUDED.category,
                template_key = EXCLUDED.template_key,
                source_label = EXCLUDED.source_label,
                last_updated = NOW()
            """,
            (
                article["article_id"],
                article["title"],
                article["url"],
                article.get("content"),
                article.get("image_url"),
                article["created_at"],
                article.get("detected_at"),
                article.get("scraped_at"),
                article.get("queued_at"),
                article.get("category", "رياضة"),
                article.get("template_key", "رياضة"),
                article.get("source_label"),
            ),
        )

    def get_next_post(self) -> Optional[dict]:
        """
        Claim the next pending post (FIFO by queued_at).
        Skips posts that are within retry_after window.
        Returns None when the queue is empty.
        """
        row = db_execute(
            """
            UPDATE news_queue
            SET status        = 'processing',
                processing_at = NOW(),
                last_updated  = NOW()
            WHERE id = (
                SELECT id FROM news_queue
                WHERE status = 'pending'
                  AND (retry_after IS NULL OR retry_after <= NOW())
                ORDER BY queued_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            fetch=True,
        )
        return dict(row) if row else None

    def fail_stale_processing(self, max_minutes: int = 10) -> int:
        """Reset rows stuck in 'processing' for too long back to 'pending'."""
        result = db_execute(
            """
            UPDATE news_queue
            SET status       = 'pending',
                last_updated = NOW()
            WHERE status        = 'processing'
              AND processing_at < NOW() - (%s || ' minutes')::interval
            """,
            (max_minutes,),
            return_rowcount=True,
        )
        return result or 0

    def drop_expired(self) -> int:
        """Remove items that have been in the queue longer than MAX_QUEUE_AGE_HOURS."""
        result = db_execute(
            """
            DELETE FROM news_queue
            WHERE status  = 'pending'
              AND queued_at < NOW() - (%s || ' hours')::interval
            """,
            (MAX_QUEUE_AGE_HOURS,),
            return_rowcount=True,
        )
        return result or 0
