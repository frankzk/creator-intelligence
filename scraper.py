import asyncio
import json
import os
import re
import sys

import yt_dlp
from playwright.async_api import async_playwright


def extract_username_from_url(url: str) -> str:
    url = url.rstrip("/")
    part = url.split("/")[-1]
    return part.lstrip("@")


async def _scrape_profile(url: str, max_videos: int = 25) -> list[dict]:
    captured: list[dict] = []
    seen: set[str] = set()

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
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await ctx.new_page()

        async def handle_response(resp):
            try:
                if not any(k in resp.url for k in ("item_list", "user/post", "aweme/post", "post/item")):
                    return
                body = await resp.text()
                data = json.loads(body)
                items = (
                    data.get("itemList")
                    or data.get("aweme_list")
                    or data.get("items")
                    or []
                )
                for item in items:
                    vid_id = str(item.get("id") or item.get("aweme_id") or "")
                    if not vid_id or vid_id in seen:
                        continue
                    seen.add(vid_id)
                    stats = item.get("stats") or item.get("statistics") or {}
                    author = item.get("author") or {}
                    handle = author.get("uniqueId") or author.get("unique_id") or ""
                    duration = (item.get("video") or {}).get("duration") or 0
                    captured.append({
                        "tiktok_id": vid_id,
                        "title": item.get("desc") or item.get("description") or "",
                        "views": int(stats.get("playCount") or stats.get("play_count") or 0),
                        "likes": int(stats.get("diggCount") or stats.get("digg_count") or 0),
                        "comments": int(stats.get("commentCount") or stats.get("comment_count") or 0),
                        "shares": int(stats.get("shareCount") or stats.get("share_count") or 0),
                        "duration": int(duration),
                        "url": f"https://www.tiktok.com/@{handle}/video/{vid_id}",
                    })
            except Exception:
                pass

        page.on("response", handle_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3_000)

        for _ in range(8):
            if len(captured) >= max_videos:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2_000)

        if not captured:
            captured = await _dom_fallback(page, url, max_videos)

        await browser.close()

    return captured[:max_videos]


async def _dom_fallback(page, url: str, max_videos: int) -> list[dict]:
    """Extract video URLs from DOM when API interception yields nothing."""
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
    """Run an async coroutine safely on Windows using ProactorEventLoop."""
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
        "ignoreerrors": True,
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
