from datetime import datetime

# ── Source ────────────────────────────────────────────────────────────────────
BASE_URL = "https://www.tiroo.net"

# ── Categories to scrape ─────────────────────────────────────────────────────
# All categories share the single رياضة template — no AI classification needed.
# Pagination uses ?page=N  (not /page/N like mnsht)
CATEGORIES: dict[str, dict] = {
    f"{BASE_URL}/category/1":  {"name": "uae_1",         "label": "ستاد الإمارات"},
    f"{BASE_URL}/category/21": {"name": "uae_21",        "label": "الساحة"},
    f"{BASE_URL}/category/22": {"name": "leagues",       "label": "دوريات وبطولات"},
    f"{BASE_URL}/category/7":  {"name": "world_cup",     "label": "كأس العالم"},
    f"{BASE_URL}/category/8":  {"name": "horse_racing",  "label": "خيول"},
    f"{BASE_URL}/category/11": {"name": "camel_racing",  "label": "هجن"},
    f"{BASE_URL}/category/10": {"name": "other_sports",  "label": "رياضات أخرى"},
}

# Single template used for ALL posts
TEMPLATE_KEY = "رياضة"

# ── Scraping ──────────────────────────────────────────────────────────────────
SCRAPE_INTERVAL_SECONDS: int = 30
SCRAPE_TIMEOUT_CONNECT:  int = 8
SCRAPE_TIMEOUT_READ:     int = 25
SCRAPE_MAX_RETRIES:      int = 4
SCRAPE_RETRY_DELAY:      int = 3
# How many pages to fetch per category per cycle (only during warm-up snapshot)
MAX_SCRAPE_PAGES: int = 3
PARALLEL_WORKERS: int = len(CATEGORIES)

# On first run: snapshot existing article URLs (warm-up), then only publish NEW ones.
SCRAPE_ONLY_NEW: bool = True

# ── Publishing ────────────────────────────────────────────────────────────────
# Every post goes to ALL four platforms simultaneously — no priority routing.
ENABLE_FACEBOOK_POSTING: bool = True

# Facebook date window — keep existing window
FACEBOOK_START_DATE = datetime(2026, 5, 20)
FACEBOOK_END_DATE   = datetime(2026, 12, 31)

# Maximum age a queued item can sit before being dropped
MAX_QUEUE_AGE_HOURS: float = 3.0

# ── Rate limits (per platform) ────────────────────────────────────────────────
INSTAGRAM_MIN_INTERVAL_SECONDS: int = 30
TWITTER_MIN_INTERVAL_SECONDS:   int = 15
FACEBOOK_MIN_INTERVAL_SECONDS:  int = 60
TELEGRAM_MIN_INTERVAL_SECONDS:  int = 5

FACEBOOK_MAX_PER_HOUR:  int = 25
TWITTER_MAX_PER_HOUR:   int = 50
INSTAGRAM_MAX_PER_HOUR: int = 20
TELEGRAM_MAX_PER_HOUR:  int = 100

BURST_WINDOW_SECONDS: int = 60
BURST_MAX_INSTANT:    int = 2

# ── Publish log ───────────────────────────────────────────────────────────────
PUBLISH_LOG_RETENTION_DAYS: int = 30
