"""Bounded product thumbnail collection for the after-sale workbook."""
import io
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageOps

from .procurement_image_fetch import MAX_IMAGE_BYTES, fetch_procurement_image, trusted_procurement_image_url

MAX_UNIQUE_IMAGES = 500
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
DOWNLOAD_START_WINDOW = 15
MAX_IMAGE_PIXELS = 4_000_000
THUMB_SIZE = (72, 96)
_DECODE_SLOT = threading.BoundedSemaphore(1)


def product_image_urls(row):
    """Keep all distinct supplied images, including legacy single-image rows."""
    candidates = list(row.get('goodsImages') or [])
    candidates.extend(item.get('goodsImg') for item in row.get('goodsItems') or [] if isinstance(item, dict))
    if row.get('goodsImg'):
        candidates.append(row['goodsImg'])
    return list(dict.fromkeys(str(value).strip() for value in candidates if value))


def _trusted_url(value):
    return trusted_procurement_image_url('https:'+value if value.startswith('//') else value)


def collect_thumbnails(rows, image_fetcher=None):
    """No credentials; reuse the existing allowlist and checked redirects."""
    unique = {}
    for row in rows:
        for url in product_image_urls(row):
            unique.setdefault(url, None)
            if len(unique) >= MAX_UNIQUE_IMAGES:
                break
        if len(unique) >= MAX_UNIQUE_IMAGES:
            break
    urls = list(unique)
    deadline = time.monotonic() + DOWNLOAD_START_WINDOW
    spent = 0
    inflight = 0
    condition = threading.Condition()

    def read(url):
        nonlocal spent, inflight
        trusted = _trusted_url(url)
        if not trusted:
            return url, None
        reservation = MAX_IMAGE_BYTES + 1
        with condition:
            while spent + reservation > MAX_DOWNLOAD_BYTES and inflight and time.monotonic() < deadline:
                condition.wait(timeout=max(.001, deadline-time.monotonic()))
            if spent + reservation > MAX_DOWNLOAD_BYTES or time.monotonic() >= deadline:
                return url, None
            spent += reservation
            inflight += 1
        consumed = reservation
        try:
            raw = image_fetcher(trusted) if image_fetcher else fetch_procurement_image(trusted, deadline=deadline)
            if not isinstance(raw, bytes) or len(raw) > 5 * 1024 * 1024:
                return url, None
            consumed = len(raw)
            with _DECODE_SLOT, Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    return url, None
                source.draft('RGB', THUMB_SIZE)
                thumb = ImageOps.exif_transpose(source)
                thumb.thumbnail(THUMB_SIZE)
                thumb = thumb.convert('RGBA')
                canvas = Image.new('RGB', THUMB_SIZE, 'white')
                canvas.paste(thumb, ((72-thumb.width)//2, (96-thumb.height)//2), thumb)
                output = io.BytesIO()
                canvas.save(output, format='JPEG', quality=85)
                return url, output.getvalue()
        except Exception:
            # A missing/corrupt/oversized picture cannot discard order data.
            return url, None
        finally:
            with condition:
                spent -= reservation - consumed
                inflight -= 1
                condition.notify_all()

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(read, urls))


def product_image_grid(urls, thumbnails):
    """One in-cell image containing every product, without extra order rows."""
    if not urls or not any(thumbnails.get(url) for url in urls):
        return None
    columns = min(3, len(urls))
    canvas = Image.new('RGB', (columns*76+4, math.ceil(len(urls)/columns)*100+4), 'white')
    draw = ImageDraw.Draw(canvas)
    for index, url in enumerate(urls):
        x, y = (index % columns)*76+4, (index // columns)*100+4
        data = thumbnails.get(url)
        if data:
            with Image.open(io.BytesIO(data)) as thumb:
                canvas.paste(thumb, (x, y))
        else:
            draw.rectangle((x,y,x+71,y+95), fill='#f2f5f8', outline='#d8e2ea')
            draw.text((x+5,y+40), '#%d N/A' % (index+1), fill='#65758a')
    output = io.BytesIO()
    canvas.save(output, format='JPEG', quality=85)
    return output.getvalue(), min(409, canvas.height * .75)
