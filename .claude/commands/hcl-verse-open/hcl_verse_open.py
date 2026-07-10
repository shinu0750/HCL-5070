#!/usr/bin/env python3
"""
開啟瀏覽器 → 登入 HCL Portal → 進入 Verse 信箱，登入後保持瀏覽器開啟供用戶操作。
用戶關閉瀏覽器視窗後腳本才結束。
"""
import os, sys

_env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                os.environ.setdefault(k.strip(), v.strip())

from playwright.sync_api import sync_playwright

PORTAL_URL = os.environ.get("HCL_PORTAL_URL", "https://portal.ecic.com.tw/app/eip.nsf/XPortal.xsp")
VERSE_URL  = os.environ.get("HCL_VERSE_URL",  "https://mail1.ecic.com.tw/verse")
USERNAME   = os.environ.get("HCL_USERNAME",    "shuhsing")
PASSWORD   = os.environ.get("HCL_PASSWORD",    "")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, ignore_https_errors=True, locale="en-US")
        page = ctx.new_page()
        page.set_default_timeout(60000)

        # 登入 Portal
        page.goto(PORTAL_URL)
        page.wait_for_load_state("networkidle")
        page.fill('input[type="text"], input[placeholder*="Email"], input[name*="user"]', USERNAME)
        page.fill('input[type="password"]', PASSWORD)
        page.click('button[type="submit"], input[type="submit"], button:has-text("登入")')
        page.wait_for_load_state("networkidle")

        # 進入 Verse 信箱
        page.goto(VERSE_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_selector('[role="treeitem"]', timeout=15000)
        sys.stdout.buffer.write("✓ 已登入並開啟 Verse 信箱，瀏覽器保持開啟（關閉視窗即結束）\n".encode("utf-8"))
        sys.stdout.buffer.flush()

        # 保持開啟，直到用戶關閉瀏覽器視窗
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass


if __name__ == "__main__":
    main()
