import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, jsonify, request, render_template_string
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'friends.sqlite3'
PROFILE = ROOT / 'browser_profile'
app = Flask(__name__)

def connect():
    db = sqlite3.connect(DB, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout = 5000')
    return db

def init():
    with connect() as db:
        db.execute('PRAGMA journal_mode = WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS friends (
            profile_url TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','keep','remove'))
        )''')
        cols = {x['name'] for x in db.execute('PRAGMA table_info(friends)')}
        if 'photo_url' not in cols:
            db.execute('ALTER TABLE friends ADD COLUMN photo_url TEXT')
        if 'reviewed_at' not in cols:
            db.execute('ALTER TABLE friends ADD COLUMN reviewed_at TEXT')

        # Mutual friends cache and results tables
        db.execute('''CREATE TABLE IF NOT EXISTS mutual_friend_cache (
            profile_url TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('found', 'none', 'unavailable', 'error')),
            error_message TEXT,
            friend_count INTEGER NOT NULL DEFAULT 0,
            fetched_at TEXT NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS mutual_friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_profile_url TEXT NOT NULL,
            mutual_profile_url TEXT NOT NULL,
            name TEXT NOT NULL,
            photo_url TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(target_profile_url, mutual_profile_url)
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_mutual_friends_target ON mutual_friends(target_profile_url)')

def scrape_mutual_friends(page, target_url):
    target_parts = [x for x in urlsplit(target_url).path.split('/') if x]
    target_slug = target_parts[0].lower() if target_parts else ""
    
    mutual_url = target_url + '&sk=friends_mutual' if '?' in target_url else target_url.rstrip('/') + '/friends_mutual'
    page.goto(mutual_url, wait_until='domcontentloaded', timeout=20000)
    page.wait_for_timeout(2500)

    # Scroll down slightly to trigger lazy-loading of mutual friends
    for _ in range(2):
        page.evaluate("window.scrollBy(0, 1000)")
        page.wait_for_timeout(800)

    return page.evaluate("""(targetSlug) => {
        const bodyText = document.body ? document.body.innerText : "";

        // 1. Session check
        const isLogin = (bodyText.includes("Log In") && bodyText.includes("Password")) ||
                        document.querySelector('input[type="password"]') !== null;
        if (isLogin) {
            return { status: "error", error: "Facebook session expired or login required", friends: [] };
        }

        // 2. Unavailable check
        const isUnavailable = bodyText.includes("This content isn't available right now") ||
                              bodyText.includes("This page isn't available") ||
                              bodyText.includes("The link you followed may be broken");
        if (isUnavailable) {
            return { status: "unavailable", error: "Profile or mutual friends list is unavailable", friends: [] };
        }

        // 3. No mutual friends explicit check
        const hasNoMutualFriends = bodyText.includes("No friends to show") ||
                                   bodyText.includes("No mutual friends to show") ||
                                   bodyText.includes("No mutual friends");

        const excludedPaths = new Set([
            'friends', 'groups', 'watch', 'marketplace', 'messages', 'notifications',
            'settings', 'gaming', 'events', 'pages', 'reel', 'reels', 'photo', 'photos',
            'videos', 'profile.php', 'about', 'map', 'likes', 'music', 'books', 'sports',
            'allactivity', 'friends_mutual', 'friends_all', 'saved', 'memories',
            targetSlug.toLowerCase()
        ]);

        const results = [];
        const seenUrls = new Set();

        const allAnchors = Array.from(document.querySelectorAll('a[href]'));
        for (const a of allAnchors) {
            const href = a.href;
            let urlObj;
            try {
                urlObj = new URL(href);
            } catch(e) {
                continue;
            }
            if (!['www.facebook.com', 'facebook.com', 'm.facebook.com'].includes(urlObj.hostname)) {
                continue;
            }
            const parts = urlObj.pathname.split('/').filter(Boolean);
            if (parts.length === 0) continue;
            const slug = parts[0].toLowerCase();
            if (excludedPaths.has(slug)) continue;

            const cleanUrl = 'https://www.facebook.com/' + parts[0];
            if (seenUrls.has(cleanUrl)) continue;

            let rawText = (a.innerText || a.getAttribute('aria-label') || '').trim();
            if (!rawText || rawText.includes('\\n') || rawText.length > 70) {
                continue;
            }

            if (/^\\d+\\s+friends?$/i.test(rawText) || /^\\d+\\s+mutual/i.test(rawText)) {
                continue;
            }
            const lowerText = rawText.toLowerCase();
            if (['friends', 'mutual friends', 'all friends', 'see all', 'message', 'add friend', 'home', 'reels'].includes(lowerText)) {
                continue;
            }

            let parent = a.parentElement;
            let card = null;
            let photoUrl = null;
            for (let i = 0; i < 7; i++) {
                if (!parent) break;
                const img = parent.querySelector('img');
                if (img && (img.src || img.currentSrc)) {
                    const src = img.currentSrc || img.src;
                    if (src && !src.includes('rsrc.php')) {
                        card = parent;
                        photoUrl = src;
                        break;
                    }
                }
                parent = parent.parentElement;
            }

            if (!card) continue;
            const cardText = card.innerText || '';
            const isFriendCard = cardText.toLowerCase().includes('mutual') ||
                                 cardText.toLowerCase().includes('friend') ||
                                 card.querySelector('div[role="button"]') !== null;

            if (isFriendCard && rawText.length >= 2) {
                seenUrls.add(cleanUrl);
                results.push({
                    name: rawText,
                    profile_url: cleanUrl,
                    photo_url: photoUrl
                });
            }
        }

        if (results.length > 0) {
            return { status: "found", friends: results };
        }

        if (hasNoMutualFriends) {
            return { status: "none", friends: [] };
        }

        if (bodyText.includes("Mutual friends") || bodyText.includes("mutual")) {
            return { status: "none", friends: [] };
        }

        return { status: "unavailable", error: "Mutual friends information not available or hidden", friends: [] };
    }""", target_slug)

class MutualFriendsWorker:
    def __init__(self, profile_path):
        self.profile_path = profile_path
        self.queue = []
        self.lock = threading.Lock()
        self.current_url = None
        self.thread = None
        self.running = False

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def enqueue(self, url):
        with self.lock:
            if url == self.current_url:
                return
            if url in self.queue:
                self.queue.remove(url)
            self.queue.insert(0, url)
        self.start()

    def is_in_progress(self, url):
        with self.lock:
            return url == self.current_url or url in self.queue

    def _run(self):
        with sync_playwright() as p:
            browser = None
            page = None
            while self.running:
                url_to_process = None
                with self.lock:
                    if self.queue:
                        url_to_process = self.queue.pop(0)
                        self.current_url = url_to_process
                
                if not url_to_process:
                    time.sleep(0.4)
                    continue

                try:
                    if browser is None or page is None or page.is_closed():
                        browser = p.chromium.launch_persistent_context(
                            user_data_dir=str(self.profile_path),
                            headless=True,
                            args=["--disable-blink-features=AutomationControlled"]
                        )
                        page = browser.pages[0] if browser.pages else browser.new_page()

                    # Check if already cached (skip if handled)
                    with connect() as db:
                        cached = db.execute("SELECT status FROM mutual_friend_cache WHERE profile_url=?", (url_to_process,)).fetchone()
                        if cached and cached['status'] in ('found', 'none', 'unavailable'):
                            continue

                    result = scrape_mutual_friends(page, url_to_process)
                    now = datetime.now(timezone.utc).isoformat()
                    with connect() as db:
                        db.execute("""INSERT INTO mutual_friend_cache (profile_url, status, error_message, friend_count, fetched_at)
                                      VALUES (?, ?, ?, ?, ?)
                                      ON CONFLICT(profile_url) DO UPDATE SET
                                        status=excluded.status,
                                        error_message=excluded.error_message,
                                        friend_count=excluded.friend_count,
                                        fetched_at=excluded.fetched_at""",
                                   (url_to_process, result['status'], result.get('error'), len(result.get('friends', [])), now))
                        if result['status'] == 'found':
                            db.execute("DELETE FROM mutual_friends WHERE target_profile_url=?", (url_to_process,))
                            db.executemany("""INSERT OR IGNORE INTO mutual_friends (target_profile_url, mutual_profile_url, name, photo_url, created_at)
                                              VALUES (?, ?, ?, ?, ?)""",
                                           [(url_to_process, f['profile_url'], f['name'], f.get('photo_url'), now) for f in result.get('friends', [])])
                except Exception as e:
                    now = datetime.now(timezone.utc).isoformat()
                    try:
                        with connect() as db:
                            db.execute("""INSERT INTO mutual_friend_cache (profile_url, status, error_message, friend_count, fetched_at)
                                          VALUES (?, ?, ?, ?, ?)
                                          ON CONFLICT(profile_url) DO UPDATE SET
                                            status=excluded.status,
                                            error_message=excluded.error_message,
                                            friend_count=excluded.friend_count,
                                            fetched_at=excluded.fetched_at""",
                                       (url_to_process, 'error', str(e), 0, now))
                    except Exception:
                        pass
                    # If browser crashed, reset handles so next run re-launches
                    if "closed" in str(e).lower():
                        try:
                            if browser: browser.close()
                        except Exception:
                            pass
                        browser = None
                        page = None
                finally:
                    with self.lock:
                        if self.current_url == url_to_process:
                            self.current_url = None

            if browser:
                try: browser.close()
                except Exception: pass

worker = MutualFriendsWorker(PROFILE)

PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FacebookCleanup</title><style>
body{font-family:system-ui,Arial;background:#f3f5f9;color:#162036;margin:0}
main{max-width:680px;margin:48px auto;padding:20px}
.panel{background:white;border-radius:18px;padding:30px;box-shadow:0 10px 35px #1c34501a}
h1{font-size:25px;margin:0 0 10px}
.muted{color:#667085}
.friend{display:flex;gap:22px;align-items:center;margin:30px 0 20px}
.avatar{width:110px;height:110px;border-radius:50%;object-fit:cover;background:#e8edf5;flex-shrink:0}
.name{font-size:24px;font-weight:700}
a{color:#155bd5;word-break:break-all}
.actions{display:flex;gap:12px;margin-top:24px}
.actions button{flex:1;padding:18px;border:0;border-radius:10px;font-weight:700;font-size:17px;cursor:pointer;transition:transform .1s,opacity .15s}
.actions button:hover{opacity:.92;transform:scale(1.01)}
.actions button:active{transform:scale(0.99)}
.keep{background:#d5f5e3;color:#126138}
.remove{background:#ffe0e0;color:#992d2d}
.undo{background:transparent;border:1px solid #ccc;border-radius:8px;padding:8px 14px;cursor:pointer}
.undo:hover{background:#f8fafc}
.bar{height:8px;background:#e8edf5;border-radius:8px;overflow:hidden;margin:16px 0}
.fill{height:100%;background:#276ef1;width:0;transition:width .3s ease}
.note{font-size:13px;color:#667085;margin-top:20px}
.hidden{display:none}

/* Mutual Friends Styles */
.mutual-box{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin:16px 0}
.mutual-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.mutual-label{font-weight:700;font-size:13px;color:#475569;text-transform:uppercase;letter-spacing:.5px}
.badge{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;background:#e2e8f0;color:#475569}
.badge.found{background:#dbeafe;color:#1e40af}
.badge.none{background:#f1f5f9;color:#64748b}
.badge.unavailable{background:#fef3c7;color:#92400e}
.badge.error{background:#fee2e2;color:#991b1b}
.badge.loading{background:#e0e7ff;color:#3730a3}
.mutual-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;max-height:220px;overflow-y:auto;padding-right:4px}
.mutual-card{display:flex;align-items:center;gap:10px;background:white;border:1px solid #e2e8f0;border-radius:8px;padding:8px 10px;text-decoration:none;color:inherit;transition:border-color .15s,box-shadow .15s}
.mutual-card:hover{border-color:#3b82f6;box-shadow:0 2px 6px #0000000d}
.mutual-thumb{width:36px;height:36px;border-radius:50%;object-fit:cover;background:#e2e8f0;flex-shrink:0}
.mutual-name{font-size:13px;font-weight:600;color:#1e293b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mutual-status-text{font-size:13px;color:#64748b;display:flex;align-items:center;gap:8px;padding:4px 0}
.spinner{width:13px;height:13px;border:2px solid #cbd5e1;border-top-color:#2563eb;border-radius:50%;animation:spin .8s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
.retry-btn{background:#f1f5f9;border:1px solid #cbd5e1;border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer;color:#334155;margin-left:8px}
.retry-btn:hover{background:#e2e8f0}
</style><main><div class="panel"><h1>FacebookCleanup</h1><div class="muted" id="stats">Loading...</div><div class="bar"><div class="fill" id="fill"></div></div><div id="content"><div class="friend"><img id="avatar" class="avatar" alt="Profile photo"><div><div class="name" id="name"></div><a id="profile" href="#" target="_blank" rel="noopener noreferrer">Open Facebook profile ↗</a></div></div>

<div class="mutual-box" id="mutual-box">
  <div class="mutual-header">
    <span class="mutual-label">Mutual Friends</span>
    <span id="mutual-badge" class="badge"></span>
  </div>
  <div id="mutual-content" class="mutual-content"></div>
</div>

<div class="actions"><button class="keep" onclick="decide('keep')">✓ KEEP</button><button class="remove" onclick="decide('remove')">✕ REMOVE</button></div></div><div id="done" class="hidden"><h2>Review complete</h2><p>All candidate profiles have a recorded decision.</p></div><p><button class="undo" onclick="undo()">↶ Undo last decision</button></p><div class="note">Review mode only. REMOVE queues a decision; it does not unfriend, unfollow, or delete chats. Photos are shown only if previously captured. Verify the person in Facebook before deciding.</div></div></main><script>
const fallback='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110"><rect width="110" height="110" fill="#e8edf5"/><circle cx="55" cy="41" r="20" fill="#9aa8bc"/><ellipse cx="55" cy="105" rx="38" ry="38" fill="#9aa8bc"/></svg>');

let currentProfileUrl = null;
let mutualPollTimer = null;

function escapeHtml(s){
    if(!s) return '';
    return s.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m]);
}

async function refresh(){
    if (mutualPollTimer) {
        clearTimeout(mutualPollTimer);
        mutualPollTimer = null;
    }
    let r = await fetch('/api/next');
    let d = await r.json();
    document.getElementById('stats').textContent=`${d.reviewed} of ${d.total} reviewed · ${d.keep} kept · ${d.remove} marked for removal`;
    document.getElementById('fill').style.width=d.total?`${100*d.reviewed/d.total}%`:'0%';
    document.getElementById('content').classList.toggle('hidden',!d.friend);
    document.getElementById('done').classList.toggle('hidden',!!d.friend);

    if(d.friend){
        currentProfileUrl = d.friend.profile_url;
        document.getElementById('name').textContent=d.friend.name;
        document.getElementById('profile').href=d.friend.profile_url;
        let im=document.getElementById('avatar');
        im.src=d.friend.photo_url||fallback;
        im.onerror=()=>{im.onerror=null;im.src=fallback};
        loadMutualFriends(d.friend.profile_url);
    } else {
        currentProfileUrl = null;
    }
}

async function loadMutualFriends(url, force=false){
    if (mutualPollTimer) {
        clearTimeout(mutualPollTimer);
        mutualPollTimer = null;
    }
    const badge = document.getElementById('mutual-badge');
    const box = document.getElementById('mutual-content');
    
    badge.className = 'badge loading';
    badge.textContent = 'Checking...';
    box.innerHTML = '<div class="mutual-status-text"><span class="spinner"></span> Checking mutual friends on Facebook...</div>';

    try {
        let res = await fetch(`/api/mutual-friends?profile_url=${encodeURIComponent(url)}${force ? '&force=1' : ''}`);
        if (!res.ok) throw new Error('Request failed');
        let data = await res.json();

        if (currentProfileUrl !== url) return;

        if (data.status === 'loading') {
            mutualPollTimer = setTimeout(() => loadMutualFriends(url), 1500);
            return;
        }

        if (data.status === 'found') {
            badge.className = 'badge found';
            badge.textContent = `${data.friend_count} mutual`;
            if (!data.friends || data.friends.length === 0) {
                box.innerHTML = '<div class="mutual-status-text">Mutual friends found.</div>';
            } else {
                let html = '<div class="mutual-grid">';
                for (let f of data.friends) {
                    let photo = f.photo_url || fallback;
                    html += `<a class="mutual-card" href="${escapeHtml(f.profile_url)}" target="_blank" rel="noopener noreferrer">
                        <img class="mutual-thumb" src="${escapeHtml(photo)}" onerror="this.onerror=null;this.src='${fallback}'">
                        <div class="mutual-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</div>
                    </a>`;
                }
                html += '</div>';
                box.innerHTML = html;
            }
        } else if (data.status === 'none') {
            badge.className = 'badge none';
            badge.textContent = 'None';
            box.innerHTML = '<div class="mutual-status-text">No mutual friends found with this profile.</div>';
        } else if (data.status === 'unavailable') {
            badge.className = 'badge unavailable';
            badge.textContent = 'Unavailable';
            box.innerHTML = '<div class="mutual-status-text">🔒 Mutual friends information is hidden or unavailable on this profile.</div>';
        } else if (data.status === 'error') {
            badge.className = 'badge error';
            badge.textContent = 'Error';
            box.innerHTML = `<div class="mutual-status-text">⚠️ ${escapeHtml(data.error_message || 'Could not load mutual friends')}
                <button class="retry-btn" onclick="loadMutualFriends('${escapeHtml(url)}', true)">Retry</button></div>`;
        }
    } catch(err) {
        if (currentProfileUrl !== url) return;
        badge.className = 'badge error';
        badge.textContent = 'Error';
        box.innerHTML = `<div class="mutual-status-text">⚠️ Network or server error loading mutual friends.
            <button class="retry-btn" onclick="loadMutualFriends('${escapeHtml(url)}', true)">Retry</button></div>`;
    }
}

async function decide(decision){
    let r=await fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision})});
    if(!r.ok){alert('Could not save decision');return}
    refresh();
}

async function undo(){
    let r=await fetch('/api/undo',{method:'POST'});
    if(!r.ok){alert('Nothing to undo');return}
    refresh();
}

refresh();
</script></html>'''

@app.get('/')
def home():
    return render_template_string(PAGE)

@app.get('/api/next')
def next_friend():
    with connect() as db:
        counts = {r['decision']: r['n'] for r in db.execute('SELECT decision, COUNT(*) n FROM friends GROUP BY decision')}
        friend = db.execute("SELECT profile_url, name, photo_url FROM friends WHERE decision='pending' ORDER BY name COLLATE NOCASE, profile_url LIMIT 1").fetchone()
        total = sum(counts.values())
        return jsonify(
            total=total,
            reviewed=total - counts.get('pending', 0),
            keep=counts.get('keep', 0),
            remove=counts.get('remove', 0),
            friend=dict(friend) if friend else None
        )

@app.get('/api/mutual-friends')
def get_mutual_friends():
    profile_url = request.args.get('profile_url')
    force = request.args.get('force') in ('1', 'true', 'True')
    if not profile_url:
        return jsonify(error='Missing profile_url'), 400

    with connect() as db:
        if force:
            db.execute("DELETE FROM mutual_friend_cache WHERE profile_url=?", (profile_url,))
            db.execute("DELETE FROM mutual_friends WHERE target_profile_url=?", (profile_url,))
        else:
            cached = db.execute("SELECT status, error_message, friend_count, fetched_at FROM mutual_friend_cache WHERE profile_url=?", (profile_url,)).fetchone()
            if cached:
                friends = []
                if cached['status'] == 'found':
                    rows = db.execute("SELECT mutual_profile_url AS profile_url, name, photo_url FROM mutual_friends WHERE target_profile_url=? ORDER BY id ASC", (profile_url,)).fetchall()
                    friends = [dict(r) for r in rows]
                return jsonify(
                    profile_url=profile_url,
                    status=cached['status'],
                    error_message=cached['error_message'],
                    friend_count=cached['friend_count'],
                    fetched_at=cached['fetched_at'],
                    friends=friends
                )

    # Enqueue background fetch if not already running
    worker.enqueue(profile_url)
    return jsonify(profile_url=profile_url, status='loading', friends=[])

@app.post('/api/decision')
def decision():
    value = (request.get_json(silent=True) or {}).get('decision')
    if value not in ('keep', 'remove'):
        return jsonify(error='Invalid decision'), 400
    with connect() as db:
        row = db.execute("SELECT profile_url FROM friends WHERE decision='pending' ORDER BY name COLLATE NOCASE, profile_url LIMIT 1").fetchone()
        if not row:
            return jsonify(error='No pending profiles'), 409
        db.execute("UPDATE friends SET decision=?, reviewed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE profile_url=? AND decision='pending'", (value, row['profile_url']))
    return jsonify(ok=True)

@app.post('/api/undo')
def undo():
    with connect() as db:
        row = db.execute("SELECT profile_url FROM friends WHERE decision!='pending' ORDER BY reviewed_at DESC LIMIT 1").fetchone()
        if not row:
            return jsonify(error='No decision to undo'), 409
        db.execute("UPDATE friends SET decision='pending', reviewed_at=NULL WHERE profile_url=?", (row['profile_url'],))
    return jsonify(ok=True)

if __name__ == '__main__':
    init()
    print('Open http://127.0.0.1:8765 in your browser')
    app.run(host="127.0.0.1", port=8765, debug=False)
