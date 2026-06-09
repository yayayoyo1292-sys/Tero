from __future__ import annotations

import io
import os
import time
import traceback
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, UnidentifiedImageError
from io import BytesIO

from utils.text_filter import sanitize_text
from image.text_formatter import prepare_ar_text, fit_text
from utils.logger import logger


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FONT_CANDIDATES = [
    os.path.join(_BASE_DIR, "Cairo-ExtraBold.ttf"),
    os.path.join(_BASE_DIR, "Cairo-Bold.ttf"),
    os.path.join(_BASE_DIR, "Cairo-Black.ttf"),
]

FONT_PATH: str = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), "")
if not FONT_PATH:
    raise FileNotFoundError(
        "No Cairo font found. Expected one of: " + ", ".join(_FONT_CANDIDATES)
    )

logger.info(f"🔤 Using font: {os.path.basename(FONT_PATH)}")

MAX_FONT_SIZE = 60
MIN_FONT_SIZE = 26
TEXT_COLOR    = (255, 255, 255)


_MAX_RETRIES      = 5     
_RETRY_BASE_DELAY = 1.5   
_DOWNLOAD_TIMEOUT = 20   


_FALLBACK_BOX_COLOR = (30, 30, 50, 255)   # داكن مع لمسة زرقاء — واضح إنه fallback مقصود


_TRANSPARENT_THRESHOLD = 0.50



def _template_has_transparent_image_box(
    template: Image.Image,
    image_box: tuple,
) -> bool:
    
    x1, y1, x2, y2 = image_box
    w, h = template.size
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    if x2 <= x1 or y2 <= y1:
        return False   

    arr = np.array(template.convert("RGBA"))
    alpha_region = arr[y1:y2, x1:x2, 3]
    transparent_fraction = (alpha_region < 10).mean()
    return bool(transparent_fraction > _TRANSPARENT_THRESHOLD)



def _build_image_layer(
    canvas: Image.Image,
    news_img: Optional[Image.Image],
    image_box: tuple,
) -> Image.Image:
   
    x1, y1, x2, y2 = image_box
    box_w = x2 - x1
    box_h = y2 - y1

    if news_img is None:
        fallback = Image.new("RGBA", (box_w, box_h), _FALLBACK_BOX_COLOR)
        canvas.paste(fallback, (x1, y1), fallback)
        return canvas

    img_w, img_h = news_img.size
    img_ratio    = img_w / img_h
    box_ratio    = box_w / box_h

    if img_ratio > box_ratio:
        bg_h, bg_w = box_h, int(box_h * img_ratio)
    else:
        bg_w, bg_h = box_w, int(box_w / img_ratio)

    bg  = news_img.resize((bg_w, bg_h), Image.Resampling.LANCZOS).convert("RGBA")
    bx  = (bg_w - box_w) // 2
    by  = (bg_h - box_h) // 2
    bg  = bg.crop((bx, by, bx + box_w, by + box_h))
    bg  = bg.filter(ImageFilter.GaussianBlur(radius=20))
    overlay = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 80))
    bg  = Image.alpha_composite(bg, overlay)

    if img_ratio > box_ratio:
        fg_w, fg_h = box_w, int(box_w / img_ratio)
    else:
        fg_h, fg_w = box_h, int(box_h * img_ratio)

    fg   = news_img.resize((fg_w, fg_h), Image.Resampling.LANCZOS).convert("RGBA")
    fg_x = (box_w - fg_w) // 2
    fg_y = (box_h - fg_h) // 2

    layer = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    layer.paste(bg, (0, 0))
    layer.paste(fg, (fg_x, fg_y), fg)

    canvas.paste(layer, (x1, y1), layer)
    return canvas


def _draw_title(
    final_img: Image.Image,
    title: str,
    text_box: tuple,
) -> Image.Image:
    x1, y1, x2, y2 = text_box
    box_w = x2 - x1
    box_h = y2 - y1

    draw     = ImageDraw.Draw(final_img)
    title    = sanitize_text(title)
    ar_title = prepare_ar_text(title)

    font, lines, line_height = fit_text(
        draw, ar_title, FONT_PATH,
        box_w, box_h,
        MAX_FONT_SIZE, MIN_FONT_SIZE,
    )

    if not font:
        logger.error("❌ FONT FIT ERROR — title too long for any supported size")
        return final_img

    assert lines is not None and line_height is not None

    lines.reverse()
    total_h = len(lines) * line_height
    y       = y1 + ((box_h - total_h) // 2)

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w    = bbox[2] - bbox[0]
        x    = x1 + ((box_w - w) // 2)


        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))
        draw.text(
            (x, y), line, font=font,
            fill=TEXT_COLOR,stroke_width=1, stroke_fill=TEXT_COLOR
        )
        y += line_height

    return final_img


def _download_news_image(
    session,
    image_url: str,
    news_id: int,
) -> Optional[Image.Image]:
    
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = session.get(image_url, timeout=_DOWNLOAD_TIMEOUT)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "image" not in content_type and len(resp.content) < 1000:
                raise ValueError(
                    f"Response doesn't look like an image "
                    f"(Content-Type={content_type!r}, {len(resp.content)} bytes)"
                )

            img = Image.open(BytesIO(resp.content)).convert("RGBA")

            # تجاهل الصور الصغيرة جداً — غالباً placeholders أو tracking pixels
            if img.size[0] < 50 or img.size[1] < 50:
                raise ValueError(
                    f"Image too small {img.size} — likely a placeholder/tracking pixel"
                )

            logger.info(
                f"✅ Image downloaded | id={news_id} | "
                f"attempt={attempt}/{_MAX_RETRIES} | size={img.size}"
            )
            return img

        except UnidentifiedImageError as exc:
            logger.warning(
                f"⚠️ Corrupt image data | id={news_id} "
                f"attempt={attempt}/{_MAX_RETRIES}: {exc}"
            )
            break

        except Exception as exc:
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                f"⚠️ Image download failed | id={news_id} "
                f"attempt={attempt}/{_MAX_RETRIES}: {type(exc).__name__}: {exc}"
            )
            if attempt < _MAX_RETRIES:
                logger.info(f"   ↳ Retrying in {delay:.1f}s …")
                time.sleep(delay)

    logger.error(
        f"❌ All {_MAX_RETRIES} download attempts failed for id={news_id} "
        f"url={image_url!r} — using fallback background"
    )
    return None



def generate_post_image(
    title: str,
    image_url: Optional[str],
    news_id: int,
    url: str,
    category: str,
    confidence: float,
    content: Optional[str],
    template_config: dict,
    session,
    upload_fn: Callable,
    supabase_storage,
    send_telegram_fn: Optional[Callable] = None,
    send_to_telegram: bool = True,
) -> Optional[str]:
   
    logger.info(
        f"🎨 Generating image | id={news_id} | category={category!r} | "
        f"image_url={'set' if image_url else 'NONE'}"
    )

    try:

        config = (
            template_config.get(category)
            or template_config.get(category.lstrip("ال"))
            or template_config.get("عام")
        )
        if not config:
            logger.error(
                f"❌ No template config for category={category!r} "
                f"and no 'عام' fallback — aborting"
            )
            return None

        image_box     = config["image_box"]
        text_box      = config["text_box"]
        template_path = config.get("template", "")

        if not template_path or not os.path.exists(template_path):
            logger.error(f"❌ Template file not found: {template_path!r}")
            return None

        template = Image.open(template_path).convert("RGBA")

        news_img: Optional[Image.Image] = None

        if image_url:
            news_img = _download_news_image(session, image_url, news_id)
        else:
            logger.info(
                f"ℹ️  No image_url for id={news_id} — using fallback background"
            )

        is_hole_style = _template_has_transparent_image_box(template, image_box)

        logger.debug(
            f"🖼  Template style for id={news_id} | category={category!r} | "
            f"{'HOLE (transparent → image below template)' if is_hole_style else 'FRAME (opaque → image above template)'}"
        )

        if is_hole_style:

            base = Image.new("RGBA", template.size, (0, 0, 0, 255))
            base = _build_image_layer(base, news_img, image_box)
            final_img = Image.alpha_composite(base, template)

        else:

            base = Image.new("RGBA", template.size, (0, 0, 0, 0))
            base.paste(template, (0, 0))
            base = _build_image_layer(base, news_img, image_box)
            final_img = base

        final_img = _draw_title(final_img, title, text_box)

        filename   = f"news_{news_id}.jpg"
        upload_fn(final_img, filename)
        public_url = supabase_storage.from_("generated").get_public_url(filename)

        used_source = "article image" if news_img else "fallback background"
        logger.info(
            f"✅ Image ready | id={news_id} | "
            f"style={'hole' if is_hole_style else 'frame'} | "
            f"source={used_source} | url={public_url}"
        )

        if send_to_telegram and send_telegram_fn:
            buf = io.BytesIO()
            final_img.convert("RGB").save(buf, format="JPEG", quality=85)
            buf.seek(0)
            send_telegram_fn(buf, title, url, category, confidence, content or "")
            logger.info(f"🖼️  Telegram image sent | id={news_id}")

        return public_url

    except Exception as exc:
        logger.error(f"❌ Image generation hard error | id={news_id}: {exc}")
        traceback.print_exc()
        return None
