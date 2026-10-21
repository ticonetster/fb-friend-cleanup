"""Automated Unfollow Runner.
Safely processes profiles & pages marked with decision='unfollow' in SQLite `following` table.
Executes Unfollow action on their Facebook page with anti-abuse rate limits, jitter, and safety checks.
"""

import argparse
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright


def get_root():
    cwd = Path.cwd()
    if (cwd / 'friends.sqlite3').exists() or (cwd / 'browser_profile').exists():
        return cwd
    return Path(__file__).resolve().parent


ROOT = get_root()
DB = ROOT / 'friends.sqlite3'
PROFILE = ROOT / 'browser_profile'


def connect():
    db = sqlite3.connect(DB, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout = 5000')
    return db


def init_unfollow_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS unfollow_log (
            profile_url TEXT PRIMARY KEY,
            name TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'unfollowed', 'failed', 'skipped')),
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            unfollowed_at TEXT,
            created_at TEXT NOT NULL
        )''')


def sync_pending_targets():
    """Ensure any profile marked 'unfollow' in following is queued in unfollow_log."""
    with connect() as db:
        db.execute('''
            DELETE FROM unfollow_log
            WHERE status = 'pending' AND profile_url IN (
                SELECT profile_url FROM following WHERE decision = 'keep'
            )
        ''')
        db.execute('''
            INSERT INTO unfollow_log (profile_url, name, status, created_at)
            SELECT f.profile_url, f.name, 'pending', ?
            FROM following f
            LEFT JOIN unfollow_log u ON f.profile_url = u.profile_url
            WHERE f.decision = 'unfollow' AND u.profile_url IS NULL
        ''', (datetime.now(timezone.utc).isoformat(),))


def get_unfollow_stats():
    with connect() as db:
        counts = {r['status']: r['n'] for r in db.execute(
            'SELECT status, COUNT(*) n FROM unfollow_log GROUP BY status')}
        queued = db.execute("SELECT COUNT(*) FROM following WHERE decision = 'unfollow'").fetchone()[0]
        return {
            'queued_total': queued,
            'pending': counts.get('pending', 0),
            'unfollowed': counts.get('unfollowed', 0),
            'failed': counts.get('failed', 0),
            'skipped': counts.get('skipped', 0)
        }


def safe_goto(page, url: str, wait_timeout: int = 15000):
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=wait_timeout)
    except Exception as e:
        if 'Timeout' in type(e).__name__:
            print(f"    [safe_goto] domcontentloaded timeout absorbed for {url}")
        else:
            raise
    time.sleep(2)


def check_warning_or_block(page) -> str | None:
    suspicious_phrases = [
        "Please slow down",
        "You're temporarily blocked",
        "We restrict certain activity",
        "You’re temporarily blocked",
        "Try again later"
    ]
    for phrase in suspicious_phrases:
        if page.get_by_text(phrase, exact=False).first.is_visible():
            return phrase
    return None


def execute_unfollow_on_profile(page, profile_url: str, dry_run: bool = False) -> tuple[bool, str]:
    """Navigates to profile_url and clicks Following -> Unfollow."""
    safe_goto(page, profile_url)

    warning = check_warning_or_block(page)
    if warning:
        return False, f"CIRCUIT_BREAKER: Facebook warning banner: '{warning}'"

    # Check if already unfollowed (button says 'Follow')
    follow_btn = page.locator('div[role="button"]:has-text("Follow"), button:has-text("Follow")').first
    following_btn = page.locator('div[role="button"][aria-label="Following"], div[role="button"]:has-text("Following"), button:has-text("Following")').first

    # Also handle "Followed" or icon buttons
    if not following_btn.is_visible(timeout=3000):
        # Look for alternative aria labels
        following_btn = page.locator('div[role="button"][aria-label*="Following"], div[role="button"][title*="Following"]').first

    if not following_btn.is_visible(timeout=2000):
        if follow_btn.is_visible(timeout=1000):
            print("    [Info] Profile already shows 'Follow' (already unfollowed).")
            return True, "already_unfollowed"
        return False, "Could not find 'Following' button on profile header"

    if dry_run:
        print(f"    [DRY-RUN] Found 'Following' button for {profile_url}. (Not clicking)")
        return True, "dry_run"

    print("    Clicking 'Following' button...")
    following_btn.click()
    time.sleep(1.5)

    # In the dropdown / popover menu, find "Unfollow"
    unfollow_menu_option = page.locator('[role="menuitem"]:has-text("Unfollow"), div[role="button"]:has-text("Unfollow"), span:has-text("Unfollow")').first

    if not unfollow_menu_option.is_visible(timeout=4000):
        # On some page types, clicking "Following" toggles it immediately to "Follow"
        time.sleep(1)
        if follow_btn.is_visible(timeout=2000):
            print("    Direct toggle succeeded (button now says 'Follow').")
            return True, "toggled"
        return False, "Dropdown opened but 'Unfollow' menu option was not found"

    print("    Selecting 'Unfollow'...")
    unfollow_menu_option.click()
    time.sleep(2)

    # Check if a confirmation dialog appeared (e.g. "Are you sure you want to unfollow?")
    confirm_btn = page.locator('[role="dialog"] div[role="button"]:has-text("Update"), [role="dialog"] div[role="button"]:has-text("Confirm"), [role="dialog"] div[role="button"]:has-text("Unfollow")').first
    if confirm_btn.is_visible(timeout=2000):
        print("    Confirming unfollow dialog...")
        confirm_btn.click()
        time.sleep(1.5)

    return True, "unfollowed"


def run_batch(batch_size: int, delay_min: int, delay_max: int, single: bool, dry_run: bool, headless: bool):
    init_unfollow_table()
    sync_pending_targets()

    stats = get_unfollow_stats()
    print("=== Facebook Following Cleanup Runner ===")
    print(f"Queued to unfollow: {stats['queued_total']}")
    print(f"Pending execution:  {stats['pending']}")
    print(f"Already unfollowed: {stats['unfollowed']}")
    print(f"Failed / retry:     {stats['failed']}")
    print("=========================================\n")

    if stats['pending'] == 0:
        print("No pending unfollow profiles queued! Use the review UI to mark profiles to unfollow first.")
        return

    limit = 1 if single else batch_size
    with connect() as db:
        candidates = db.execute('''
            SELECT profile_url, name, attempts
            FROM unfollow_log
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT ?
        ''', (limit,)).fetchall()

    if not candidates:
        print("No eligible candidates found.")
        return

    print(f"Preparing to process {len(candidates)} profile(s)... (Dry Run: {dry_run}, Headless: {headless})\n")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=headless,
            viewport={'width': 1280, 'height': 800}
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        # Initial check
        print("Checking session validity...")
        safe_goto(page, 'https://www.facebook.com/')
        if "login" in page.url.lower():
            print("Session expired or logged out! Run 'python test_facebook.py' to re-authenticate.")
            browser.close()
            return

        processed = 0
        circuit_broken = False

        for idx, row in enumerate(candidates, 1):
            url = row['profile_url']
            name = row['name']
            attempts = (row['attempts'] or 0) + 1
            print(f"[{idx}/{len(candidates)}] Processing: {name} ({url})")

            try:
                ok, status_msg = execute_unfollow_on_profile(page, url, dry_run=dry_run)
            except Exception as e:
                ok = False
                status_msg = f"Exception: {str(e)[:150]}"

            now_iso = datetime.now(timezone.utc).isoformat()

            if "CIRCUIT_BREAKER" in status_msg:
                print(f"\n[ALERT] {status_msg}")
                print("Halting execution immediately to protect account.")
                with connect() as db:
                    db.execute('''
                        UPDATE unfollow_log
                        SET attempts = ?, last_error = ?
                        WHERE profile_url = ?
                    ''', (attempts, status_msg, url))
                circuit_broken = True
                break

            if ok:
                print(f"  ✓ Success ({status_msg})")
                if not dry_run:
                    with connect() as db:
                        db.execute('''
                            UPDATE unfollow_log
                            SET status = 'unfollowed', unfollowed_at = ?, attempts = ?, last_error = NULL
                            WHERE profile_url = ?
                        ''', (now_iso, attempts, url))
                processed += 1
            else:
                print(f"  ✕ Failed: {status_msg}")
                if not dry_run:
                    with connect() as db:
                        db.execute('''
                            UPDATE unfollow_log
                            SET status = 'failed', attempts = ?, last_error = ?
                            WHERE profile_url = ?
                        ''', (attempts, status_msg, url))

            # Jitter between actions if more remain
            if idx < len(candidates):
                pause = random.uniform(delay_min, delay_max)
                print(f"  Pacing delay: {pause:.1f}s...")
                time.sleep(pause)

                # Micro-cooldown every 10 actions
                if idx % 10 == 0:
                    cooldown = random.uniform(25.0, 35.0)
                    print(f"  [Safety cooldown] Pausing for {cooldown:.1f}s...")
                    time.sleep(cooldown)

        browser.close()

    print("\n=== Batch Finished ===")
    print(f"Processed: {processed} profile(s).")
    latest = get_unfollow_stats()
    print(f"Remaining pending: {latest['pending']}")
    print(f"Total unfollowed:  {latest['unfollowed']}")
    if circuit_broken:
        print("[WARNING] Stopped early due to Facebook circuit breaker.")


def main():
    parser = argparse.ArgumentParser(description="Facebook Following Unfollow Runner")
    parser.add_argument("--batch-size", type=int, default=30, help="Number of profiles to unfollow per batch (default: 30)")
    parser.add_argument("--delay-min", type=int, default=8, help="Minimum seconds delay between actions (default: 8)")
    parser.add_argument("--delay-max", type=int, default=18, help="Maximum seconds delay between actions (default: 18)")
    parser.add_argument("--single", action="store_true", help="Process only 1 profile for live testing")
    parser.add_argument("--dry-run", action="store_true", help="Inspect profile without clicking Unfollow")
    parser.add_argument("--headless", action="store_true", help="Run browser in background headless mode")
    args = parser.parse_args()

    run_batch(
        batch_size=args.batch_size,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        single=args.single,
        dry_run=args.dry_run,
        headless=args.headless
    )


if __name__ == '__main__':
    main()

