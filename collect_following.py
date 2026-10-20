"""Collect visible followed profiles and pages from facebook.com/me/following.
Stores records in SQLite `following` table for review.
"""

import argparse
import random
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def get_root():
    cwd = Path.cwd()
    if (cwd / 'friends.sqlite3').exists() or (cwd / 'browser_profile').exists():
        return cwd
    return Path(__file__).resolve().parent


ROOT = get_root()
DB = ROOT / 'friends.sqlite3'
PROFILE = ROOT / 'browser_profile'


def init_db():
    with sqlite3.connect(DB) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS following (
            profile_url TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            photo_url TEXT,
            decision TEXT NOT NULL DEFAULT 'pending'
                CHECK(decision IN ('pending', 'keep', 'unfollow')),
            reviewed_at TEXT
        )''')


def normalize_fb_url(raw_url: str):
    try:
        p = urlsplit(raw_url)
        if p.hostname not in {'www.facebook.com', 'facebook.com', 'm.facebook.com'}:
            return None

        # Check profile.php?id=12345
        if p.path.rstrip('/') == '/profile.php':
            qs = parse_qs(p.query)
            if 'id' in qs and qs['id']:
                return f"https://www.facebook.com/profile.php?id={qs['id'][0]}"
            return None

        parts = [x for x in p.path.split('/') if x]
        if len(parts) == 1:
            slug = parts[0].lower()
            excluded = {
                'friends', 'groups', 'watch', 'marketplace', 'messages',
                'notifications', 'settings', 'gaming', 'events', 'pages',
                'reel', 'reels', 'photo', 'photos', 'videos', 'stories',
                'saved', 'bookmarks', 'me'
            }
            if slug in excluded:
                return None
            return f"https://www.facebook.com/{parts[0]}"
        return None
    except Exception:
        return None


def extract_visible_followed(page):
    return page.evaluate('''() => {
        let main = document.querySelector('div[role="main"]');
        if (!main) return [];

        let results = [];
        let links = Array.from(main.querySelectorAll('a[role="link"], a[href]'));

        for (let a of links) {
            let href = a.href;
            let text = (a.innerText || '').trim();

            if (!text || text.includes('\\n') || text.length > 80) continue;
            let lower = text.toLowerCase();
            if (lower.includes('friend') || lower.includes('reels') || lower.includes('post') ||
                lower.includes('about') || lower.includes('story') || lower.includes('following')) {
                continue;
            }

            // Find parent card that contains the avatar image
            let card = a.parentElement;
            let img = null;
            for (let i = 0; i < 6; i++) {
                if (!card || card.getAttribute('role') === 'main') break;
                let foundImg = card.querySelector('img');
                if (foundImg && foundImg.src && !foundImg.src.includes('data:') && !foundImg.src.includes('rsrc.php')) {
                    img = foundImg.src;
                    break;
                }
                card = card.parentElement;
            }

            results.push({
                name: text,
                url: href,
                photo_url: img
            });
        }
        return results;
    }''')


def main():
    parser = argparse.ArgumentParser(description="Collect profiles and pages you follow from Facebook.")
    parser.add_argument("--auto-scroll", action="store_true", help="Automatically scroll down to load items.")
    parser.add_argument("--max-scrolls", type=int, default=30, help="Maximum number of scrolls in auto mode.")
    args = parser.parse_args()

    init_db()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        print("Navigating to https://www.facebook.com/me/following ...")
        page.goto("https://www.facebook.com/me/following", wait_until="domcontentloaded")
        time.sleep(3)

        # Detect logged-in user's own profile base URL to exclude self
        own_url_clean = page.url.split('/following')[0].rstrip('/')
        own_slug = own_url_clean.split('/')[-1].lower() if own_url_clean else ''

        if args.auto_scroll:
            print(f"Auto-scrolling enabled (up to {args.max_scrolls} scrolls)...")
            prev_height = 0
            for i in range(args.max_scrolls):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(2.0, 3.5))
                curr_height = page.evaluate("document.body.scrollHeight")
                print(f"  Scroll {i+1}/{args.max_scrolls} complete (page height: {curr_height}px)")
                if curr_height == prev_height and i > 5:
                    print("  No new items loaded after repeated scrolls. Stopping auto-scroll.")
                    break
                prev_height = curr_height
        else:
            print("\nBrowser is open to your 'Following' page.")
            print("Scroll down in the browser to load all accounts and pages you follow.")
            input("When finished scrolling, press Enter here to collect visible profiles... ")

        raw_items = extract_visible_followed(page)
        browser.close()

    # Deduplicate and normalize
    items = {}
    for it in raw_items:
        clean_url = normalize_fb_url(it['url'])
        if clean_url and clean_url.rstrip('/') != own_url_clean and (not own_slug or own_slug not in clean_url.lower()):
            if clean_url not in items or (not items[clean_url]['photo_url'] and it['photo_url']):
                items[clean_url] = {
                    'name': it['name'],
                    'photo_url': it['photo_url']
                }

    with sqlite3.connect(DB) as db:
        db.executemany('''
            INSERT INTO following (profile_url, name, photo_url)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_url) DO UPDATE SET
                photo_url = coalesce(excluded.photo_url, following.photo_url),
                name = excluded.name
        ''', [(url, data['name'], data['photo_url']) for url, data in items.items()])

        total = db.execute("SELECT COUNT(*) FROM following").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM following WHERE decision='pending'").fetchone()[0]

    print(f"\nSuccessfully collected {len(items)} followed profiles/pages from this session.")
    print(f"Total in database: {total} ({pending} pending review).")
    print("Start 'app.py' or 'fb-cleanup-app' to review who to keep and who to unfollow!")


if __name__ == '__main__':
    main()
