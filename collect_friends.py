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

with sqlite3.connect(DB) as db:
    db.execute('''CREATE TABLE IF NOT EXISTS friends (
        profile_url TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        decision TEXT NOT NULL DEFAULT 'pending'
        CHECK(decision IN ('pending','keep','remove'))
    )''')

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE), headless=False)
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto('https://www.facebook.com/me/friends', wait_until='domcontentloaded')
    print('Scroll through your friends list to load the people you want to review.')
    input('When ready, press Enter here to collect visible profiles... ')
    links = page.locator('a[href]').evaluate_all('''elements => elements.map(a => ({
        name: (a.innerText || a.getAttribute('aria-label') || '').trim(),
        url: a.href
    }))''')
    rows = {}
    excluded = {'friends', 'groups', 'watch', 'marketplace', 'messages', 'notifications', 'settings', 'gaming', 'events', 'pages', 'reel', 'reels', 'photo', 'photos', 'videos', 'profile.php'}
    for item in links:
        name = ' '.join(item['name'].split())
        parts = urlsplit(item['url'])
        if parts.hostname not in {'www.facebook.com', 'facebook.com', 'm.facebook.com'}:
            continue
        segments = [s for s in parts.path.split('/') if s]
        if not segments or segments[0].lower() in excluded or len(segments) != 1:
            continue
        if not name or len(name) > 100 or '\n' in name:
            continue
        url = urlunsplit(('https', 'www.facebook.com', '/' + segments[0], '', ''))
        rows[url] = name
    with sqlite3.connect(DB) as db:
        db.executemany('INSERT OR IGNORE INTO friends(profile_url, name) VALUES (?, ?)',
                       [(url, name) for url, name in rows.items()])
        count = db.execute('SELECT COUNT(*) FROM friends').fetchone()[0]
    print(f'Found {len(rows)} candidate profile links; {count} saved total.')
    print('Review these entries before using them: navigation/profile links can be misclassified.')
    for url, name in list(rows.items())[:15]:
        print(f'  {name}: {url}')
    browser.close()
