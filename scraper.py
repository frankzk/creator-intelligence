import asyncio
import json
import os
import re
import sys

import yt_dlp
from playwright.async_api import async_playwright


_FFMPEG_HINTS = [
    r"C:\Program Files\DICloak\vendor\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ffmpeg\bin",
]


def extract_username_from_url(url: str) -> str:
    url = url.rstrip("/")
    part = url.split("/")[-1]
    return part.lstrip("@")


def _find_ffmpeg() -> str | None:
    """Return ffmpeg bin directory if found outside PATH."""
    import shutil
    if shutil.which("ffmpeg"):
        return None  # Already in PATH
    for hint in _FFMPEG_HINTS:
        exe = os.path.join(hint, "ffmpeg.exe")
        if os.path.exists(exe):
            return hint
    return None


def _parse_item(item: dict, fallback_handle: str = "") -> dict | None:
    vid_id = str(
        item.get("id") or item.get("aweme_id") or item.get("video_id") or ""
    )
    if not vid_id or not vid_id.isdigit():
        return None

    stats = item.get("stats") or item.get("statistics") or item.get("statsV2") or {}
    author = item.get("author") or item.get("authorInfo") or {}
    handle = author.get("uniqueId") or author.get("unique_id") or fallback_handle
    duration = (item.get("video") or {}).get("duration") or item.get("duration") or 0

    def _int(v):
        try:
            return int(str(v).replace(",", ""))
        except Exception:
            return 0

    return {
        "tiktok_id": vid_id,
        "title": item.get("desc") or item.get("description") or "",
        "views": _int(stats.get("playCount") or stats.get("play_count") or 0),
        "likes": _int(stats.get("diggCount") or stats.get("digg_count") or 0),
        "comments": _int(stats.get("commentCount") or stats.get("comment_count") or 0),
        "shares": _int(stats.get("shareCount") or stats.get("share_count") or 0),
        "duration": int(duration),
        "url": f"https://www.tiktok.com/@{handle}/video/{vid_id}",
    }


async def _extract_sigi_state(page, username: str) -> list[dict]:
    """Extract video data from TikTok's SIGI_STATE or __NEXT_DATA__ embedded JSON."""
    results = []
    try:
        data = await page.evaluate("""
            () => {
                // Try SIGI_STATE script tag (current TikTok format)
                const scripts = Array.from(document.querySelectorAll('script'));
                for (const s of scripts) {
                    const t = s.textContent || '';
                    if (t.includes('ItemModule') || t.includes('SIGI_STATE')) {
                        try {
                            const m = t.match(/window\\[['\"](SIGI_STATE|__NEXT_DATA__)['\"]]\\s*=\\s*(\\{.+\\})/s);
                            if (m) return {source: 'sigi', data: JSON.parse(m[2])};
                            const start = t.indexOf('{');
                            if (start >= 0) {
                                const obj = JSON.parse(t.slice(start));
                                if (obj && obj.ItemModule) return {source: 'sigi', data: obj};
                            }
                        } catch(e) {}
                    }
                }
                // Try __NEXT_DATA__ tag
                const nd = document.querySelector('#__NEXT_DATA__');
                if (nd) {
                    try { return {source: 'next', data: JSON.parse(nd.textContent)}; } catch(e) {}
                }
                // Try SIGI_STATE script id
                const sg = document.querySelector('#SIGI_STATE');
                if (sg) {
                    try { return {source: 'sigi', data: JSON.parse(sg.textContent)}; } catch(e) {}
                }
                return null;
            }
        """)

        if not data:
            return []

        source = data.get("source", "")
        d = data.get("data", {})

        if source == "sigi":
            # ItemModule is a dict of {video_id: video_data}
            item_module = d.get("ItemModule", {})
            for _vid_id, item in item_module.items():
                parsed = _parse_item(item, username)
                if parsed:
                    results.append(parsed)

        elif source == "next":
            page_props = (d.get("props") or {}).get("pageProps") or {}
            items = page_props.get("items") or page_props.get("videoList") or []
            for item in items:
                parsed = _parse_item(item, username)
                if parsed:
                    results.append(parsed)

    except Exception as e:
        print(f"[scraper] embedded data extraction error: {e}")

    return results


async def _scrape_profile(url: str, max_videos: int = 25) -> list[dict]:
    captured: list[dict] = []
    seen: set[str] = set()
    username = extract_username_from_url(url)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124"',
                "sec-ch-ua-platform": '"Windows"',
            },
        )
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            window.chrome = {runtime: {}};
        """)
        page = await ctx.new_page()

        async def handle_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = await resp.text()
                if len(body) < 200:
                    return
                data = json.loads(body)

                def find_items(d, depth=0):
                    if depth > 4 or not isinstance(d, dict):
                        return []
                    for v in d.values():
                        if isinstance(v, list) and v and isinstance(v[0], dict):
                            if any(k in v[0] for k in ("id", "aweme_id", "video_id")):
                                return v
                        elif isinstance(v, dict):
                            found = find_items(v, depth + 1)
                            if found:
                                return found
                    return []

                for item in find_items(data):
                    parsed = _parse_item(item, username)
                    if parsed and parsed["tiktok_id"] not in seen:
                        seen.add(parsed["tiktok_id"])
                        captured.append(parsed)
            except Exception:
                pass

        page.on("response", handle_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(4_000)

        for _ in range(10):
            if len(captured) >= max_videos:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2_500)

        if not captured:
            print(f"[scraper] API interception: 0 results — trying SIGI_STATE...")
            captured = await _extract_sigi_state(page, username)
            for v in captured:
                seen.add(v["tiktok_id"])

        if not captured:
            print(f"[scraper] SIGI_STATE: 0 results — trying DOM fallback...")
            captured = await _dom_fallback(page, url, max_videos)

        await browser.close()
        print(f"[scraper] total captured: {len(captured)} videos for @{username}")

    return captured[:max_videos]


async def _dom_fallback(page, url: str, max_videos: int) -> list[dict]:
    username = url.rstrip("/").split("/")[-1].lstrip("@")
    links = await page.eval_on_selector_all(
        'a[href*="/video/"]',
        "els => els.map(e => e.href)",
    )
    results = []
    seen = set()
    for link in links:
        m = re.search(r"/video/(\d+)", link)
        if not m:
            continue
        vid_id = m.group(1)
        if vid_id in seen:
            continue
        seen.add(vid_id)
        results.append({
            "tiktok_id": vid_id,
            "title": "",
            "views": 0,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "duration": 0,
            "url": f"https://www.tiktok.com/@{username}/video/{vid_id}",
        })
        if len(results) >= max_videos:
            break
    return results


def _run_async(coro):
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return asyncio.run(coro)


def scrape_profile_metadata(url: str, max_videos: int = 25) -> list[dict]:
    return _run_async(_scrape_profile(url, max_videos))


def scrape_profile_full(url: str, max_videos: int = 25) -> list[dict]:
    return _run_async(_scrape_profile(url, max_videos))


def download_video_audio(video_url: str, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    outtmpl = os.path.join(output_dir, "%(id)s.%(ext)s")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "96",
        }],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }

    ffmpeg_dir = _find_ffmpeg()
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    if not info:
        raise RuntimeError(f"Could not download audio from {video_url}")

    video_id = info.get("id", "")
    mp3_path = os.path.join(output_dir, f"{video_id}.mp3")

    if not os.path.exists(mp3_path):
        for f in os.listdir(output_dir):
            if f.startswith(video_id):
                return os.path.join(output_dir, f)
        raise FileNotFoundError(f"Audio file not found for video {video_id}")

    return mp3_path
