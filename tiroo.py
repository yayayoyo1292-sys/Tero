"""
tiroo.py — Main entry point
============================
Starts:
  • One async category scraper worker per configured category (7 total)
  • One article consumer per worker (parallel image gen + publishing)
  • One background publishing/retry scheduler thread

Publishing rule: every scraped article → Telegram + Instagram + Facebook + Twitter
No priority engine, no AI classification, single رياضة template.
"""
from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Thread

from dotenv import load_dotenv

# ── Load .env FIRST — before any other import that reads os.environ ───────────
load_dotenv()

# ── Env validation — fail fast with a clear message ──────────────────────────
_PLACEHOLDER = {"[PROJECT-REF]", "[PASSWORD]", "[SERVICE_ROLE_KEY]"}

def _is_placeholder(val: str) -> bool:
    return not val or any(p in val for p in _PLACEHOLDER)

_required = {
    "DATABASE_URL":                  "Supabase → Settings → Database → URI",
    "SUPABASE_URL":                  "Supabase → Settings → API → Project URL",
    "SUPABASE_KEY":                  "Supabase → Settings → API → service_role key",
    "PRIORITY_TELEGRAM_BOT_TOKEN":   "BotFather token",
    "PRIORITY_TELEGRAM_CHAT_ID":     "Channel/group ID (e.g. -100XXXXXXXXXX)",
}

_bad: list[str] = []
for _k, _hint in _required.items():
    _v = os.getenv(_k, "")
    if _is_placeholder(_v):
        _bad.append(f"  • {_k:<40} ← {_hint}")

if _bad:
    print("\n❌  Cannot start — fill in these values in your .env file:\n")
    print("\n".join(_bad))
    print("\nSee .env.example for the full template.\n")
    sys.exit(1)

# ── All env vars present — now safe to import modules that need them ──────────
import aiohttp
import requests as _requests
from supabase import create_client

from config.settings import CATEGORIES, PARALLEL_WORKERS, TEMPLATE_KEY
from DB.cloud_storage import upload_image
from image.composer import generate_post_image
from scraper.extractor import category_worker, HEADERS as _SCRAPER_HEADERS
from scraper.save_news import process_article
from services.scheduler import publishing_worker
from utils.logger import logger

# ── Supabase client ───────────────────────────────────────────────────────────
supabase = create_client(
    str(os.getenv("SUPABASE_URL")),
    str(os.getenv("SUPABASE_KEY")),
)

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(BASE_DIR, "templates")

# ── Single template config ────────────────────────────────────────────────────
TEMPLATE_CONFIG = {
    TEMPLATE_KEY: {
        "template":  os.path.join(TEMPLATES, "رياضة.png"),
        "image_box": (0,   0,   1080, 835),
        "text_box":  (20,  940, 1060, 1200),
        "align":     "center",
    },
}

# ── Sync HTTP session for image downloads ─────────────────────────────────────
_sync_session = _requests.Session()
_sync_session.headers.update(_SCRAPER_HEADERS)


def _generate_image(
    title: str,
    image_url: str | None,
    news_id: int,
    url: str,
    template_key: str,
    content: str | None,
) -> str | None:
    return generate_post_image(
        title=title,
        image_url=image_url,
        news_id=news_id,
        url=url,
        category=template_key,
        confidence=1.0,
        content=content,
        template_config=TEMPLATE_CONFIG,
        session=_sync_session,
        upload_fn=upload_image,
        supabase_storage=supabase.storage,
        send_telegram_fn=None,
        send_to_telegram=False,
    )


# ── Async article consumer ────────────────────────────────────────────────────

async def article_consumer(
    queue: asyncio.Queue,
    executor: ThreadPoolExecutor,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        item = await queue.get()
        try:
            await loop.run_in_executor(
                executor,
                partial(process_article, item, TEMPLATE_CONFIG, _generate_image),
            )
        except Exception as exc:
            logger.error(f"❌ Consumer error: {exc}", exc_info=True)
        finally:
            queue.task_done()


# ── Main async loop ───────────────────────────────────────────────────────────

async def main() -> None:
    article_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    executor = ThreadPoolExecutor(max_workers=PARALLEL_WORKERS + 4)

    connector = aiohttp.TCPConnector(
        limit=50,
        limit_per_host=5,
        ttl_dns_cache=300,
        force_close=False,
    )
    async with aiohttp.ClientSession(
        connector=connector,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "ar,en;q=0.9",
        },
    ) as session:

        worker_tasks = [
            asyncio.create_task(
                category_worker(url, article_queue, session),
                name=f"worker-{cfg['name']}",
            )
            for url, cfg in CATEGORIES.items()
        ]

        consumer_tasks = [
            asyncio.create_task(
                article_consumer(article_queue, executor),
                name=f"consumer-{i}",
            )
            for i in range(min(PARALLEL_WORKERS, 8))
        ]

        logger.info(
            f"🚀 Tiroo scraper started | "
            f"{len(worker_tasks)} category workers | "
            f"{len(consumer_tasks)} article consumers | "
            f"template={TEMPLATE_KEY}"
        )

        await asyncio.gather(*worker_tasks, *consumer_tasks, return_exceptions=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    Thread(target=publishing_worker, daemon=True, name="publishing-worker").start()
    logger.info("🧵 Background publishing worker started")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Stopped manually")
    except Exception as exc:
        logger.error(f"CRASH: {exc}", exc_info=True)
        raise