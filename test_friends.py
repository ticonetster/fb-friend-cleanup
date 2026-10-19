
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "browser_profile"

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=False
    )

    page = browser.pages[0] if browser.pages else browser.new_page()

    page.goto("https://www.facebook.com/me/friends", wait_until="domcontentloaded")

    print("\nFacebook friends page opened.")
    print("Current URL:", page.url)

    input("\nPress Enter when you're finished checking the page...")

    browser.close()
