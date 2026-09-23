"""Extract X article and post text for podcast queueing."""

import re

import time

JS_ARTICLE_ITEMS = """
() => {
    const comp = document.querySelector('[data-testid="longformRichTextComponent"]');
    if (!comp) return null;
    const items = [];
    const seen = new Set();
    const all = comp.querySelectorAll('[data-block="true"], [data-testid="tweetPhoto"]');
    for (const el of all) {
        const tid = el.getAttribute('data-testid') || '';
        if (tid === 'tweetPhoto') {
            const img = el.querySelector('img');
            if (img && img.src && img.src.includes('pbs.twimg.com')) {
                const src = img.src.replace(/&name=\\w+$/, '&name=large');
                if (!seen.has(src)) { seen.add(src); items.push({type:'image', src}); }
            }
            continue;
        }
        let parent = el.parentElement;
        let nested = false;
        while (parent && parent !== comp) {
            if (parent.hasAttribute('data-block')) { nested = true; break; }
            parent = parent.parentElement;
        }
        if (nested) continue;
        const tag = el.tagName.toLowerCase();
        const cls = el.className || '';
        const text = (el.innerText || '').trim();
        if (!text) continue;
        let blockType = 'para';
        if (tag === 'li') blockType = 'li';
        else if (tag === 'h1') blockType = 'h1';
        else if (tag === 'h2') blockType = 'h2';
        else if (tag === 'h3' || tag === 'h4') blockType = 'h3';
        else if (cls.includes('longform-header-two') || cls.includes('longform-header-one')) blockType = 'h2';
        items.push({type: blockType, text});
    }
    return items;
}
"""

JS_TWEET_ITEMS = """
() => {
    const container = document.querySelector('article[data-testid="tweet"]');
    if (!container) return null;
    const items = [];
    const seen = new Set();
    const elems = container.querySelectorAll('[data-testid="tweetText"],[data-testid="tweetPhoto"]');
    for (const el of elems) {
        let nested = false;
        let par = el.parentElement;
        while (par && par !== container) {
            const pid = par.getAttribute && par.getAttribute('data-testid');
            if (pid === 'tweetText' || pid === 'tweetPhoto') { nested = true; break; }
            par = par.parentElement;
        }
        if (nested) continue;
        const tid = el.getAttribute('data-testid');
        if (tid === 'tweetText') {
            const text = (el.innerText || '').trim();
            if (text) items.push({type:'para', text});
        } else if (tid === 'tweetPhoto') {
            const img = el.querySelector('img');
            if (img && img.src && img.src.includes('pbs.twimg.com')) {
                const src = img.src.replace(/&name=\\w+$/, '&name=large');
                if (!seen.has(src)) { seen.add(src); items.push({type:'image', src}); }
            }
        }
    }
    return items;
}
"""

JS_TITLE = """
() => {
    const t = document.querySelector('[data-testid="twitter-article-title"]');
    return t ? (t.innerText || '').trim() : '';
}
"""

JS_AUTHOR = """
() => {
    const el = document.querySelector('[data-testid="User-Name"]');
    if (!el) return '';
    return (el.innerText || '').split('\\n').join(' ').trim();
}
"""

CUTOFF_PHRASES = [
    "Want to publish your own Article?",
    "Upgrade to Premium",
    "View quotes",
]

STATS_LINE = re.compile(r"^\d+(\.\d+)?[KkMm]?$")

def is_junk_line(s: str) -> bool:
    s = s.strip()
    return not s or s.startswith("@") or bool(STATS_LINE.match(s))

def strip_tweet_header(raw: str, author_name: str) -> tuple[str, str]:
    """Remove author/handle/stats header from raw tweetText. Returns (title, body)."""
    for phrase in CUTOFF_PHRASES:
        idx = raw.find(phrase)
        if idx != -1:
            raw = raw[:idx]
    lines = raw.strip().splitlines()
    display_name = author_name.split("@")[0].strip()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or is_junk_line(lines[i]):
            i += 1
            continue
        if s == display_name or s.replace(" ", "") == display_name.replace(" ", ""):
            i += 1
            continue
        break
    remaining = lines[i:]
    title, body_start = "", 0
    for j, line in enumerate(remaining):
        if not is_junk_line(line):
            title = line.strip()
            body_start = j + 1
            break
    body_lines, skip_stats = [], True
    for line in remaining[body_start:]:
        if skip_stats and is_junk_line(line):
            continue
        skip_stats = False
        body_lines.append(line)
    return title, "\n".join(body_lines).strip()

def strip_cutoff(text: str) -> str:
    for phrase in CUTOFF_PHRASES:
        idx = text.find(phrase)
        if idx != -1:
            text = text[:idx]
    return text.strip()

def wait_for_content(page):
    """Wait for the page to expose readable content."""
    try:
        page.wait_for_selector(
            "[data-testid='twitterArticleRichTextView'], [data-testid='tweetText']",
            timeout=20000,
        )
    except Exception:
        try:
            page.wait_for_selector("article[data-testid='tweet']", timeout=10000)
        except Exception:
            pass
        time.sleep(3)
    time.sleep(2)

def extract_content_from_page(page) -> dict:
    """
    Extract ordered content from an already-loaded Playwright page.

    Returns:
        {"title": str, "author": str, "items": list[dict], "is_article": bool}
    """
    author = page.evaluate(JS_AUTHOR) or ""
    title = page.evaluate(JS_TITLE) or ""
    is_article = bool(title)

    if is_article:
        raw_items = page.evaluate(JS_ARTICLE_ITEMS) or []
        items = []
        for item in raw_items:
            if item["type"] == "image":
                items.append(item)
            else:
                text = strip_cutoff(item["text"])
                if text:
                    items.append({**item, "text": text})
    else:
        raw_items = page.evaluate(JS_TWEET_ITEMS) or []
        items = []
        first_done = False
        for item in raw_items:
            if item["type"] == "para" and not first_done:
                first_done = True
                extracted_title, body = strip_tweet_header(item["text"], author)
                if not title:
                    title = extracted_title
                if body:
                    items.append({"type": "para", "text": body})
            elif item["type"] == "para":
                text = strip_cutoff(item["text"])
                if text:
                    items.append({"type": "para", "text": text})
            else:
                items.append(item)

    return {"title": title, "author": author, "items": items, "is_article": is_article}
