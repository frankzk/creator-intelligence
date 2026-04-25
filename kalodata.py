"""
Kalodata scraper using Playwright.
Logs in with credentials from .env and extracts top GMV videos for a keyword.
"""

import asyncio
import os
import re

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout


KALODATA_EMAIL = os.getenv("KALODATA_EMAIL", "")
KALODATA_PASSWORD = os.getenv("KALODATA_PASSWORD", "")
KALODATA_BASE = "https://www.kalodata.com"


def _parse_number(text: str) -> float:
    """Parse '1.2M', '450K', '$2,500' → float."""
    if not text:
        return 0.0
    text = re.sub(r"[,$\s€£]", "", text.strip())
    multiplier = 1.0
    if text.upper().endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


async def _login(page: Page) -> None:
    await page.goto(f"{KALODATA_BASE}/login", timeout=30_000)
    await page.wait_for_load_state("networkidle", timeout=15_000)

    # Try common selectors for email/password fields
    for sel in ['input[type="email"]', 'input[name="email"]', 'input[placeholder*="email" i]']:
        if await page.locator(sel).count() > 0:
            await page.fill(sel, KALODATA_EMAIL)
            break

    for sel in ['input[type="password"]', 'input[name="password"]']:
        if await page.locator(sel).count() > 0:
            await page.fill(sel, KALODATA_PASSWORD)
            break

    for sel in ['button[type="submit"]', 'button:has-text("Login")', 'button:has-text("Sign in")', 'button:has-text("Iniciar")']:
        if await page.locator(sel).count() > 0:
            await page.click(sel)
            break

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass


async def _extract_videos(page: Page, max_videos: int = 15) -> list[dict]:
    """Extract video rows from the current Kalodata video list page."""
    await page.wait_for_timeout(3_000)

    videos = []

    # Strategy 1: look for anchor tags with tiktok URLs
    anchors = await page.query_selector_all('a[href*="tiktok.com"]')
    seen_urls = set()

    for anchor in anchors[:max_videos * 3]:
        try:
            href = await anchor.get_attribute("href") or ""
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)

            # Try to find parent row and extract metrics
            row = await anchor.evaluate_handle(
                "el => el.closest('tr') || el.closest('[class*=\"row\"]') || el.closest('[class*=\"item\"]')"
            )

            creator = ""
            views = 0
            gmv = 0.0

            # Look for creator handle in the row
            for handle_sel in [
                '[class*="creator"]', '[class*="author"]', '[class*="username"]',
                'td:nth-child(3)', 'td:nth-child(2)',
            ]:
                try:
                    el = await row.as_element().query_selector(handle_sel)
                    if el:
                        creator = (await el.inner_text()).strip().lstrip("@")
                        break
                except Exception:
                    pass

            # Look for view count
            for views_sel in ['[class*="view"]', 'td:nth-child(4)', 'td:nth-child(5)']:
                try:
                    el = await row.as_element().query_selector(views_sel)
                    if el:
                        views = int(_parse_number(await el.inner_text()))
                        break
                except Exception:
                    pass

            # Look for GMV
            for gmv_sel in ['[class*="gmv"]', '[class*="revenue"]', 'td:nth-child(6)', 'td:nth-child(7)']:
                try:
                    el = await row.as_element().query_selector(gmv_sel)
                    if el:
                        gmv = _parse_number(await el.inner_text())
                        break
                except Exception:
                    pass

            videos.append({
                "tiktok_url": href,
                "creator_handle": creator,
                "views": views,
                "gmv": gmv,
            })

            if len(videos) >= max_videos:
                break

        except Exception:
            continue

    # Strategy 2: scrape table rows directly if no anchors found
    if not videos:
        rows = await page.query_selector_all("table tbody tr")
        for row in rows[:max_videos]:
            try:
                cells = await row.query_selector_all("td")
                if len(cells) < 3:
                    continue

                url_el = await row.query_selector('a[href*="tiktok"]')
                tiktok_url = await url_el.get_attribute("href") if url_el else ""

                texts = [await c.inner_text() for c in cells]

                videos.append({
                    "tiktok_url": tiktok_url,
                    "creator_handle": texts[1].strip() if len(texts) > 1 else "",
                    "views": int(_parse_number(texts[2])) if len(texts) > 2 else 0,
                    "gmv": _parse_number(texts[3]) if len(texts) > 3 else 0.0,
                })
            except Exception:
                continue

    return videos


async def scrape_kalodata(keyword: str, max_videos: int = 15) -> dict:
    """
    Login to Kalodata and scrape top GMV videos for a keyword.
    Returns {'keyword', 'videos': [...], 'total_gmv': float}
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        try:
            if KALODATA_EMAIL and KALODATA_PASSWORD:
                await _login(page)

            # Navigate to video search
            search_url = f"{KALODATA_BASE}/video?keyword={keyword}&sort=gmv&order=desc"
            await page.goto(search_url, timeout=30_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)

            videos = await _extract_videos(page, max_videos=max_videos)

            return {
                "keyword": keyword,
                "videos": videos,
                "total_gmv": sum(v.get("gmv", 0) for v in videos),
                "total_videos": len(videos),
            }

        finally:
            await browser.close()


def scrape_kalodata_sync(keyword: str, max_videos: int = 15) -> dict:
    """Synchronous wrapper for use in FastAPI background tasks."""
    return asyncio.run(scrape_kalodata(keyword, max_videos=max_videos))
