"""
scraper/save_news.py — Tiroo edition
======================================
Processes each scraped article:
  1. Dedup check (url + title)
  2. Insert into news table
  3. Generate template image (always رياضة template)
  4. Upload image to Supabase Storage
  5. Add to news_queue for publishing to all 4 platforms
  6. Immediately publish (no priority gate — every post goes to all platforms)
"""
from __future__ import annotations

import time
import traceback
import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

from config.settings import TEMPLATE_KEY
from DB.db import db_execute
from services.instant_publisher import instant_publish
from services.queue_manager import QueueManager
from utils.logger import logger

_queue = QueueManager()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def process_article(
    article: dict,
    template_config: dict,
    generate_image_fn: Callable,
) -> None:
    t_start = time.time()

    try:
        # ── 1. Dedup ──────────────────────────────────────────────────────────
        exists = db_execute(
            "SELECT id FROM news WHERE url = %s OR title = %s LIMIT 1",
            (article["url"], article["title"]),
            fetch=True,
        )
        if exists:
            logger.debug(f"⏭️  Duplicate skipped: {article['title'][:60]}")
            return

        # ── 2. Insert news record ─────────────────────────────────────────────
        processed_at = _now()
        result = db_execute(
            """
            INSERT INTO news (
                title, url, image, category, template_key, content,
                source_url, source_name, source_label,
                detected_at, scraped_at, inserted_at, processed_at
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            ON CONFLICT (url) DO NOTHING
            RETURNING id
            """,
            (
                article["title"],
                article["url"],
                article.get("image"),
                TEMPLATE_KEY,       # always 'رياضة'
                TEMPLATE_KEY,
                article.get("content"),
                article.get("source_url"),
                article.get("source_name"),
                article.get("source_label"),
                article.get("detected_at"),
                article.get("scraped_at"),
                processed_at,
            ),
            fetch=True,
        )

        if not result:
            logger.warning(f"⏭️  ON CONFLICT skip: {article['title'][:60]}")
            return

        news_id = result["id"] if isinstance(result, dict) else result[0]
        if not news_id:
            logger.error("❌ No news_id from DB insert")
            return

        logger.info(
            f"🟢 [{article.get('source_label','?')}] Saved"
            f" | id={news_id} | {article['title'][:60]}"
        )

        # ── 3. Generate image ─────────────────────────────────────────────────
        image_url = generate_image_fn(
            article["title"],
            article.get("image"),
            news_id,
            article["url"],
            TEMPLATE_KEY,
            article.get("content"),
        )

        if not image_url:
            logger.warning(
                f"⚠️  Image gen returned None for id={news_id} — "
                f"continuing without generated image"
            )

        # ── 4. Enqueue ────────────────────────────────────────────────────────
        created_ts = time.time()
        queued_at  = _now()

        _queue.add_to_queue({
            "article_id":   news_id,
            "title":        article["title"],
            "url":          article["url"],
            "content":      article.get("content"),
            "image_url":    image_url,
            "created_at":   created_ts,
            "detected_at":  article.get("detected_at"),
            "scraped_at":   article.get("scraped_at"),
            "queued_at":    queued_at,
            "category":     TEMPLATE_KEY,
            "template_key": TEMPLATE_KEY,
            "source_label": article.get("source_label"),
        })

        # Update image URL on queue row
        db_execute(
            """
            UPDATE news_queue
            SET image_url       = %s,
                generated_image = %s,
                queued_at       = NOW()
            WHERE article_id = %s
            """,
            (image_url, f"news_{news_id}.jpg", news_id),
        )

        db_execute(
            "UPDATE news SET queued_at = NOW() WHERE id = %s",
            (news_id,),
        )

        # ── 5. Fetch the queue row so instant_publish has the id ──────────────
        id_row = db_execute(
            "SELECT id FROM news_queue WHERE article_id = %s LIMIT 1",
            (news_id,),
            fetch=True,
        )
        if not id_row:
            logger.error(f"❌ Queue row not found for article_id={news_id}")
            return

        queue_id = id_row["id"] if isinstance(id_row, dict) else id_row[0]

        t_total = (time.time() - t_start) * 1000
        logger.info(
            f"⚡ Processed in {t_total:.0f}ms"
            f" | id={news_id}"
            f" | image={'✅' if image_url else '❌'}"
        )

        # ── 6. Immediate publish to all platforms ─────────────────────────────
        queue_row: dict = {
            "id":           queue_id,
            "article_id":   news_id,
            "title":        article["title"],
            "url":          article["url"],
            "content":      article.get("content"),
            "image_url":    image_url,
            "created_at":   created_ts,
            "priority_score": 0,            # no priority in tiroo
            "category":     TEMPLATE_KEY,
            "template_key": TEMPLATE_KEY,
            "source_label": article.get("source_label"),
            "status":       "pending",
        }
        instant_publish(queue_row)

    except Exception as exc:
        logger.error(f"❌ process_article error: {exc}")
        traceback.print_exc()


async def article_consumer(
    in_queue: asyncio.Queue,
    template_config: dict,
    generate_image_fn: Callable,
) -> None:
    loop = asyncio.get_event_loop()
    logger.info("📦 Article consumer started")

    while True:
        try:
            article = await asyncio.wait_for(in_queue.get(), timeout=5.0)
            await loop.run_in_executor(
                None,
                process_article,
                article, template_config, generate_image_fn,
            )
            in_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            logger.error(f"❌ Consumer error: {exc}", exc_info=True)
            await asyncio.sleep(1)
