"""Collect visible friend thumbnails from the logged-in Facebook friends page.
No Facebook modifications. Only updates photo_url for exact matching profile URLs.
"""
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from playwright.sync_api import sync_playwright

def get_root():
    cwd = Path.cwd()
    if (cwd / 'friends.sqlite3').exists() or (cwd / 'browser_profile').exists():
        return cwd
    return Path(__file__).resolve().parent

ROOT = get_root()
DB = ROOT / 'friends.sqlite3'
PROFILE = ROOT / 'browser_profile'


def normalize(url):
    try:
        p = urlsplit(url)
        if p.hostname not in {'www.facebook.com', 'facebook.com', 'm.facebook.com'}:
            return None
        parts = [x for x in p.path.split('/') if x]
        if len(parts) != 1 or parts[0].lower() in {'friends', 'groups', 'watch', 'marketplace', 'messages', 'notifications', 'settings', 'gaming', 'events', 'pages', 'reel', 'reels', 'photo', 'photos', 'videos', 'profile.php'}:
            return None
        return urlunsplit(('https', 'www.facebook.com', '/' + parts[0], '', ''))
    except (ValueError, TypeError):
        return None


with sqlite3.connect(DB) as db:
    cols = {r[1] for r in db.execute('PRAGMA table_info(friends)')}
    if not cols:
        raise SystemExit('Missing friends table. Run your existing collector first.')
    if 'photo_url' not in cols:
        db.execute('ALTER TABLE friends ADD COLUMN photo_url TEXT')
    known = {r[0] for r in db.execute('SELECT profile_url FROM friends')}

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(user_data_dir=str(PROFILE), headless=False)
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto('https://www.facebook.com/me/friends', wait_until='domcontentloaded')
    print('Scroll through your friends list until pictures are loaded.')
    print('Press Enter here when finished. No Facebook changes will be made.')
    input('Press Enter to collect photos... ')
    candidates = page.locator('a[href]').evaluate_all('''anchors => anchors.map(a => {
      const imgs = Array.from(a.querySelectorAll('img'));
      const img = imgs.find(i => i.currentSrc && i.naturalWidth > 0 && i.naturalHeight > 0);
      return {url:a.href, photo:img ? img.currentSrc : null};
    }).filter(x => x.photo)''')
    updates = {}
    for item in candidates:
        url = normalize(item['url'])
        photo = item['photo']
        if url in known and isinstance(photo, str) and photo.startswith('https://'):
            updates[url] = photo
    with sqlite3.connect(DB) as db:
        db.executemany('UPDATE friends SET photo_url=? WHERE profile_url=?', [(photo, url) for url, photo in updates.items()])
        covered = db.execute("SELECT COUNT(*) FROM friends WHERE photo_url IS NOT NULL AND photo_url!=''").fetchone()[0]
    print(f'Matched {len(updates)} visible photos; {covered} total profiles now have photo URLs.')
    print('If few matched, Facebook may put images outside the profile links; report the count so we can adjust.')
    browser.close()
