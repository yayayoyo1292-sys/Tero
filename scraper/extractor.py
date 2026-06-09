"""
scraper/extractor.py — Tiroo edition
=====================================
tiroo.net category pages render articles as a plain link list, NOT item-cards.
Structure observed:

  Category page (e.g. /category/1):
    Each article is a plain <a href="/1360">Title text</a> with a date span.
    Pagination: ?page=2 (query-param, not /page/2 path-segment).

  Article page (e.g. /1360):
    - og:image  meta tag  → primary image source
    - article body in     <div class="paragraph-list">  (same as mnsht)

Warm-up behaviour (SCRAPE_ONLY_NEW=True):
  First cycle: snapshot ALL current article URLs from category pages (no DB write,
  no publishing) so we only ever publish articles that appear AFTER first run.
  Exception: the SINGLE most-recent article on the first cycle IS published if it
  is not already in the database — giving you one immediate post on startup.
"""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qs

import aiohttp
from bs4 import BeautifulSoup

from config.settings import (
    BASE_URL,
    CATEGORIES,
    SCRAPE_INTERVAL_SECONDS,
    SCRAPE_ONLY_NEW,
    SCRAPE_TIMEOUT_CONNECT,
    SCRAPE_TIMEOUT_READ,
    SCRAPE_MAX_RETRIES,
    SCRAPE_RETRY_DELAY,
    MAX_SCRAPE_PAGES,
)
from DB.db import db_execute
from utils.logger import logger

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en;q=0.9",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_text(text) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def clean_image_url(src: Optional[str]) -> Optional[str]:
    """Normalise Tiroo image URLs — fix UploadCache → Upload path."""
    if not src:
        return None
    full_url = urljoin(BASE_URL, src)
    full_url = full_url.replace("/UploadCache/libfiles/", "/Upload/libfiles/")
    full_url = full_url.replace("/UploadCache/files/",    "/Upload/files/")
    full_url = re.sub(r"/\d+x\d+o?/", "/", full_url)
    return full_url


def now_ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_page_url(category_url: str, page: int) -> str:
    """Build paginated URL using ?page=N query param (tiroo style)."""
    if page <= 1:
        return category_url
    parsed = urlparse(category_url)
    return urlunparse(parsed._replace(query=f"page={page}"))


def article_exists(url: str, title: Optional[str] = None) -> bool:
    if title:
        row = db_execute(
            "SELECT id FROM news WHERE url = %s OR title = %s LIMIT 1",
            (url, title), fetch=True,
        )
    else:
        row = db_execute(
            "SELECT id FROM news WHERE url = %s LIMIT 1",
            (url,), fetch=True,
        )
    return bool(row)


def update_scraper_health(
    category_url: str,
    category_name: str,
    *,
    success: bool,
    articles_found: int = 0,
) -> None:
    if success:
        db_execute(
            """
            INSERT INTO scraper_health
                (category_url, category_name, last_checked, last_success,
                 consecutive_failures, articles_found, status)
            VALUES (%s, %s, NOW(), NOW(), 0, %s, 'ok')
            ON CONFLICT (category_url) DO UPDATE SET
                last_checked         = NOW(),
                last_success         = NOW(),
                consecutive_failures = 0,
                articles_found       = scraper_health.articles_found + EXCLUDED.articles_found,
                status               = 'ok'
            """,
            (category_url, category_name, articles_found),
        )
    else:
        db_execute(
            """
            INSERT INTO scraper_health
                (category_url, category_name, last_checked, last_failure,
                 consecutive_failures, status)
            VALUES (%s, %s, NOW(), NOW(), 1, 'degraded')
            ON CONFLICT (category_url) DO UPDATE SET
                last_checked         = NOW(),
                last_failure         = NOW(),
                consecutive_failures = scraper_health.consecutive_failures + 1,
                status = CASE
                    WHEN scraper_health.consecutive_failures + 1 >= 5 THEN 'down'
                    ELSE 'degraded'
                END
            """,
            (category_url, category_name),
        )


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _fetch_html(
    session: aiohttp.ClientSession,
    url: str,
    *,
    retries: int = SCRAPE_MAX_RETRIES,
) -> Optional[str]:
    timeout = aiohttp.ClientTimeout(
        connect=SCRAPE_TIMEOUT_CONNECT,
        total=SCRAPE_TIMEOUT_READ,
    )
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, timeout=timeout) as resp:
                resp.raise_for_status()
                return await resp.text(encoding="utf-8", errors="replace")
        except Exception as exc:
            last_exc = exc
            msg = repr(exc) if not str(exc).strip() else str(exc)
            if attempt < retries:
                wait = SCRAPE_RETRY_DELAY * attempt
                logger.warning(
                    f"⚠️ fetch attempt {attempt}/{retries} failed for {url}: "
                    f"{msg} — retrying in {wait}s"
                )
                await asyncio.sleep(wait)
    msg = repr(last_exc) if not str(last_exc).strip() else str(last_exc)
    logger.error(f"❌ All {retries} fetch attempts failed for {url}: {msg}")
    return None


async def _fetch_article_og_image(
    session: aiohttp.ClientSession,
    url: str,
) -> Optional[str]:
    """Fetch og:image from article page — primary image source for tiroo."""
    html = await _fetch_html(session, url)
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        og   = soup.find("meta", property="og:image")
        if og:
            src = og.get("content")
            if src and not str(src).startswith("data:"):
                return clean_image_url(str(src))
        # Fallback: first img inside article body
        for img in soup.select("div.paragraph-list img, article img, .news-content img"):
            _s = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
            if _s and not str(_s).startswith("data:"):
                return clean_image_url(str(_s))
    except Exception as exc:
        logger.warning(f"⚠️ OG image parse error for {url}: {exc}")
    return None


async def _fetch_article_content(
    session: aiohttp.ClientSession,
    url: str,
    max_words: int = 150,
) -> Optional[str]:
    """Extract article body text — uses div.paragraph-list (same selector as mnsht)."""
    html = await _fetch_html(session, url)
    if not html:
        return None
    try:
        soup       = BeautifulSoup(html, "lxml")
        tag        = soup.select_one("div.paragraph-list")
        if not tag:
            # Fallback: grab all <p> in article area
            tag = soup.select_one("article, .news-content, .article-body")
        if not tag:
            return None
        paragraphs = tag.find_all("p")
        content    = " ".join(p.get_text(strip=True) for p in paragraphs)
        words      = content.split()
        chunk      = " ".join(words[:max_words])
        # Cut at first sentence boundary within the chunk
        cutoff = " ".join(words[:30])
        rest   = chunk[len(cutoff):]
        dot_idx = -1
        for sep in (".", ",", "؟", "!", "…"):
            idx = rest.find(sep)
            if idx != -1 and (dot_idx == -1 or idx < dot_idx):
                dot_idx = idx
        content = cutoff + rest[: dot_idx + 1] if dot_idx != -1 else chunk
        return clean_text(content) or None
    except Exception as exc:
        logger.warning(f"⚠️ Content parse error for {url}: {exc}")
    return None


# ── Category page parser ──────────────────────────────────────────────────────

def _parse_article_links(html: str, category_url: str) -> list[dict]:
    """
    Parse tiroo category page.

    tiroo renders articles as plain anchor links:
      <a href="/1360">Article title  DD/MM/YYYY HH:MM</a>

    We extract the href (numeric article ID path) and the title text,
    stripping the trailing date if present.
    """
    soup  = BeautifulSoup(html, "lxml")
    items = []
    seen  : set[str] = set()

    # Primary selector: all <a> tags whose href matches /NNN (numeric article ID)
    _article_href_re = re.compile(r"^/\d+$")

    for a_tag in soup.find_all("a", href=_article_href_re):
        try:
            href = str(a_tag.get("href", ""))
            url  = urljoin(BASE_URL, href)
            if url in seen:
                continue
            seen.add(url)

            raw_text = a_tag.get_text(separator=" ", strip=True)
            # Strip trailing date pattern  "DD/MM/YYYY HH:MM ص|م|AM|PM"
            title = re.sub(
                r"\s+\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}.*$",
                "",
                raw_text,
            ).strip()
            title = clean_text(title) or "بدون عنوان"

            if len(title) < 5:
                continue  # skip nav/footer links

            items.append({"url": url, "title": title})
        except Exception as exc:
            logger.error(f"❌ Link parse error ({category_url}): {exc}")

    return items


# ── Category worker ───────────────────────────────────────────────────────────

async def category_worker(
    category_url: str,
    out_queue: asyncio.Queue,
    session: aiohttp.ClientSession,
) -> None:
    cfg       = CATEGORIES[category_url]
    cat_name  = cfg["name"]
    cat_label = cfg["label"]

    _seen_urls: set[str] = set()
    _is_warmup: bool     = SCRAPE_ONLY_NEW
    _warmup_done: bool   = False

    if _is_warmup:
        logger.info(
            f"🔍 Warm-up mode: [{cat_label}] — "
            f"first cycle snapshots existing URLs + publishes the latest one"
        )
    else:
        logger.info(f"🚀 Worker started (process-all mode): [{cat_label}]")

    while True:
        loop_start  = time.monotonic()
        detected_at = now_ts()

        try:
            html = await _fetch_html(session, category_url)
            if html is None:
                update_scraper_health(category_url, cat_name, success=False)
                await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)
                continue

            raw_items = _parse_article_links(html, category_url)

            # Fetch additional pages during warm-up to build a full snapshot
            if _is_warmup and not _warmup_done:
                for page in range(2, MAX_SCRAPE_PAGES + 1):
                    page_url  = _make_page_url(category_url, page)
                    page_html = await _fetch_html(session, page_url)
                    if page_html:
                        raw_items += _parse_article_links(page_html, category_url)

            if _is_warmup and not _warmup_done:
                # ── Warm-up: snapshot ALL existing URLs — publish NOTHING ────
                # Only articles that appear AFTER this run will be published.
                for item in raw_items:
                    _seen_urls.add(item["url"])

                _warmup_done = True
                _is_warmup   = False

                logger.info(
                    f"✅ Warm-up done: [{cat_label}] — "
                    f"{len(_seen_urls)} existing URLs snapshotted — watching for NEW articles"
                )

                try:
                    update_scraper_health(category_url, cat_name, success=True, articles_found=0)
                except Exception:
                    pass
                elapsed   = time.monotonic() - loop_start
                sleep_for = max(0.0, SCRAPE_INTERVAL_SECONDS - elapsed)
                await asyncio.sleep(sleep_for)
                continue

            # ── Normal cycle: look for NEW articles ──────────────────────────
            new_count = 0
            for item in raw_items:
                url = item["url"]

                if url in _seen_urls:
                    continue

                if article_exists(url, item["title"]):
                    _seen_urls.add(url)
                    continue

                _seen_urls.add(url)
                await _enqueue_article(
                    item, category_url, cat_name, cat_label,
                    detected_at, out_queue, session,
                )
                new_count += 1

            try:
                update_scraper_health(
                    category_url, cat_name, success=True, articles_found=new_count
                )
            except Exception:
                pass

        except Exception as exc:
            logger.error(f"❌ Worker error [{cat_label}]: {exc}", exc_info=True)
            try:
                update_scraper_health(category_url, cat_name, success=False)
            except Exception:
                pass  # DB unavailable — don't crash the worker

        elapsed   = time.monotonic() - loop_start
        sleep_for = max(0.0, SCRAPE_INTERVAL_SECONDS - elapsed)
        await asyncio.sleep(sleep_for)


async def _enqueue_article(
    item: dict,
    category_url: str,
    cat_name: str,
    cat_label: str,
    detected_at: datetime,
    out_queue: asyncio.Queue,
    session: aiohttp.ClientSession,
) -> None:
    """Fetch full article details and put into the processing queue."""
    url        = item["url"]
    scraped_at = now_ts()

    content       = await _fetch_article_content(session, url)
    article_image = await _fetch_article_og_image(session, url)

    if article_image:
        logger.debug(f"🖼️  og:image used | {item['title'][:60]}")
    else:
        logger.warning(f"⚠️ No image found | {item['title'][:60]}")

    article = {
        "title":        item["title"],
        "url":          url,
        "image":        article_image,
        "content":      content,
        "source_url":   category_url,
        "source_name":  cat_name,
        "source_label": cat_label,
        "detected_at":  detected_at,
        "scraped_at":   scraped_at,
    }
    await out_queue.put(article)
    logger.info(
        f"📥 [{cat_label}] New: {item['title'][:70]} "
        f"| scraped_at={scraped_at.strftime('%H:%M:%S')}"
    )