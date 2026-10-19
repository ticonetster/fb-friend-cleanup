"""Facebook Friend, Chat & Follow Removal Runner.

Safely executes:
1. Unfollow
2. Delete Messenger chat / conversation (if any)
3. Unfriend
in controlled batches with conservative rate-limiting, warning detection,
and persistent SQLite tracking.
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


def init_removal_table():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS removal_log (
            profile_url TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'removed', 'failed', 'skipped')),
            unfollow_status TEXT NOT NULL DEFAULT 'pending',
            chat_status TEXT NOT NULL DEFAULT 'pending',
            unfriend_status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            removed_at TEXT,
            created_at TEXT NOT NULL
        )''')
        cols = {r['name'] for r in db.execute('PRAGMA table_info(removal_log)')}
        if 'unfollow_status' not in cols:
            db.execute("ALTER TABLE removal_log ADD COLUMN unfollow_status TEXT NOT NULL DEFAULT 'pending'")

        now = datetime.now(timezone.utc).isoformat()
        db.execute('''INSERT OR IGNORE INTO removal_log (profile_url, name, status, created_at)
                      SELECT profile_url, name, 'pending', ?
                      FROM friends WHERE decision='remove' ''', (now,))
        db.commit()


def get_pending_count():
    with connect() as db:
        row = db.execute("SELECT COUNT(*) as n FROM removal_log WHERE status='pending'").fetchone()
        return row['n'] if row else 0


def get_removal_stats():
    with connect() as db:
        rows = db.execute("SELECT status, COUNT(*) as n FROM removal_log GROUP BY status").fetchall()
        return {r['status']: r['n'] for r in rows}


def check_for_warnings(page):
    """Returns warning message if Facebook displays a rate-limit, action block, or checkpoint."""
    text = page.inner_text("body") if page else ""
    warnings = [
        "misusing this feature by going too fast",
        "temporarily blocked from using it",
        "You're temporarily blocked",
        "temporarily blocked",
        "Please slow down",
        "Account restricted",
        "Your account is restricted",
        "We limit how often you can post, comment or do other things",
        "Action blocked"
    ]
    for w in warnings:
        if w.lower() in text.lower():
            return w
    # Check for login requirement
    if ("log into facebook" in text.lower() or "forgot password?" in text.lower()) and "log in" in text.lower():
        if page.locator('input[type="password"]').count() > 0:
            return "Session logged out / Login required"
    return None


def safe_goto(page, url, timeout=30000):
    """Navigates safely, catching and handling server/client redirects (e.g. 2FA, remember browser)
    and network/telemetry timeouts without crashing.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as e:
        err_str = str(e).lower()
        if "interrupted by another navigation" in err_str or "interrupted" in err_str:
            # Facebook redirected while loading; wait for redirected page
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
        elif "timeout" in err_str:
            # Facebook telemetry/media held connection; DOM is usually interactive
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        else:
            raise
    page.wait_for_timeout(1500)


def ensure_logged_in(page):
    """Verifies that Facebook is logged in; if not, prompts the user to log in or handle 2FA."""
    safe_goto(page, "https://www.facebook.com/", timeout=30000)

    for _ in range(3):
        current_url = page.url.lower()
        warning = check_for_warnings(page)
        is_2fa = any(x in current_url for x in ["two_factor", "checkpoint", "remember_browser", "recover/code", "login/device-based"])
        is_logged_out = (warning and "login" in warning.lower()) or ("login" in current_url and "facebook.com" in current_url)

        if is_2fa or is_logged_out:
            print("\n========================================================")
            if is_2fa:
                print("  ACTION REQUIRED: Facebook 2FA / Remember Browser detected.")
                print(f"  Current URL: {page.url}")
                print("  Please complete the prompt in the open browser window.")
            else:
                print("  NOTICE: Facebook session in browser_profile is logged out.")
                print("  A browser window is open. Please log into Facebook.")
            print("========================================================\n")
            input("Press Enter here once you have finished in the browser and reached Facebook home... ")
            safe_goto(page, "https://www.facebook.com/", timeout=30000)
        else:
            break

    # Final verification
    current_url = page.url.lower()
    warning = check_for_warnings(page)
    if (warning and "login" in warning.lower()) or "two_factor" in current_url or "checkpoint" in current_url:
        print(f"ERROR: Still not at Facebook home (URL: {page.url}). Aborting.")
        return False

    print(f"Session verified: Logged into Facebook ({page.url}).")
    return True


def unfollow_profile(page, target_url, dry_run=False):
    """Checks follow status and unfollows the profile if currently followed.
    Returns: 'unfollowed', 'already_unfollowed', or 'failed: <reason>'
    """
    try:
        # Check 1: Standalone "Following" button
        following_btn = page.locator('div[aria-label="Following"][role="button"], div[role="button"]:has-text("Following")').first
        if following_btn.count() > 0 and following_btn.is_visible():
            if dry_run:
                print("    [DRY RUN] Would click 'Following' button to unfollow")
                return "dry_run_unfollowed"
            following_btn.click()
            page.wait_for_timeout(1000)
            # Check for dropdown menu item "Unfollow"
            unfollow_menu_opt = page.locator('div[role="menuitem"]:has-text("Unfollow"), span:has-text("Unfollow")').first
            if unfollow_menu_opt.count() > 0 and unfollow_menu_opt.is_visible():
                unfollow_menu_opt.click()
                page.wait_for_timeout(1500)
            return "unfollowed"

        # Check 2: Inside "Friends" dropdown menu
        friends_btn = page.locator('div[aria-label="Friends"][role="button"], div[aria-label*="Friends"][role="button"], div[role="button"]:has-text("Friends")').first
        if friends_btn.count() > 0 and friends_btn.is_visible():
            friends_btn.click()
            page.wait_for_timeout(1200)

            # Check if "Unfollow" option exists in the menu
            unfollow_opt = page.locator('div[role="menuitem"]:has-text("Unfollow"), span:has-text("Unfollow")').first
            if unfollow_opt.count() > 0 and unfollow_opt.is_visible():
                if dry_run:
                    print("    [DRY RUN] Would click 'Unfollow' in Friends menu")
                    page.keyboard.press("Escape")
                    return "dry_run_unfollowed"
                unfollow_opt.click()
                page.wait_for_timeout(1500)
                return "unfollowed"
            else:
                # If "Follow" is in the menu instead, they are already unfollowed
                page.keyboard.press("Escape")
                return "already_unfollowed"

        return "already_unfollowed"

    except Exception as e:
        return f"failed: {str(e)[:100]}"


def delete_chat(page, target_url, dry_run=False):
    """Navigates to the person's profile, opens chat, and deletes the conversation if present.
    Returns: 'deleted', 'no_chat', or 'failed: <reason>'
    """
    try:
        # Check if Message button exists on profile
        message_btn = page.locator('div[aria-label="Message"][role="button"], a[aria-label="Message"], div[role="button"]:has-text("Message")').first
        if message_btn.count() == 0 or not message_btn.is_visible():
            return "no_chat_button"

        # Click Message to open chat
        message_btn.click(timeout=6000)
        page.wait_for_timeout(2500)

        # In chat dock header, look for options menu or gear
        chat_menu_selectors = [
            'div[aria-label="Chat settings"][role="button"]',
            'div[aria-label*="Chat settings"]',
            'div[aria-label="Conversation settings"][role="button"]',
            'div[aria-label="More options"][role="button"]',
            'div[aria-label="Options"][role="button"]'
        ]
        chat_menu = None
        for sel in chat_menu_selectors:
            menu = page.locator(sel).first
            if menu.count() > 0 and menu.is_visible():
                chat_menu = menu
                break

        if not chat_menu:
            page.keyboard.press("Escape")
            return "no_chat"

        chat_menu.click(timeout=6000)
        page.wait_for_timeout(1200)

        # Look for "Delete chat" in popup menu
        delete_opt = page.locator('div[role="menuitem"]:has-text("Delete chat")').first
        if delete_opt.count() == 0 or not delete_opt.is_visible():
            delete_opt = page.locator('div[role="menuitem"]:has-text("Delete conversation")').first

        if delete_opt.count() == 0 or not delete_opt.is_visible():
            page.keyboard.press("Escape")
            return "no_chat"

        if dry_run:
            print("    [DRY RUN] Would click 'Delete chat' and confirm dialog")
            page.keyboard.press("Escape")
            return "dry_run_deleted"

        delete_opt.click(timeout=6000)
        page.wait_for_timeout(1500)

        # Confirm modal "Delete chat"
        confirm_btn = page.locator('div[aria-label="Delete chat"][role="button"], div[role="button"]:has-text("Delete chat"), button:has-text("Delete chat")')
        try:
            confirm_btn.first.wait_for(state="visible", timeout=4000)
            confirm_btn.first.click(timeout=5000)
            page.wait_for_timeout(2000)
            return "deleted"
        except Exception:
            page.keyboard.press("Escape")
            return "no_confirm_button"

    except Exception as e:
        return f"failed: {str(e)[:100]}"
    finally:
        # Always close chat dock to leave page clean
        try:
            close_btn = page.locator('div[aria-label="Close chat"][role="button"]').first
            if close_btn.count() > 0 and close_btn.is_visible():
                close_btn.click(timeout=2000)
            page.keyboard.press("Escape")
        except Exception:
            pass


def unfriend_profile(page, target_url, dry_run=False):
    """Locates the 'Friends' button on profile, clicks 'Unfriend', and confirms removal.
    Returns: 'unfriended', 'already_unfriended', or 'failed: <reason>'
    """
    try:
        # Ensure any leftover menu is closed
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        # Find Friends button
        friends_btn = page.locator('div[aria-label="Friends"][role="button"], div[role="button"]:has-text("Friends")').first
        if friends_btn.count() == 0 or not friends_btn.is_visible():
            body_text = page.inner_text("body")
            if "Add friend" in body_text or "Follow" in body_text:
                return "already_unfriended"
            return "not_found"

        friends_btn.click(timeout=6000)
        page.wait_for_timeout(1500)

        # Look for "Unfriend" in the dropdown menu
        unfriend_opt = page.locator('div[role="menuitem"]:has-text("Unfriend")').first
        if unfriend_opt.count() == 0 or not unfriend_opt.is_visible():
            page.keyboard.press("Escape")
            return "no_unfriend_option"

        if dry_run:
            print("    [DRY RUN] Would click 'Unfriend' and confirm dialog")
            page.keyboard.press("Escape")
            return "dry_run_unfriended"

        unfriend_opt.click(timeout=6000)
        page.wait_for_timeout(1500)

        # Confirmation dialog: "Unfriend [Name]?" -> "Confirm"
        confirm_btn = page.locator('div[aria-label="Confirm"][role="button"], div[role="button"]:has-text("Confirm"), button:has-text("Confirm")')
        try:
            confirm_btn.first.wait_for(state="visible", timeout=4000)
            confirm_btn.first.click(timeout=5000)
            page.wait_for_timeout(2500)
            return "unfriended"
        except Exception:
            page.keyboard.press("Escape")
            return "no_confirm_button"

    except Exception as e:
        return f"failed: {str(e)[:100]}"


def process_single_candidate(page, profile_url, name, dry_run=False):
    """Processes a single profile:
    1. Warning check
    2. Unfollow
    3. Delete chat / conversation (if any)
    4. Unfriend
    5. Record status to SQLite
    """
    print(f"\n--> Processing: {name} ({profile_url})")

    safe_goto(page, profile_url, timeout=25000)

    # 1. Anti-bot / rate-limit warning check
    warning = check_for_warnings(page)
    if warning:
        print(f"  [CRITICAL] Facebook Warning/Block detected: {warning}")
        return {"action": "halt", "reason": warning}

    # 2. Step 1: Unfollow
    print("  [1/3] Unfollowing profile...")
    unfollow_result = unfollow_profile(page, profile_url, dry_run=dry_run)
    print(f"  Unfollow result: {unfollow_result}")

    # Re-navigate or ensure clean profile view
    if page.url != profile_url:
        safe_goto(page, profile_url, timeout=20000)

    # 3. Step 2: Delete Chat
    print("  [2/3] Checking and deleting Messenger chat...")
    chat_result = delete_chat(page, profile_url, dry_run=dry_run)
    print(f"  Chat result: {chat_result}")

    # Close chat dock or any open popup cleanly without an unnecessary full page reload
    try:
        close_btn = page.locator('div[aria-label="Close chat"][role="button"]').first
        if close_btn.count() > 0 and close_btn.is_visible():
            close_btn.click(timeout=1500)
    except Exception:
        pass
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)

    # Only re-navigate if the URL genuinely changed away from the profile
    if not page.url.startswith(profile_url.rstrip("/")):
        safe_goto(page, profile_url, timeout=20000)

    # 4. Step 3: Unfriend
    print("  [3/3] Executing unfriend...")
    unfriend_result = unfriend_profile(page, profile_url, dry_run=dry_run)
    print(f"  Unfriend result: {unfriend_result}")

    # 5. Record to SQLite
    now = datetime.now(timezone.utc).isoformat()
    status = "removed" if (unfriend_result in ("unfriended", "already_unfriended", "dry_run_unfriended")) else "failed"

    if not dry_run:
        with connect() as db:
            db.execute('''UPDATE removal_log SET
                            status = ?,
                            unfollow_status = ?,
                            chat_status = ?,
                            unfriend_status = ?,
                            attempts = attempts + 1,
                            last_error = ?,
                            removed_at = ?
                          WHERE profile_url = ?''',
                       (status, unfollow_result, chat_result, unfriend_result,
                        None if status == 'removed' else f"unfollow:{unfollow_result}|chat:{chat_result}|unfriend:{unfriend_result}",
                        now if status == 'removed' else None,
                        profile_url))
            db.commit()

    return {"action": "continue", "status": status, "unfollow": unfollow_result, "chat": chat_result, "unfriend": unfriend_result}


def run_batch(batch_size=30, delay_min=10, delay_max=22, dry_run=False, headless=False, single=False):
    init_removal_table()
    stats = get_removal_stats()
    pending_total = get_pending_count()

    print("\n========================================================")
    print(f"  Facebook 3-in-1 Cleanup Runner (Unfollow + Chat + Unfriend)")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
    print(f"  Total Queued for Removal: {stats.get('pending', 0)}")
    print(f"  Already Removed: {stats.get('removed', 0)}")
    print(f"  Target for this run: {'1 single profile' if single else f'Batch of up to {batch_size}'}")
    print(f"  Rate-Limit Delay: {delay_min}s - {delay_max}s jitter")
    print("========================================================\n")

    if pending_total == 0:
        print("All candidate profiles have already been processed! Nothing pending.")
        return

    limit = 1 if single else batch_size
    with connect() as db:
        candidates = db.execute('''SELECT profile_url, name FROM removal_log
                                   WHERE status='pending'
                                   ORDER BY name COLLATE NOCASE
                                   LIMIT ?''', (limit,)).fetchall()

    if not candidates:
        print("No candidates available for this batch.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = browser.pages[0] if browser.pages else browser.new_page()

        if not ensure_logged_in(page):
            browser.close()
            return

        processed = 0
        succeeded = 0

        for candidate in candidates:
            profile_url = candidate['profile_url']
            name = candidate['name']

            try:
                res = process_single_candidate(page, profile_url, name, dry_run=dry_run)
            except Exception as e:
                print(f"  [ERROR] Encountered error on {name}: {e}")
                if not dry_run:
                    with connect() as db:
                        db.execute('''UPDATE removal_log SET
                                        status = 'failed',
                                        attempts = attempts + 1,
                                        last_error = ?
                                      WHERE profile_url = ?''',
                                   (f"error: {str(e)[:150]}", profile_url))
                        db.commit()
                res = {"action": "continue", "status": "failed"}

            if res.get("action") == "halt":
                print("\n[HALT] Stopping batch immediately due to Facebook warning or security checkpoint.")
                break

            processed += 1
            if res.get("status") in ("removed", "dry_run_unfriended"):
                succeeded += 1

            # Micro-pause and jitter delay between profiles
            if processed < len(candidates):
                delay = random.uniform(delay_min, delay_max)
                if processed % 10 == 0:
                    delay += random.uniform(30, 45)  # Micro-cooloff pause
                    print(f"  [Pacing] 10 profiles completed in batch. Taking extended {delay:.1f}s cooldown pause...")
                else:
                    print(f"  [Pacing] Waiting {delay:.1f}s before next profile...")
                time.sleep(delay)

        browser.close()

    print("\n========================================================")
    print(f"  Batch Complete: {succeeded}/{processed} succeeded.")
    remaining = get_pending_count()
    print(f"  Remaining to remove: {remaining}")
    print("========================================================\n")


def main():
    parser = argparse.ArgumentParser(description="Facebook Friend, Chat, and Follow Removal Runner")
    parser.add_argument("--batch-size", type=int, default=30, help="Number of profiles to process in this batch (default: 30)")
    parser.add_argument("--delay-min", type=int, default=10, help="Minimum delay between actions in seconds (default: 10)")
    parser.add_argument("--delay-max", type=int, default=22, help="Maximum delay between actions in seconds (default: 22)")
    parser.add_argument("--dry-run", action="store_true", help="Perform checks without executing final deletions/unfriends")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode (default: headed)")
    parser.add_argument("--single", action="store_true", help="Test on exactly 1 profile first")
    args = parser.parse_args()

    run_batch(
        batch_size=args.batch_size,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        dry_run=args.dry_run,
        headless=args.headless,
        single=args.single
    )


if __name__ == '__main__':
    main()
