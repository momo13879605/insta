#!/usr/bin/env python3
"""
================================================================================
ربات FastDL + شکارچی APIها (نسخه مقاوم‌سازی‌شده در برابر ERR_CONNECTION_RESET)
--------------------------------------------------------------------------------
ویژگی‌های جدید:
  • تشخیص خودکار پروکسی از متغیرهای محیطی و اتصال از طریق آن
  • هشدار پیش از اجرا در صورت عدم وجود پروکسی
  • تلاش مجدد خودکار با تاخیر تصادفی
  • لاگ‌گیری کامل برای دیباگ
================================================================================
"""

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
)
from playwright_stealth import Stealth

# ------------------------------------------------------------------ #
#                         تنظیمات
# ------------------------------------------------------------------ #
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1366, "height": 900}
NAV_TIMEOUT_MS = 60_000
RESULT_WAIT_MS = 15_000
MAX_RETRIES = 3


# ------------------------------------------------------------------ #
#              تشخیص خودکار پروکسی از محیط
# ------------------------------------------------------------------ #
def detect_proxy() -> Optional[str]:
    """
    ترتیب اولویت:
      1. پارامتر خط فرمان (در main مدیریت می‌شود)
      2. متغیر BROWSE_PROXY (برای محیط‌های TUN/FakeIP)
      3. HTTPS_PROXY
      4. HTTP_PROXY
    """
    for var in ("BROWSE_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        val = os.environ.get(var)
        if val:
            print(f"🔌 پروکسی از متغیر محیطی {var} شناسایی شد: {val}")
            return val
    print("⚠️  هیچ پروکسی در متغیرهای محیطی یافت نشد (BROWSE_PROXY/HTTPS_PROXY/HTTP_PROXY).")
    return None


# ------------------------------------------------------------------ #
#              شکارچی شبکه (بدون تغییر ساختاری)
# ------------------------------------------------------------------ #
class NetworkCapture:
    def __init__(self) -> None:
        self.all_requests: list[Dict[str, Any]] = []
        self.all_responses: list[Dict[str, Any]] = []
        self.api_calls: list[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def on_request(self, request: Request) -> None:
        entry = {
            "type": "request",
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "headers": dict(request.headers),
            "post_data": request.post_data if request.post_data else None,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        }
        async with self._lock:
            self.all_requests.append(entry)

    async def on_response(self, response: Response) -> None:
        entry: Dict[str, Any] = {
            "type": "response",
            "status": response.status,
            "url": response.url,
            "headers": dict(response.headers),
            "body": None,
            "body_size": None,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        }
        try:
            content_type = response.headers.get("content-type", "").lower()
            is_texty = any(
                t in content_type
                for t in ("json", "javascript", "text", "xml", "html", "plain")
            )
            if is_texty:
                body = await response.text()
                try:
                    entry["body"] = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    entry["body"] = body[:5000]
                entry["body_size"] = len(body)
            else:
                entry["body"] = f"<binary:{content_type}>"
        except Exception as exc:
            entry["body"] = f"<error: {exc}>"

        async with self._lock:
            self.all_responses.append(entry)
            if self._looks_like_api(response):
                self.api_calls.append(entry)

    @staticmethod
    def _looks_like_api(response: Response) -> bool:
        url = response.url.lower()
        ctype = response.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            return True
        if any(k in url for k in ("/api/", "graphql", "/ajax/", "xhr", ".json")):
            return True
        return False

    def summary(self) -> Dict[str, Any]:
        return {
            "total_requests": len(self.all_requests),
            "total_responses": len(self.all_responses),
            "api_calls_detected": len(self.api_calls),
        }


# ------------------------------------------------------------------ #
#                استخراج داده از صفحه نتیجه
# ------------------------------------------------------------------ #
async def extract_result_page(page: Page) -> Dict[str, Any]:
    return {
        "title": await page.title(),
        "url": page.url,
        "download_links": await page.eval_on_selector_all(
            "a[href]",
            """els => els
                .map(e => ({href: e.href, text: e.innerText.trim().substring(0, 200)}))
                .filter(e => /download|dl|media|cdn/i.test(e.href) || /download/i.test(e.text))""",
        ),
        "images": await page.eval_on_selector_all(
            "img",
            """els => els
                .map(e => ({src: e.currentSrc || e.src, alt: e.alt}))
                .filter(e => e.src && !e.src.startsWith('data:'))""",
        ),
        "videos": await page.eval_on_selector_all(
            "video, video source", "els => els.map(e => e.src || e.getAttribute('src'))"
        ),
        "headings": await page.eval_on_selector_all(
            "h1, h2, h3", "els => els.map(e => e.innerText.trim().substring(0, 300))"
        ),
    }


# ------------------------------------------------------------------ #
#                      اجرای اصلی با تلاش مجدد
# ------------------------------------------------------------------ #
async def run_bot_once(
    *,
    instagram_url: str,
    headless: bool,
    proxy: Optional[str],
    output_path: str,
) -> Dict[str, Any]:
    """یک بار اجرای کامل فرآیند."""
    capture = NetworkCapture()

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        ctx_kwargs: Dict[str, Any] = {
            "viewport": DEFAULT_VIEWPORT,
            "user_agent": USER_AGENT,
            "ignore_https_errors": True,
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if proxy:
            ctx_kwargs["proxy"] = {"server": proxy}
            print(f"🔌 استفاده از پروکسی: {proxy}")

        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        page: Page = await context.new_page()

        await Stealth().apply_stealth_async(page)
        page.on("request", capture.on_request)
        page.on("response", capture.on_response)

        # 1) باز کردن سایت
        print("⏳ باز کردن صفحه FastDL ...")
        await page.goto(
            "https://fastdl.app/en5IW",
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT_MS,
        )
        await page.wait_for_timeout(random.uniform(800, 1500))

        # 2) پر کردن و ارسال فرم
        print(f"✍️  وارد کردن لینک: {instagram_url}")
        await page.fill("#search-form-input", instagram_url, timeout=10_000)
        await page.wait_for_timeout(random.uniform(300, 700))

        print("🚀 ارسال فرم ...")
        try:
            async with page.expect_navigation(timeout=RESULT_WAIT_MS):
                await page.click("#searchFormButton", timeout=10_000)
        except Exception:
            await page.wait_for_timeout(3000)

        # 3) صبر برای APIها
        print("⏳ در انتظار بارگذاری نتایج و APIها ...")
        await page.wait_for_timeout(RESULT_WAIT_MS // 2)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # 4) استخراج داده
        result_data = await extract_result_page(page)

        screenshot_path = str(Path(output_path).with_suffix(".png"))
        await page.screenshot(path=screenshot_path, full_page=True)

        await context.close()
        await browser.close()

    final_output = {
        "_meta": {
            "instagram_url": instagram_url,
            "proxy_used": proxy or "none",
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
            "screenshot": screenshot_path,
            "network_summary": capture.summary(),
        },
        "page_result": result_data,
        "network_capture": {
            "api_calls": capture.api_calls,
            "all_requests_sample": capture.all_requests[:200],
            "all_responses_sample": capture.all_responses[:200],
        },
    }

    out_file = Path(output_path).expanduser().resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as fh:
        json.dump(final_output, fh, ensure_ascii=False, indent=2)

    print(f"\n💾 خروجی ذخیره شد: {out_file}")
    print(f"🖼️  اسکرین‌شات: {screenshot_path}")
    print(f"📊 خلاصه شبکه: {capture.summary()}")
    return final_output


async def run_bot_with_retry(
    *,
    instagram_url: str,
    headless: bool,
    proxy: Optional[str],
    output_path: str,
) -> Dict[str, Any]:
    """اجرای ربات با تلاش مجدد خودکار در صورت خطاهای شبکه."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"\n🔄 تلاش {attempt}/{MAX_RETRIES} ...")
            return await run_bot_once(
                instagram_url=instagram_url,
                headless=headless,
                proxy=proxy,
                output_path=output_path,
            )
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            if "ERR_CONNECTION_RESET" in msg or "ERR_TUNNEL_CONNECTION_FAILED" in msg:
                wait = random.uniform(2.0, 5.0)
                print(f"⚠️  خطای شبکه در تلاش {attempt}: {exc}")
                print(f"⏳ تلاش مجدد پس از {wait:.1f} ثانیه ...")
                await asyncio.sleep(wait)
                continue
            else:
                raise  # خطای غیرشبکه‌ای → توقف فوری

    raise RuntimeError(
        f"پس از {MAX_RETRIES} تلاش، اتصال به fastdl.app برقرار نشد. "
        f"آخرین خطا: {last_exc}\n"
        "💡 راه‌حل: یک پروکسی/VPN فعال کنید و متغیر BROWSE_PROXY را تنظیم کنید."
    ) from last_exc


# ------------------------------------------------------------------ #
#                    ورودی تعاملی و CLI
# ------------------------------------------------------------------ #
def prompt_user() -> Dict[str, Any]:
    print("\n" + "═" * 60)
    print("  🤖 ربات FastDL + شکارچی APIهای شبکه")
    print("═" * 60)

    while True:
        ig_url = input("\n🔗 لینک اینستاگرام: ").strip()
        if not ig_url:
            continue
        if "instagram.com" not in ig_url:
            print("⚠️  لینک معتبر نیست.")
            continue
        break

    headed = input("👁️  مرورگر قابل مشاهده باشد؟ (y/N): ").strip().lower() in ("y", "yes", "بله", "ب")
    default_out = f"fastdl_{datetime.now():%Y%m%d_%H%M%S}.json"
    out = input(f"💾 فایل خروجی [{default_out}]: ").strip() or default_out
    return {"instagram_url": ig_url, "headed": headed, "output": out}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("instagram_url", nargs="?", default=None)
    p.add_argument("--headed", action="store_true")
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--proxy", default=None, help="مثال: http://127.0.0.1:8080 یا socks5://...")
    return p.parse_args()


async def async_main() -> None:
    args = parse_args()

    if args.instagram_url:
        config = {
            "instagram_url": args.instagram_url.strip(),
            "headed": args.headed,
            "output": args.output or f"fastdl_{datetime.now():%Y%m%d_%H%M%S}.json",
            "proxy": args.proxy,
        }
    else:
        cfg = prompt_user()
        config = {**cfg, "proxy": args.proxy}

    # اگر پروکسی صریح داده نشده، از محیط بخوان
    if not config["proxy"]:
        config["proxy"] = detect_proxy()

    # هشدار نهایی اگر پروکسی وجود ندارد
    if not config["proxy"]:
        print("\n⚠️  هشدار: پروکسی فعال نیست. اگر IP شما توسط Cloudflare مسدود باشد،")
        print("   اتصال با ERR_CONNECTION_RESET قطع خواهد شد.\n")

    try:
        await run_bot_with_retry(
            instagram_url=config["instagram_url"],
            headless=not config["headed"],
            proxy=config["proxy"],
            output_path=config["output"],
        )
    except Exception as exc:
        print(f"\n❌ خطا: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n⛔ لغو شد.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
