#!/usr/bin/env python3
"""
================================================================================
ربات FastDL + شکارچی APIها (نسخه نهایی و مقاوم)
--------------------------------------------------------------------------------
ویژگی‌های کلیدی:
  • ارسال مطمئن فرم با استفاده از تکنیک‌های جایگزین (JavaScript و کیبورد)
  • شکار کامل تمام درخواست‌های شبکه پس از ارسال فرم
  • استخراج هوشمندانه لینک‌های دانلود و اطلاعات ویدیو
  • مدیریت خطا و تلاش مجدد
================================================================================
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Request,
    Response,
    TimeoutError as PlaywrightTimeoutError,
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
RESULT_WAIT_MS = 20_000  # زمان انتظار برای نتایج
MAX_RETRIES = 2


# ------------------------------------------------------------------ #
#              کلاس شکارچی شبکه (Network Capture)
# ------------------------------------------------------------------ #
class NetworkCapture:
    """ضبط تمام درخواست‌ها و پاسخ‌های شبکه، با تمرکز بر APIها."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self.api_calls: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def on_request(self, request: Request) -> None:
        entry = {
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "post_data": request.post_data,
            "headers": dict(request.headers),
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        }
        async with self._lock:
            self.requests.append(entry)

    async def on_response(self, response: Response) -> None:
        entry: Dict[str, Any] = {
            "status": response.status,
            "url": response.url,
            "headers": dict(response.headers),
            "body": None,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        }
        # فقط بدنه‌های متنی/JSON را می‌خوانیم
        try:
            content_type = response.headers.get("content-type", "").lower()
            if any(t in content_type for t in ("json", "text", "javascript", "xml", "html")):
                body_text = await response.text()
                try:
                    entry["body"] = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    entry["body"] = body_text[:10000]  # محدودیت حجم
        except Exception as exc:
            entry["body"] = f"<error: {exc}>"

        async with self._lock:
            self.responses.append(entry)
            # تشخیص API: پاسخ JSON یا URL شامل api
            if self._is_api(response, entry):
                self.api_calls.append(entry)

    @staticmethod
    def _is_api(response: Response, entry: Dict[str, Any]) -> bool:
        url = response.url.lower()
        content_type = response.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            return True
        if any(k in url for k in ("/api/", "graphql", "/ajax/", "get_", "search")):
            return True
        return False

    def summary(self) -> Dict[str, Any]:
        return {
            "total_requests": len(self.requests),
            "total_responses": len(self.responses),
            "api_calls": len(self.api_calls),
        }


# ------------------------------------------------------------------ #
#                استخراج داده از صفحه نتیجه
# ------------------------------------------------------------------ #
async def extract_results(page: Page) -> Dict[str, Any]:
    """استخراج لینک‌های دانلود، تصاویر و اطلاعات از صفحه نتایج."""
    results: Dict[str, Any] = {
        "page_title": await page.title(),
        "page_url": page.url,
        "download_links": [],
        "video_previews": [],
        "images": [],
        "text_content": "",
    }

    # 1. لینک‌های دانلود (بر اساس href حاوی download یا cdn)
    links = await page.eval_on_selector_all(
        "a[href]",
        """els => els
            .map(e => ({href: e.href, text: e.innerText.trim()}))
            .filter(l => /download|dl|cdn|media|video|photo/i.test(l.href))""",
    )
    results["download_links"] = links

    # 2. ویدیوها و پیش‌نمایش‌ها
    videos = await page.eval_on_selector_all(
        "video, video source, img[src*='cdninstagram'], img[src*='fbcdn']",
        """els => els.map(e => e.src || e.getAttribute('src'))""",
    )
    results["video_previews"] = [v for v in videos if v]

    # 3. تصاویر
    results["images"] = await page.eval_on_selector_all(
        "img",
        """els => els
            .map(e => e.src)
            .filter(src => src && !src.startsWith('data:'))""",
    )

    # 4. متن اصلی صفحه (برای خطاها یا پیام‌ها)
    results["text_content"] = await page.eval_on_selector(
        "body", "el => el.innerText.substring(0, 3000)"
    )

    return results


# ------------------------------------------------------------------ #
#                      منطق اصلی ربات
# ------------------------------------------------------------------ #
async def run_once(
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
        }
        if proxy:
            ctx_kwargs["proxy"] = {"server": proxy}

        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        page: Page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        # اتصال شنودکننده‌ها
        page.on("request", capture.on_request)
        page.on("response", capture.on_response)

        # 1) باز کردن صفحه اصلی
        print("⏳ بارگذاری صفحه اصلی ...")
        await page.goto(
            "https://fastdl.app/en5IW",
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT_MS,
        )
        await page.wait_for_timeout(2000)

        # 2) پر کردن فیلد ورودی
        print(f"✍️  وارد کردن لینک: {instagram_url}")
        try:
            # استفاده از evaluate برای اطمینان از مقداردهی
            await page.evaluate(
                """(url) => {
                    const input = document.querySelector('#search-form-input');
                    if (input) {
                        input.value = url;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                instagram_url,
            )
            await page.wait_for_timeout(500)
        except Exception as e:
            print(f"⚠️ خطا در پر کردن فیلد: {e}")

        # 3) ارسال فرم (سه روش جایگزین برای اطمینان)
        print("🚀 ارسال فرم ...")
        submitted = False
        # روش الف: کلیک روی دکمه
        try:
            await page.click("#searchFormButton", timeout=5000, force=True)
            submitted = True
            print("✅ ارسال با کلیک روی دکمه.")
        except Exception:
            print("⚠️ کلیک روی دکمه ناموفق بود، تلاش با روش‌های جایگزین...")

        if not submitted:
            # روش ب: ارسال فرم با JavaScript
            try:
                await page.evaluate("document.querySelector('#searchForm').submit()")
                submitted = True
                print("✅ ارسال با JavaScript.")
            except Exception:
                print("⚠️ ارسال با JavaScript ناموفق بود.")

        if not submitted:
            # روش ج: فشار دادن Enter در فیلد ورودی
            try:
                await page.press("#search-form-input", "Enter")
                submitted = True
                print("✅ ارسال با کلید Enter.")
            except Exception:
                print("⚠️ ارسال با Enter ناموفق بود.")

        if not submitted:
            await browser.close()
            raise RuntimeError("هیچ‌کدام از روش‌های ارسال فرم موفق نبود.")

        # 4) انتظار برای بارگذاری نتایج و شکار APIها
        print("⏳ در انتظار نتایج و APIها ...")
        await page.wait_for_timeout(RESULT_WAIT_MS)

        # 5) استخراج داده از صفحه نتیجه
        print("📄 استخراج داده‌های صفحه نتیجه ...")
        result_data = await extract_results(page)

        # ذخیره اسکرین‌شات
        screenshot_path = str(Path(output_path).with_suffix(".png"))
        await page.screenshot(path=screenshot_path, full_page=True)

        await context.close()
        await browser.close()

    # ساخت خروجی نهایی
    output = {
        "_meta": {
            "instagram_url": instagram_url,
            "proxy_used": proxy or "none",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "screenshot": screenshot_path,
            "network_summary": capture.summary(),
        },
        "page_result": result_data,
        "network_capture": {
            "api_calls": capture.api_calls,
            "all_requests": capture.requests,
            "all_responses": capture.responses,
        },
    }

    # ذخیره در فایل
    out_path = Path(output_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)

    print(f"\n💾 خروجی ذخیره شد: {out_path}")
    print(f"🖼️  اسکرین‌شات: {screenshot_path}")
    print(f"📊 خلاصه شبکه: {capture.summary()}")
    return output


# ------------------------------------------------------------------ #
#                    اجرای اصلی با تلاش مجدد
# ------------------------------------------------------------------ #
async def run_with_retry(
    *,
    instagram_url: str,
    headless: bool,
    proxy: Optional[str],
    output_path: str,
) -> Dict[str, Any]:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"\n🔄 تلاش {attempt}/{MAX_RETRIES} ...")
            return await run_once(
                instagram_url=instagram_url,
                headless=headless,
                proxy=proxy,
                output_path=output_path,
            )
        except Exception as exc:
            last_error = exc
            print(f"⚠️ خطا در تلاش {attempt}: {exc}")
            if attempt < MAX_RETRIES:
                wait = random.uniform(3, 7)
                print(f"⏳ تلاش مجدد پس از {wait:.1f} ثانیه ...")
                await asyncio.sleep(wait)
            else:
                raise

    raise RuntimeError(f"پس از {MAX_RETRIES} تلاش ناموفق بود: {last_error}")


# ------------------------------------------------------------------ #
#                        ورودی تعاملی و CLI
# ------------------------------------------------------------------ #
def prompt_user() -> Dict[str, Any]:
    print("\n" + "═" * 60)
    print("  🤖 ربات FastDL + شکارچی API (نسخه نهایی)")
    print("═" * 60)

    while True:
        url = input("\n🔗 لینک اینستاگرام: ").strip()
        if url and "instagram.com" in url:
            break
        print("⚠️ لینک معتبر نیست. دوباره تلاش کنید.")

    headed = input("👁️  مرورگر قابل مشاهده باشد؟ (y/N): ").strip().lower() in ("y", "yes", "بله", "ب")

    default_out = f"fastdl_result_{datetime.now():%Y%m%d_%H%M%S}.json"
    out = input(f"💾 فایل خروجی [{default_out}]: ").strip() or default_out

    return {"url": url, "headed": headed, "output": out}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ربات FastDL + شکار API")
    p.add_argument("instagram_url", nargs="?", default=None, help="لینک اینستاگرام")
    p.add_argument("--headed", action="store_true", help="مرورگر قابل مشاهده")
    p.add_argument("-o", "--output", default=None, help="فایل خروجی JSON")
    p.add_argument("--proxy", default=None, help="پروکسی (مثال: http://127.0.0.1:8080)")
    return p.parse_args()


def detect_proxy() -> Optional[str]:
    for var in ("BROWSE_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        if os.environ.get(var):
            print(f"🔌 پروکسی از {var} شناسایی شد.")
            return os.environ[var]
    return None


async def async_main() -> None:
    args = parse_args()

    if args.instagram_url:
        config = {
            "instagram_url": args.instagram_url.strip(),
            "headed": args.headed,
            "output": args.output or f"fastdl_result_{datetime.now():%Y%m%d_%H%M%S}.json",
            "proxy": args.proxy or detect_proxy(),
        }
    else:
        cfg = prompt_user()
        config = {
            "instagram_url": cfg["url"],
            "headed": cfg["headed"],
            "output": cfg["output"],
            "proxy": detect_proxy(),
        }

    if not config["proxy"]:
        print("⚠️ هشدار: پروکسی فعال نیست. در صورت مسدود بودن IP، اتصال قطع می‌شود.")

    try:
        await run_with_retry(
            instagram_url=config["instagram_url"],
            headless=not config["headed"],
            proxy=config["proxy"],
            output_path=config["output"],
        )
    except Exception as exc:
        print(f"\n❌ خطای نهایی: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n⛔ لغو شد.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
