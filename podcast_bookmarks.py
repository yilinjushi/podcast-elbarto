"""Scan X bookmarks and queue eligible source snapshots for podcast production."""

import argparse
import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from podcast_sources import enqueue_source
from x_extract import extract_content_from_page, wait_for_content


BOOKMARKS_URL = "https://x.com/i/bookmarks"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"]


def get_bookmark_urls(page, target_count):
    seen = []
    page.goto(BOOKMARKS_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
    except Exception:
        pass
    time.sleep(3)
    for _ in range(int(os.getenv("MAX_SCROLLS", "40"))):
        for url in page.eval_on_selector_all("a[href*='/status/']", "els => [...new Set(els.map(e => e.href))]"):
            if re.fullmatch(r"https://x\.com/[^/]+/status/\d+", url) and url not in seen:
                seen.append(url)
        if len(seen) >= target_count:
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
    return seen[:target_count]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=int(os.getenv("TARGET_COUNT", "15")))
    args = parser.parse_args()
    session = Path(os.getenv("X_SESSION_FILE", "x_session.json"))
    if not session.is_file():
        raise SystemExit("X session file is missing")
    queued = existing = failures = 0
    with sync_playwright() as playwright:
        options = {"headless": True, "args": LAUNCH_ARGS}
        if os.getenv("CHROME_EXE"):
            options["executable_path"] = os.environ["CHROME_EXE"]
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(storage_state=str(session), user_agent=USER_AGENT)
            page = context.new_page()
            urls = get_bookmark_urls(page, args.count)
            if not urls:
                raise SystemExit("No bookmarks found; check the X session")
            for url in urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    wait_for_content(page)
                    content = extract_content_from_page(page)
                    items = content["items"]
                    chars = sum(len(item.get("text", "")) for item in items if item["type"] != "image")
                    if not (content["is_article"] or chars >= int(os.getenv("MIN_TEXT_CHARS", "1200"))):
                        continue
                    task = enqueue_source(url=url, title=content["title"] or "article", author=content["author"], items=items)
                    queued += bool(task["enqueued"])
                    existing += not task["enqueued"]
                except Exception as exc:
                    print(f"Bookmark failed ({type(exc).__name__}): {url}")
                    failures += 1
            context.close()
        finally:
            browser.close()
    print(f"Podcast: {queued} queued, {existing} existing, {failures} failures")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
