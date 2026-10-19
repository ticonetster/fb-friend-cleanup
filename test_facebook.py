from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(
        user_data_dir=str(Path("browser_profile").resolve()),
        headless=False
    )

    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto("https://www.facebook.com/")

    input("Log into Facebook, then press Enter to finish testing...")
    browser.close()