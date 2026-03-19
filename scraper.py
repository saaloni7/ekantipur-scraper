from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


def main() -> None:
    """
    Scrape Ekantipur using Playwright (sync API) and write `output.json`.

    Flow:
    - Start Playwright and launch Chromium (headless=False for debugging).
    - Create an isolated browser context (separate cookies/cache).
    - Task 1: scrape top 5 Entertainment articles.
    - Task 2: scrape Cartoon of the Day from /cartoon.
    - Save results to output.json as UTF-8 JSON (ensure_ascii=False).
    - Close the browser to clean up resources.
    """

    # Ensure stdout can print Nepali/Unicode text on Windows terminals.
    # (Prevents UnicodeEncodeError when printing debug output.)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    base_url = "https://ekantipur.com"
    entertainment_url = f"{base_url}/entertainment"
    cartoon_url = f"{base_url}/cartoon"

    with sync_playwright() as p:
        # Launch a visible browser window for debugging/inspection.
        browser = p.chromium.launch(headless=False)

        # Browser context = isolated session (cookies/storage). We also set an explicit language preference
        # to encourage Nepali content when the site supports localization.
        context = browser.new_context(
            locale="ne-NP",
            extra_http_headers={"Accept-Language": "ne-NP,ne;q=0.9,en;q=0.8"},
        )
        page = context.new_page()
        # Guard against hangs by applying consistent timeouts.
        page.set_default_timeout(30_000)
        page.set_default_navigation_timeout(30_000)

        try:
            entertainment_news = scrape_entertainment(
                page=page,
                base_url=base_url,
                url=entertainment_url,
                category_label="मनो रञ्जन",
            )
            cartoon_of_the_day = scrape_cartoon_of_the_day(
                page=page,
                base_url=base_url,
                url=cartoon_url,
            )

            # Enforce the required top-level JSON structure with no extra/missing keys.
            output: dict[str, Any] = {
                "entertainment_news": ensure_entertainment_shape(
                    entertainment_news, category_label="मनो रञ्जन"
                ),
                "cartoon_of_the_day": ensure_cartoon_shape(
                    cartoon_of_the_day.get("title") if isinstance(cartoon_of_the_day, dict) else None,
                    cartoon_of_the_day.get("image_url") if isinstance(cartoon_of_the_day, dict) else None,
                    cartoon_of_the_day.get("author") if isinstance(cartoon_of_the_day, dict) else None,
                ),
            }

            # Write valid UTF-8 JSON; ensure_ascii=False keeps Nepali characters intact.
            with open("output.json", "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)

            # Optional: print a small confirmation (safe with UTF-8 reconfigure above).
            print("Saved output to output.json")
        finally:
            # Always close the browser so the script doesn't leave orphaned processes.
            browser.close()


def scrape_entertainment(
    *,
    page,
    base_url: str,
    url: str,
    category_label: str,
) -> list[dict[str, Any]]:
    """
    Task 1: Entertainment News

    DOM structure (per requirement):
    - Main wrapper: div.category-wrapper
    - Article container: div.category (or article.teaser)
    - Title: h2 a (visible text)
    - Image: div.image img (src or data-src if lazy-loaded)
    - Author: div.author a (optional)
    """

    # Navigate and wait for the wrapper that contains the list of articles.
    # Timeouts prevent the run from hanging if the site is slow or blocks automation.
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector("div.category-wrapper", timeout=30_000)

        # Waiting for "networkidle" reduces flakiness on JS-heavy pages with late network requests.
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception as e:
        print(f"[entertainment] navigation failed: {e!r}")
        return ensure_entertainment_shape([], category_label=category_label)

    wrapper = page.query_selector("div.category-wrapper")
    if not wrapper:
        return ensure_entertainment_shape([], category_label=category_label)

    # Support both possible article containers.
    # We keep the selector scoped to the category wrapper to avoid unrelated cards elsewhere.
    article_nodes = wrapper.query_selector_all("div.category, article.teaser")

    # Debug: confirm how many cards we found on the page.
    print(f"[entertainment] cards_found={len(article_nodes)}")

    results: list[dict[str, Any]] = []

    # Reuse a single detail tab for author fallback to avoid opening many pages.
    detail_page = None
    try:
        detail_page = page.context.new_page()
        detail_page.set_default_timeout(15_000)
        detail_page.set_default_navigation_timeout(15_000)
    except Exception:
        detail_page = None

    # We collect until we have exactly 5 valid items (non-empty title), skipping malformed/empty cards.
    # This keeps output consistent even if the page includes promotional/empty placeholders.
    for idx, article in enumerate(article_nodes, start=1):
        if len(results) >= 5:
            break
        try:
            # Title is in h2 a.
            title_el = article.query_selector("h2 a")
            title = title_el.inner_text().strip() if title_el else None
            href = title_el.get_attribute("href") if title_el else None
            article_url = urljoin(base_url, href) if href else None

            # Image:
            # Prefer images within the article's own media container (div.image) to avoid picking logos/icons.
            # We also scroll the card into view to trigger lazy-loading before reading attributes.
            try:
                article.scroll_into_view_if_needed()
            except Exception:
                pass

            img_el = (
                article.query_selector("div.image img")
                or article.query_selector("figure img")
                or article.query_selector("img")
            )
            image_url, img_debug = extract_image_url_with_debug(img_el, base_url=base_url)

            # Author:
            # Author markup can vary; try multiple scoped selectors inside the card.
            author = None
            for sel in (
                "div.author a",
                ".author a",
                "span.author",
                "div.byline a",
                "span.byline",
                ".byline a",
                ".byline",
                ".meta a",
                ".meta",
            ):
                el = article.query_selector(sel)
                if not el:
                    continue
                try:
                    t = el.inner_text().strip()
                    if t:
                        author = t
                        break
                except Exception:
                    continue

            # Last-resort heuristic:
            # Some cards embed author text in a small metadata line (often short, near the title).
            if author is None:
                try:
                    meta = article.query_selector("div.author, div.byline, div.meta, span.meta")
                    t = meta.inner_text().strip() if meta else ""
                    if t and len(t) <= 120:
                        author = t
                except Exception:
                    pass

            # Debug: log missing fields + the exact image attributes we observed.
            missing: list[str] = []
            if not title:
                missing.append("title")
            if not image_url:
                missing.append("image_url")
            if author is None:
                missing.append("author")

            if missing:
                print(
                    f"[entertainment] card#{idx} missing={missing} "
                    f"title={repr(title)} img={img_debug}"
                )

            # Validate required fields:
            # - title must not be empty (skip the card if missing)
            # - image_url should be a string when possible; keep null if not extractable
            title = normalize_text(title)
            if not title:
                continue

            image_url = normalize_url(image_url)
            author = normalize_text(author)  # returns None if empty/missing

            # Fallback mechanism:
            # If the author is not present in the listing DOM (common on some cards),
            # visit the article detail page and extract author from its metadata area.
            if author is None and article_url:
                print(f"[entertainment] author_fallback visiting url={article_url}")
                author = fetch_author_from_article_detail(
                    context_page=page,
                    detail_page=detail_page,
                    base_url=base_url,
                    article_url=article_url,
                )
                if author is None:
                    print(f"[entertainment] author_fallback not found url={article_url}")

            results.append(
                {
                    "title": title,
                    "image_url": image_url,
                    "category": category_label,
                    "author": author,
                }
            )
        except Exception:
            # If one article card is malformed, skip it without stopping the whole scrape.
            continue

    # Guarantee exactly 5 items (pad if the site returns fewer than 5 usable cards).
    if len(results) != 5:
        print(f"[entertainment] warning: expected 5 items, got {len(results)}")
    try:
        return ensure_entertainment_shape(results, category_label=category_label)
    finally:
        if detail_page:
            try:
                detail_page.close()
            except Exception:
                pass


def scrape_cartoon_of_the_day(
    *,
    page,
    base_url: str,
    url: str,
) -> dict[str, Any]:
    """
    Task 2: Cartoon of the Day

    Requirements:
    - Navigate to https://ekantipur.com/cartoon
    - Cartoon container: div.cartoon-image
    - Image: div.cartoon-image img (src or data-src)
    - Title: from img alt/title or nearby caption if available
    - Author: cartoonist name if present (nearby caption / author area)
    """

    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception:
        # Always return the required keys even if navigation fails.
        return ensure_cartoon_shape(None, None, None)

    try:
        # Wait until the main cartoon image container exists.
        page.wait_for_selector("div.cartoon-image", timeout=30_000)

        # Wait for the <img> specifically (lazy-loading sometimes inserts it later).
        page.wait_for_selector("div.cartoon-image img", timeout=30_000)
    except Exception:
        return ensure_cartoon_shape(None, None, None)

    page.wait_for_load_state("networkidle")

    container = page.query_selector("div.cartoon-image")
    img_el = container.query_selector("img") if container else None

    # Scroll into view to trigger lazy-loading, then re-check the image URL.
    try:
        if container:
            container.scroll_into_view_if_needed()
    except Exception:
        pass

    image_url, img_debug = extract_image_url_with_debug(img_el, base_url=base_url)
    if not image_url:
        print(f"[cartoon] image_url missing img={img_debug}")

    # Title: prefer meaningful nearby caption text; fall back to img alt/title attributes.
    title = None
    try:
        # Try common caption nodes near the cartoon image.
        caption_root = container.evaluate_handle("el => el.closest('figure') || el.parentElement")
        # We avoid hardcoding a single tag; instead, scan common text-bearing tags.
        caption_text = None
        try:
            caption_text = page.evaluate(
                """(root) => {
                    if (!root) return null;
                    const candidates = root.querySelectorAll('figcaption, h1, h2, h3, h4, p, div, span');
                    for (const el of candidates) {
                        const t = (el.innerText || '').trim();
                        if (t && t.length <= 200) return t;
                    }
                    return null;
                }""",
                caption_root,
            )
        except Exception:
            caption_text = None

        if isinstance(caption_text, str) and caption_text.strip():
            # If caption has multiple lines (title/author), we take the first line as title.
            lines = [ln.strip() for ln in caption_text.splitlines() if ln.strip()]
            title = lines[0] if lines else None
    except Exception:
        title = None

    if not title and img_el:
        try:
            alt = img_el.get_attribute("alt")
            if alt and alt.strip():
                title = alt.strip()
        except Exception:
            pass

    if not title and img_el:
        try:
            t = img_el.get_attribute("title")
            if t and t.strip():
                title = t.strip()
        except Exception:
            pass

    # Author: try to find a plausible cartoonist name near the cartoon section.
    author = None
    try:
        # Look for common "author-ish" areas near the cartoon container.
        root = container.evaluate_handle("el => el.closest('section') || el.closest('div') || document")
        maybe = page.evaluate(
            """(root) => {
                if (!root) return null;
                const sels = [
                  '.author', '.cartoon-author', '.byline',
                  'figcaption .author', 'figcaption a',
                  'span.author', 'div.author',
                  '.meta', '.meta a',
                  '.cartoon-caption', '.cartoon-caption a',
                  '.cartoon-title', '.cartoon-title a',
                  '.cartoon-detail', '.cartoon-detail a'
                ];
                for (const sel of sels) {
                    const el = root.querySelector(sel);
                    if (!el) continue;
                    const t = (el.innerText || '').trim();
                    if (t && t.length <= 120) return t;
                }
                return null;
            }""",
            root,
        )
        if isinstance(maybe, str) and maybe.strip():
            author = maybe.strip()
    except Exception:
        author = None

    # If title contains a trailing "- name" (common in cartoons), split it as title/author.
    if author is None and isinstance(title, str) and " - " in title:
        try:
            parts = [p.strip() for p in title.split(" - ", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                title, author = parts[0], parts[1]
        except Exception:
            pass

    if author is None:
        print("[cartoon] author missing")

    title = normalize_text(title)
    image_url = normalize_url(image_url)
    author = normalize_text(author)

    return ensure_cartoon_shape(title, image_url, author)


def extract_image_url(img_el, *, base_url: str) -> str | None:
    """
    Convert <img> attributes into a full absolute URL.
    We check src and common lazy-load attributes; fall back to browser-resolved currentSrc/src.
    """

    if not img_el:
        return None

    raw = None
    try:
        raw = img_el.get_attribute("src") or img_el.get_attribute("data-src")
        if not raw:
            raw = img_el.get_attribute("data-lazy-src") or img_el.get_attribute("data-original")
        if not raw:
            srcset = img_el.get_attribute("srcset")
            if srcset:
                first = srcset.split(",")[0].strip()
                raw = first.split(" ")[0].strip() if first else None
        if not raw:
            # Ask the browser for the resolved URL (covers currentSrc and some lazy-loading cases).
            raw = img_el.evaluate("el => el.currentSrc || el.src")
    except Exception:
        raw = None

    return urljoin(base_url, raw) if raw else None


def extract_image_url_with_debug(img_el, *, base_url: str) -> tuple[str | None, dict[str, Any]]:
    """
    Same as extract_image_url(), but also returns a debug dictionary of the attributes inspected.
    This helps diagnose why a specific card's image_url is missing.
    """

    debug: dict[str, Any] = {
        "found": bool(img_el),
        "src": None,
        "data-src": None,
        "data-lazy-src": None,
        "data-original": None,
        "srcset": None,
        "currentSrc/src": None,
        "resolved": None,
    }

    if not img_el:
        return None, debug

    raw = None
    try:
        debug["src"] = img_el.get_attribute("src")
        debug["data-src"] = img_el.get_attribute("data-src")
        debug["data-lazy-src"] = img_el.get_attribute("data-lazy-src")
        debug["data-original"] = img_el.get_attribute("data-original")
        debug["srcset"] = img_el.get_attribute("srcset")

        # Prefer lazy-load attributes when src is empty/placeholder.
        raw = debug["data-src"] or debug["data-lazy-src"] or debug["data-original"] or debug["src"]

        if not raw and debug["srcset"]:
            first = str(debug["srcset"]).split(",")[0].strip()
            raw = first.split(" ")[0].strip() if first else None

        if not raw:
            # Ask the browser for the resolved URL (covers currentSrc and some lazy-loading cases).
            try:
                debug["currentSrc/src"] = img_el.evaluate("el => el.currentSrc || el.src")
                raw = debug["currentSrc/src"]
            except Exception:
                raw = None
    except Exception:
        raw = None

    resolved = urljoin(base_url, raw) if raw else None
    debug["resolved"] = resolved
    return resolved, debug


def normalize_text(value: Any) -> str | None:
    """
    Normalize text fields:
    - Convert non-strings to None
    - Strip whitespace
    - Convert empty strings to None
    """

    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def normalize_url(value: Any) -> str | None:
    """
    Normalize URLs:
    - Keep only non-empty strings
    - Do not attempt to "invent" a URL if missing
    """

    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value else None


def fetch_author_from_article_detail(
    *,
    context_page,
    detail_page=None,
    base_url: str,
    article_url: str,
) -> str | None:
    """
    Fallback author extraction from article detail page.

    Why a separate page:
    - We keep the listing page state intact (so we don't lose our place in the loop).
    - We avoid mixing data between cards by extracting author in a fresh, isolated tab.
    """

    detail = detail_page
    created_here = False
    if detail is None:
        try:
            detail = context_page.context.new_page()
            detail.set_default_timeout(15_000)
            detail.set_default_navigation_timeout(15_000)
            created_here = True
        except Exception:
            return None

    try:
        detail.goto(article_url, wait_until="domcontentloaded")
        detail.wait_for_load_state("networkidle")

        # Try multiple likely author selectors near headline / metadata.
        # (Exact markup can vary by article type.)
        selectors = (
            "div.author a",
            ".author a",
            "span.author",
            "a[rel='author']",
            "[itemprop='author'] [itemprop='name']",
            "[itemprop='author'] a",
            "[itemprop='author']",
            # Ekantipur often links the author to /author/author-XXXXX
            "a[href^='/author/']",
            "a[href*='/author/']",
            ".byline a",
            ".byline",
            ".author-name",
            ".author-name a",
            ".news-author a",
            ".news-author",
            ".news__author a",
            ".news__author",
            ".byline-author a",
            "div.story__author a",
            "div.story__author",
            "div.meta a",
            "div.meta",
        )

        for sel in selectors:
            try:
                el = detail.query_selector(sel)
                if not el:
                    continue
                text = el.inner_text().strip()
                text = normalize_text(text)
                if text:
                    # Debug: confirm which selector produced the author.
                    print(f"[entertainment] author_fallback hit selector={sel!r} url={article_url}")
                    return text
            except Exception:
                continue

        # Heuristic fallback:
        # Some pages include author text in a compact metadata block; scan a few common containers.
        try:
            # Meta tag fallback (often present even when the DOM differs).
            meta_author = detail.locator("meta[name='author']").first.get_attribute("content")
            meta_author = normalize_text(meta_author)
            if meta_author:
                print(f"[entertainment] author_fallback hit meta[name=author] url={article_url}")
                return meta_author

            for container_sel in ("header", ".story", ".story__header", ".content"):
                container = detail.query_selector(container_sel)
                if not container:
                    continue
                t = container.inner_text()
                if not isinstance(t, str):
                    continue
                # Look for short lines that might represent an author name (avoid huge blocks).
                lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
                for ln in lines[:30]:
                    # Keep it conservative: if it looks like "By ..." or contains Nepali/Latin name tokens.
                    if ln.lower().startswith("by "):
                        maybe = normalize_text(ln[3:])
                        if maybe:
                            return maybe
        except Exception:
            pass

        return None
    except Exception:
        return None
    finally:
        if created_here:
            try:
                detail.close()
            except Exception:
                pass


def ensure_entertainment_shape(
    items: list[dict[str, Any]],
    *,
    category_label: str,
) -> list[dict[str, Any]]:
    """
    Enforce the exact output structure for entertainment_news:
    - Exactly 5 objects
    - Exactly keys: title, image_url, category, author
    - category is always the provided label
    """

    normalized: list[dict[str, Any]] = []
    for it in items:
        normalized.append(
            {
                "title": normalize_text(it.get("title")),
                "image_url": normalize_url(it.get("image_url")),
                "category": category_label,
                "author": normalize_text(it.get("author")),
            }
        )

    # Pad or trim to exactly 5, while keeping keys consistent.
    while len(normalized) < 5:
        normalized.append(
            {
                "title": None,
                "image_url": None,
                "category": category_label,
                "author": None,
            }
        )
    return normalized[:5]


def ensure_cartoon_shape(
    title: Any,
    image_url: Any,
    author: Any,
) -> dict[str, Any]:
    """
    Enforce the exact output structure for cartoon_of_the_day:
    - Exactly keys: title, image_url, author
    - Values are strings or null
    """

    return {
        "title": normalize_text(title),
        "image_url": normalize_url(image_url),
        "author": normalize_text(author),
    }


if __name__ == "__main__":
    main()

