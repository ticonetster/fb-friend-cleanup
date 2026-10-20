import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, jsonify, request, render_template_string
from playwright.sync_api import sync_playwright

def get_root():
    cwd = Path.cwd()
    if (cwd / 'friends.sqlite3').exists() or (cwd / 'browser_profile').exists():
        return cwd
    return Path(__file__).resolve().parent

ROOT = get_root()
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
            friend_count INTEGER,
            fetched_at TEXT NOT NULL
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS mutual_friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_profile_url TEXT NOT NULL,
            mutual_profile_url TEXT NOT NULL,
            name TEXT NOT NULL,
            photo_url TEXT,
            created_at TEXT NOT NULL
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_mutual_target ON mutual_friends(target_profile_url)')

        # Following table
        db.execute('''CREATE TABLE IF NOT EXISTS following (
            profile_url TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            photo_url TEXT,
            decision TEXT NOT NULL DEFAULT 'pending'
                CHECK(decision IN ('pending', 'keep', 'unfollow')),
            reviewed_at TEXT
        )''')


class MutualFriendWorker:
    """Background worker that continuously scrapes mutual friends for pending profiles."""
    def __init__(self, db_path, profile_path):
        self.db_path = db_path
        self.profile_path = profile_path
        self.lock = threading.Lock()
        self.queue = []
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def enqueue(self, profile_url):
        with self.lock:
            if profile_url not in self.queue:
                self.queue.insert(0, profile_url)

    def _normalize_mutual_url(self, raw_url):
        try:
            p = urlsplit(raw_url)
            if p.hostname not in {'www.facebook.com', 'facebook.com', 'm.facebook.com'}:
                return None
            parts = [x for x in p.path.split('/') if x]
            if not parts:
                return None
            if parts[0] == 'profile.php':
                return raw_url.split('&')[0]
            if len(parts) == 1 or (len(parts) > 1 and parts[1] in {'friends', 'friends_mutual'}):
                slug = parts[0].lower()
                if slug in {'friends', 'groups', 'watch', 'marketplace', 'messages', 'notifications', 'settings', 'gaming', 'events', 'pages', 'reel', 'reels', 'photo', 'photos', 'videos'}:
                    return None
                return f"https://www.facebook.com/{parts[0]}"
            return None
        except Exception:
            return None

    def _scrape_profile(self, page, target_url):
        clean_url = target_url.rstrip('/')
        if 'profile.php' in clean_url:
            mutual_url = clean_url + '&sk=friends_mutual'
        else:
            mutual_url = clean_url + '/friends_mutual'

        try:
            page.goto(mutual_url, wait_until='domcontentloaded', timeout=15000)
            time.sleep(2)
        except Exception as e:
            return {'status': 'error', 'error_message': f"Navigation timeout: {str(e)[:100]}", 'count': 0, 'friends': []}

        # Check for unavailable or hidden mutual friends
        body_text = page.locator('body').inner_text()
        if "isn't available" in body_text or "This content isn't available right now" in body_text:
            return {'status': 'unavailable', 'error_message': 'Content not available or private', 'count': 0, 'friends': []}

        # Check if we were redirected back to the base profile or feed
        if page.url.rstrip('/') == clean_url or '/friends_mutual' not in page.url and 'sk=friends_mutual' not in page.url:
            try:
                page.goto(clean_url + '/friends', wait_until='domcontentloaded', timeout=15000)
                time.sleep(2)
            except Exception:
                pass

        # Attempt to parse mutual friend cards
        cards = page.locator('div[data-visualcompletion="ignore-dynamic-rendering"]').evaluate_all('''elements => {
            let results = [];
            for (let el of elements) {
                let a = el.querySelector('a[href*="/"]');
                let img = el.querySelector('img');
                if (a && a.innerText && a.innerText.trim().length > 0 && !a.href.includes('/friends_mutual')) {
                    let name = a.innerText.trim().split('\\n')[0];
                    let photo = img ? img.src : null;
                    results.push({ name: name, url: a.href, photo_url: photo });
                }
            }
            return results;
        }''')

        if not cards:
            cards = page.locator('a[href]').evaluate_all('''elements => {
                let results = [];
                for (let a of elements) {
                    let href = a.href || '';
                    let text = (a.innerText || '').trim();
                    let img = a.querySelector('img');
                    if (img && text && text.length > 1 && !text.includes('\\n') && !href.includes('friends_mutual')) {
                        results.push({ name: text, url: href, photo_url: img.src });
                    }
                }
                return results;
            }''')

        friends_dedup = {}
        for c in cards:
            norm = self._normalize_mutual_url(c['url'])
            if norm and norm != clean_url and norm not in friends_dedup:
                name = c['name'].strip()
                if len(name) > 1 and len(name) < 70 and not any(x in name.lower() for x in ['friend', 'mutual', 'see all', 'message', 'add']):
                    friends_dedup[norm] = {
                        'mutual_profile_url': norm,
                        'name': name,
                        'photo_url': c.get('photo_url')
                    }

        friends_list = list(friends_dedup.values())
        if friends_list:
            return {'status': 'found', 'error_message': None, 'count': len(friends_list), 'friends': friends_list}

        # Check if page explicitly indicates 0 mutual friends
        if "0 mutual friends" in body_text or "No mutual friends" in body_text:
            return {'status': 'none', 'error_message': None, 'count': 0, 'friends': []}

        header_match = page.locator('span:has-text("mutual friend"), h2:has-text("mutual friend"), a:has-text("mutual friend")').first
        if header_match.is_visible():
            t = header_match.inner_text().strip()
            import re
            m = re.search(r'(\d+)\s+mutual', t)
            if m:
                cnt = int(m.group(1))
                return {'status': 'found', 'error_message': None, 'count': cnt, 'friends': []}

        return {'status': 'none', 'error_message': None, 'count': 0, 'friends': []}

    def _run(self):
        with sync_playwright() as p:
            browser = None
            page = None
            while self.running:
                target_url = None
                with self.lock:
                    if self.queue:
                        target_url = self.queue.pop(0)

                if not target_url:
                    with sqlite3.connect(self.db_path) as db:
                        db.row_factory = sqlite3.Row
                        row = db.execute('''
                            SELECT f.profile_url FROM friends f
                            LEFT JOIN mutual_friend_cache c ON f.profile_url = c.profile_url
                            WHERE f.decision = 'pending' AND c.profile_url IS NULL
                            LIMIT 1
                        ''').fetchone()
                        if row:
                            target_url = row['profile_url']

                if not target_url:
                    time.sleep(1.0)
                    continue

                if browser is None:
                    try:
                        browser = p.chromium.launch_persistent_context(
                            user_data_dir=str(self.profile_path),
                            headless=True,
                            args=['--disable-blink-features=AutomationControlled']
                        )
                        page = browser.new_page()
                    except Exception as e:
                        print(f"[Worker] Could not launch browser: {e}")
                        time.sleep(5)
                        continue

                try:
                    res = self._scrape_profile(page, target_url)
                    now = datetime.now(timezone.utc).isoformat()
                    with sqlite3.connect(self.db_path) as db:
                        db.execute('''
                            INSERT OR REPLACE INTO mutual_friend_cache (profile_url, status, error_message, friend_count, fetched_at)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (target_url, res['status'], res['error_message'], res['count'], now))

                        if res['friends']:
                            db.execute('DELETE FROM mutual_friends WHERE target_profile_url = ?', (target_url,))
                            db.executemany('''
                                INSERT INTO mutual_friends (target_profile_url, mutual_profile_url, name, photo_url, created_at)
                                VALUES (?, ?, ?, ?, ?)
                            ''', [(target_url, f['mutual_profile_url'], f['name'], f['photo_url'], now) for f in res['friends']])
                except Exception as e:
                    print(f"[Worker] Error scraping {target_url}: {e}")
                time.sleep(1.0)

            if browser:
                browser.close()


init()
worker = MutualFriendWorker(DB, PROFILE)

PAGE = '''<!doctype html><html><head><meta charset="utf-8"><title>FacebookCleanup</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;background:#f3f6fb;color:#0f172a}
main{max-width:700px;margin:36px auto;padding:20px}
.panel{background:white;border-radius:18px;padding:30px;box-shadow:0 10px 35px #1c34501a}
h1{font-size:25px;margin:0 0 10px}
.muted{color:#667085}

/* Mode Tabs */
.tabs{display:flex;gap:10px;margin-bottom:22px;border-bottom:2px solid #e2e8f0;padding-bottom:12px}
.tab-btn{background:#f1f5f9;border:1px solid #cbd5e1;border-radius:8px;padding:8px 16px;font-weight:600;font-size:14px;cursor:pointer;color:#475569;transition:all .15s}
.tab-btn:hover{background:#e2e8f0}
.tab-btn.active{background:#2563eb;color:white;border-color:#2563eb;box-shadow:0 2px 6px #2563eb33}

.friend{display:flex;gap:22px;align-items:center;margin:25px 0 20px}
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
</style>
<main>
<div class="panel">
  <h1>FacebookCleanup</h1>

  <div class="tabs">
    <button id="tab-friends" class="tab-btn active" onclick="switchMode('friends')">👥 Friends Review</button>
    <button id="tab-following" class="tab-btn" onclick="switchMode('following')">🔔 Following Review</button>
  </div>

  <div class="muted" id="stats">Loading...</div>
  <div class="bar"><div class="fill" id="fill"></div></div>

  <div id="content">
    <div class="friend">
      <img id="avatar" class="avatar" alt="Profile photo">
      <div>
        <div class="name" id="name"></div>
        <a id="profile" href="#" target="_blank" rel="noopener noreferrer">Open Facebook profile ↗</a>
      </div>
    </div>

    <!-- Mutual Friends Box (Friends mode only) -->
    <div class="mutual-box" id="mutual-box">
      <div class="mutual-header">
        <span class="mutual-label">Mutual Friends</span>
        <span id="mutual-badge" class="badge"></span>
      </div>
      <div id="mutual-content" class="mutual-content"></div>
    </div>

    <div class="actions">
      <button class="keep" id="btn-keep" onclick="handleKeep()">✓ KEEP</button>
      <button class="remove" id="btn-remove" onclick="handleRemove()">✕ REMOVE</button>
    </div>
  </div>

  <div id="done" class="hidden">
    <h2>Review complete</h2>
    <p id="done-message">All candidate profiles have a recorded decision.</p>
  </div>

  <p><button class="undo" onclick="handleUndo()">↶ Undo last decision</button></p>
  <div class="note" id="mode-note">Review mode only. REMOVE queues a decision; it does not unfriend, unfollow, or delete chats. Verify the person in Facebook before deciding.</div>
</div>
</main>

<script>
const fallback='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="110" height="110"><rect width="110" height="110" fill="#e8edf5"/><circle cx="55" cy="41" r="20" fill="#9aa8bc"/><ellipse cx="55" cy="105" rx="38" ry="38" fill="#9aa8bc"/></svg>');

let currentMode = 'friends';
let currentProfileUrl = null;
let mutualPollTimer = null;

function escapeHtml(s){
    if(!s) return '';
    return s.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m]);
}

function switchMode(mode) {
    if (currentMode === mode) return;
    currentMode = mode;
    document.getElementById('tab-friends').classList.toggle('active', mode === 'friends');
    document.getElementById('tab-following').classList.toggle('active', mode === 'following');

    const mutualBox = document.getElementById('mutual-box');
    const btnKeep = document.getElementById('btn-keep');
    const btnRemove = document.getElementById('btn-remove');
    const modeNote = document.getElementById('mode-note');

    if (mode === 'following') {
        mutualBox.classList.add('hidden');
        btnKeep.textContent = '✓ KEEP FOLLOWING';
        btnRemove.textContent = '✕ UNFOLLOW';
        modeNote.textContent = "Review mode only. UNFOLLOW queues a decision; it does not unfollow until you run 'unfollow_runner.py' or 'fb-unfollow-runner'.";
    } else {
        mutualBox.classList.remove('hidden');
        btnKeep.textContent = '✓ KEEP';
        btnRemove.textContent = '✕ REMOVE';
        modeNote.textContent = "Review mode only. REMOVE queues a decision; it does not unfriend, unfollow, or delete chats until runner execution.";
    }
    refresh();
}

async function refresh(){
    if (mutualPollTimer) {
        clearTimeout(mutualPollTimer);
        mutualPollTimer = null;
    }

    const endpoint = currentMode === 'friends' ? '/api/next' : '/api/following/next';
    let r = await fetch(endpoint);
    let d = await r.json();

    if (currentMode === 'friends') {
        document.getElementById('stats').textContent=`${d.reviewed} of ${d.total} reviewed · ${d.keep} kept · ${d.remove} marked for removal`;
    } else {
        document.getElementById('stats').textContent=`${d.reviewed} of ${d.total} reviewed · ${d.keep} kept · ${d.unfollow} marked to unfollow`;
    }

    document.getElementById('fill').style.width = d.total ? `${100*d.reviewed/d.total}%` : '0%';
    document.getElementById('content').classList.toggle('hidden', !d.friend);
    document.getElementById('done').classList.toggle('hidden', !!d.friend);

    if (d.friend) {
        currentProfileUrl = d.friend.profile_url;
        document.getElementById('name').textContent = d.friend.name;
        document.getElementById('profile').href = d.friend.profile_url;
        let im = document.getElementById('avatar');
        im.src = d.friend.photo_url || fallback;
        im.onerror = () => { im.onerror = null; im.src = fallback; };

        if (currentMode === 'friends') {
            loadMutualFriends(d.friend.profile_url);
        }
    } else {
        currentProfileUrl = null;
        document.getElementById('done-message').textContent = currentMode === 'friends'
            ? 'All candidate friend profiles have a recorded decision.'
            : 'All followed profiles and pages have a recorded decision.';
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

        if (currentProfileUrl !== url || currentMode !== 'friends') return;

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
        if (currentProfileUrl !== url || currentMode !== 'friends') return;
        badge.className = 'badge error';
        badge.textContent = 'Error';
        box.innerHTML = `<div class="mutual-status-text">⚠️ Network or server error loading mutual friends.
            <button class="retry-btn" onclick="loadMutualFriends('${escapeHtml(url)}', true)">Retry</button></div>`;
    }
}

async function handleKeep() {
    if (currentMode === 'friends') {
        await decideFriends('keep');
    } else {
        await decideFollowing('keep');
    }
}

async function handleRemove() {
    if (currentMode === 'friends') {
        await decideFriends('remove');
    } else {
        await decideFollowing('unfollow');
    }
}

async function handleUndo() {
    if (currentMode === 'friends') {
        await undoFriends();
    } else {
        await undoFollowing();
    }
}

async function decideFriends(decision){
    let r = await fetch('/api/decision', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({decision})});
    if (!r.ok) { alert('Could not save decision'); return; }
    refresh();
}

async function undoFriends(){
    let r = await fetch('/api/undo', {method:'POST'});
    if (!r.ok) { alert('Nothing to undo'); return; }
    refresh();
}

async function decideFollowing(decision){
    let r = await fetch('/api/following/decision', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({decision})});
    if (!r.ok) { alert('Could not save decision'); return; }
    refresh();
}

async function undoFollowing(){
    let r = await fetch('/api/following/undo', {method:'POST'});
    if (!r.ok) { alert('Nothing to undo'); return; }
    refresh();
}

// Keyboard shortcuts: K (Keep), R/U (Remove/Unfollow), Z/Backspace (Undo)
document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    const key = e.key.toLowerCase();
    if (key === 'k') {
        handleKeep();
    } else if (key === 'r' || key === 'u') {
        handleRemove();
    } else if (key === 'z') {
        handleUndo();
    }
});

refresh();
</script></html>'''

@app.get('/')
def home():
    return render_template_string(PAGE)

# ----------------- Friends Endpoints -----------------

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

# ----------------- Following Endpoints -----------------

@app.get('/api/following/next')
def next_following():
    with connect() as db:
        counts = {r['decision']: r['n'] for r in db.execute('SELECT decision, COUNT(*) n FROM following GROUP BY decision')}
        row = db.execute("SELECT profile_url, name, photo_url FROM following WHERE decision='pending' ORDER BY name COLLATE NOCASE, profile_url LIMIT 1").fetchone()
        total = sum(counts.values())
        return jsonify(
            total=total,
            reviewed=total - counts.get('pending', 0),
            keep=counts.get('keep', 0),
            unfollow=counts.get('unfollow', 0),
            friend=dict(row) if row else None
        )

@app.post('/api/following/decision')
def following_decision():
    value = (request.get_json(silent=True) or {}).get('decision')
    if value not in ('keep', 'unfollow'):
        return jsonify(error='Invalid decision'), 400
    with connect() as db:
        row = db.execute("SELECT profile_url FROM following WHERE decision='pending' ORDER BY name COLLATE NOCASE, profile_url LIMIT 1").fetchone()
        if not row:
            return jsonify(error='No pending profiles'), 409
        db.execute("UPDATE following SET decision=?, reviewed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE profile_url=? AND decision='pending'", (value, row['profile_url']))
    return jsonify(ok=True)

@app.post('/api/following/undo')
def following_undo():
    with connect() as db:
        row = db.execute("SELECT profile_url FROM following WHERE decision!='pending' ORDER BY reviewed_at DESC LIMIT 1").fetchone()
        if not row:
            return jsonify(error='No decision to undo'), 409
        db.execute("UPDATE following SET decision='pending', reviewed_at=NULL WHERE profile_url=?", (row['profile_url'],))
    return jsonify(ok=True)


def main():
    init()
    print('Open http://127.0.0.1:8765 in your browser')
    app.run(host="127.0.0.1", port=8765, debug=False)

if __name__ == '__main__':
    main()
