#!/usr/bin/env python3
"""
================================================================================
 استخراج‌گر جامع متادیتای وب با Playwright + playwright-stealth (نسخه ۲.۰.۳+)
--------------------------------------------------------------------------------
 اجرا:
   python3 main.py                     # حالت تعاملی
   python3 main.py https://site.com    # با آرگومان
   python3 main.py https://site.com --headed -o out.json
================================================================================
"""

import argparse
import asyncio
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Response,
)
# ✅ API جدید نسخه ۲.۰.۳+ : کلاس Stealth
from playwright_stealth import Stealth

# ------------------------------------------------------------------ #
#                         تنظیمات و ثابت‌ها
# ------------------------------------------------------------------ #
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1366, "height": 900}
NAV_TIMEOUT_MS = 60_000
SETTLE_TIMEOUT_MS = 2_500  # کمی بیشتر برای اتمام رندر JS و چالش‌های ضد-ربات


# ------------------------------------------------------------------ #
#                    کلاس استخراج متادیتا (بدون تغییر)
# ------------------------------------------------------------------ #
class MetadataExtractor:
    """استخراج جامع تمام اطلاعات صفحه از DOM رندرشده و Context مرورگر."""

    def __init__(self, page: Page, response: Optional[Response] = None) -> None:
        self.page = page
        self.response = response

    async def extract_all(self) -> Dict[str, Any]:
        return {
            "page_info": await self._page_info(),
            "meta_tags": await self._meta_tags(),
            "open_graph": await self._og_tags(),
            "twitter_cards": await self._twitter_tags(),
            "json_ld": await self._json_ld(),
            "link_tags": await self._link_tags(),
            "canonical_url": await self._canonical(),
            "favicon": await self._favicon(),
            "images": await self._images(),
            "scripts": await self._scripts(),
            "stylesheets": await self._stylesheets(),
            "headings": await self._headings(),
            "forms": await self._forms(),
            "cookies": await self._cookies(),
            "local_storage": await self._local_storage(),
            "session_storage": await self._session_storage(),
            "response_headers": self._response_headers(),
        }

    async def _page_info(self) -> Dict[str, Any]:
        title = await self.page.title()
        lang = await self.page.get_attribute("html", "lang")
        charset_list = await self.page.eval_on_selector_all(
            "meta[charset]", "els => els.map(e => e.getAttribute('charset'))"
        )
        viewport = await self.page.evaluate(
            "() => ({width: window.innerWidth, height: window.innerHeight,"
            " devicePixelRatio: window.devicePixelRatio})"
        )
        return {
            "title": title,
            "url": self.page.url,
            "lang": lang,
            "charset": charset_list[0] if charset_list else None,
            "viewport": viewport,
        }

    async def _meta_tags(self) -> List[Dict[str, Any]]:
        return await self.page.eval_on_selector_all(
            "head meta",
            """els => els.map(e => ({
                name: e.getAttribute('name'),
                property: e.getAttribute('property'),
                content: e.getAttribute('content'),
                charset: e.getAttribute('charset'),
                httpEquiv: e.getAttribute('http-equiv'),
            })).filter(e => e.name || e.property || e.charset || e.httpEquiv)""",
        )

    async def _og_tags(self) -> Dict[str, str]:
        return await self.page.eval_on_selector_all(
            'head meta[property^="og:"]',
            """els => {
                const out = {};
                for (const e of els) out[e.getAttribute('property')] =
                    e.getAttribute('content');
                return out;
            }""",
        )

    async def _twitter_tags(self) -> Dict[str, str]:
        return await self.page.eval_on_selector_all(
            'head meta[name^="twitter:"]',
            """els => {
                const out = {};
                for (const e of els) out[e.getAttribute('name')] =
                    e.getAttribute('content');
                return out;
            }""",
        )

    async def _json_ld(self) -> List[Any]:
        scripts = await self.page.eval_on_selector_all(
            'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
        )
        parsed: List[Any] = []
        for s in scripts:
            try:
                parsed.append(json.loads(s))
            except json.JSONDecodeError:
                parsed.append({"_raw": s, "_error": "invalid JSON"})
        return parsed

    async def _link_tags(self) -> List[Dict[str, Optional[str]]]:
        return await self.page.eval_on_selector_all(
            "head link",
            """els => els.map(e => ({
                rel: e.getAttribute('rel'),
                href: e.getAttribute('href'),
                type: e.getAttribute('type'),
                hreflang: e.getAttribute('hreflang'),
                sizes: e.getAttribute('sizes'),
                as: e.getAttribute('as'),
            })).filter(e => e.href)""",
        )

    async def _canonical(self) -> Optional[str]:
        return await self.page.eval_on_selector_all(
            'head link[rel="canonical"]', "els => els[0]?.href || null"
        ) or None

    async def _favicon(self) -> Optional[str]:
        return await self.page.eval_on_selector_all(
            'head link[rel*="icon"]',
            "els => els[0]?.href || null",
        ) or None

    async def _images(self) -> List[Dict[str, Any]]:
        return await self.page.eval_on_selector_all(
            "img",
            """els => els.map(e => ({
                src: e.currentSrc || e.src,
                alt: e.alt,
                width: e.naturalWidth,
                height: e.naturalHeight,
            })).filter(e => e.src).slice(0, 200)""",
        )

    async def _scripts(self) -> List[str]:
        return await self.page.eval_on_selector_all(
            "script[src]", "els => els.map(e => e.src)"
        )

    async def _stylesheets(self) -> List[str]:
        return await self.page.eval_on_selector_all(
            'link[rel="stylesheet"]', "els => els.map(e => e.href)"
        )

    async def _headings(self) -> List[Dict[str, str]]:
        return await self.page.eval_on_selector_all(
            "h1, h2, h3",
            """els => els.map(e => ({
                tag: e.tagName.toLowerCase(),
                text: e.innerText.trim().substring(0, 300),
            }))""",
        )

    async def _forms(self) -> List[Dict[str, Any]]:
        return await self.page.eval_on_selector_all(
            "form",
            """els => els.map(form => ({
                action: form.action,
                method: form.method,
                id: form.id || null,
                name: form.name || null,
                fields: Array.from(form.elements)
                    .map(el => ({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || null,
                        name: el.name || null,
                        id: el.id || null,
                        placeholder: el.placeholder || null,
                        required: el.required || false,
                        value: el.value || null,
                        pattern: el.pattern || null,
                        maxLength: el.maxLength > 0 ? el.maxLength : null,
                        checked: (el.type === 'checkbox' || el.type === 'radio')
                                 ? el.checked : null,
                        options: el.tagName === 'SELECT'
                            ? Array.from(el.options).map(o => ({value: o.value, text: o.text}))
                            : null,
                    }))
                    .filter(f => f.name || f.id),
            }))""",
        )

    async def _cookies(self) -> List[Dict[str, Any]]:
        cookies = await self.page.context.cookies()
        keys = ("name", "value", "domain", "path", "httpOnly", "secure", "sameSite")
        return [{k: c.get(k) for k in keys} for c in cookies]

    async def _local_storage(self) -> Dict[str, str]:
        try:
            return await self.page.evaluate(
                "() => Object.fromEntries(Object.entries(localStorage))"
            )
        except Exception:
            return {}

    async def _session_storage(self) -> Dict[str, str]:
        try:
            return await self.page.evaluate(
                "() => Object.fromEntries(Object.entries(sessionStorage))"
            )
        except Exception:
            return {}

    def _response_headers(self) -> Dict[str, str]:
        if self.response is None:
            return {}
        try:
            return dict(self.response.headers)
        except Exception:
            return {}


# ------------------------------------------------------------------ #
#                      اجرای مرورگر با Stealth جدید
# ------------------------------------------------------------------ #
async def run_extraction(
    url: str,
    *,
    headless: bool = True,
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """
    استخراج داده‌ها با استفاده از playwright-stealth نسخه ۲.۰.۳+ .
    پارامتر proxy اختیاری است (مثال: "http://127.0.0.1:8080").
    """
    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context_kwargs: Dict[str, Any] = {
            "viewport": DEFAULT_VIEWPORT,
            "user_agent": USER_AGENT,
            "ignore_https_errors": True,
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if proxy:
            context_kwargs["proxy"] = {"server": proxy}

        context: BrowserContext = await browser.new_context(**context_kwargs)
        page: Page = await context.new_page()

        # ✅ اعمال Stealth با API جدید نسخه ۲.۰.۳
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        # کمی تأخیر تصادفی برای شبیه‌سازی رفتار انسانی (بین ۰.۵ تا ۱.۵ ثانیه)
        await asyncio.sleep(random.uniform(0.5, 1.5))

        try:
            # استفاده از domcontentloaded به جای networkidle (برای جلوگیری از تایم‌اوت)
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAV_TIMEOUT_MS,
            )
            # صبر اضافی برای اتمام رندر JS و عبور از چالش‌های احتمالی
            await page.wait_for_timeout(SETTLE_TIMEOUT_MS)
        except Exception as exc:
            await browser.close()
            raise RuntimeError(f"خطا در بارگذاری صفحه: {exc}") from exc

        extractor = MetadataExtractor(page, response)
        data = await extractor.extract_all()

        await context.close()
        await browser.close()
        return data


# ------------------------------------------------------------------ #
#                      ورودی تعاملی از کاربر (بدون تغییر)
# ------------------------------------------------------------------ #
def prompt_user() -> Dict[str, Any]:
    print("\n" + "═" * 66)
    print("  🔎  استخراج‌گر جامع متادیتای وب‌سایت با Playwright")
    print("═" * 66)

    while True:
        url = input("\n🌐 آدرس سایت را وارد کنید: ").strip()
        if not url:
            print("  ⚠️  آدرس نمی‌تواند خالی باشد. دوباره تلاش کنید.")
            continue
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        break

    ans = input("👁️  مرورگر به صورت قابل مشاهده اجرا شود؟ (y/N): ").strip().lower()
    headed = ans in ("y", "yes", "بله", "ب")

    ans = input("💾 خروجی در فایل JSON ذخیره شود؟ (y/N): ").strip().lower()
    save = ans in ("y", "yes", "بله", "ب")

    output_file: Optional[str] = None
    if save:
        default_name = _default_filename(url)
        ans = input(f"📁 نام فایل خروجی [{default_name}]: ").strip()
        output_file = ans or default_name

    return {"url": url, "headed": headed, "output": output_file}


def _default_filename(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0] or "site"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"metadata_{host}_{ts}.json"


# ------------------------------------------------------------------ #
#                      نمایش خوانای خروجی (بدون تغییر)
# ------------------------------------------------------------------ #
def print_summary(data: Dict[str, Any]) -> None:
    print("\n" + "═" * 70)
    print("  ✅  استخراج با موفقیت انجام شد")
    print("═" * 70)

    info = data.get("page_info", {})
    vp = info.get("viewport") or {}
    print(f"\n📄 عنوان       : {info.get('title')}")
    print(f"🌐 URL نهایی    : {info.get('url')}")
    print(f"🌍 زبان         : {info.get('lang')}")
    print(f"🔤 charset      : {info.get('charset')}")
    print(f"📐 viewport     : {vp.get('width')}x{vp.get('height')} (DPR={vp.get('devicePixelRatio')})")

    counts = [
        ("🏷️  متا تگ‌ها",      len(data.get("meta_tags", []))),
        ("📊 Open Graph",     len(data.get("open_graph", {}))),
        ("🐦 Twitter Cards",  len(data.get("twitter_cards", {}))),
        ("🧩 JSON-LD",        len(data.get("json_ld", []))),
        ("🔗 لینک‌ها",         len(data.get("link_tags", []))),
        ("🖼️  تصاویر",        len(data.get("images", []))),
        ("📜 اسکریپت‌ها",      len(data.get("scripts", []))),
        ("🎨 استایل‌ها",       len(data.get("stylesheets", []))),
        ("📑 تیترها",          len(data.get("headings", []))),
        ("📋 فرم‌ها",          len(data.get("forms", []))),
        ("🍪 کوکی‌ها",         len(data.get("cookies", []))),
        ("💾 localStorage",   len(data.get("local_storage", {}))),
        ("📦 sessionStorage", len(data.get("session_storage", {}))),
        ("📨 هدرهای پاسخ",    len(data.get("response_headers", {}))),
    ]
    print("\n📊 خلاصه شمارش موارد:")
    for label, count in counts:
        print(f"   {label:<20}: {count}")

    forms = data.get("forms", [])
    if forms:
        print("\n" + "─" * 70)
        print("  📋 جزئیات فرم‌ها (برای مرحله بعد — پر کردن و ارسال)")
        print("─" * 70)
        for i, form in enumerate(forms, 1):
            print(f"\nفرم #{i}:")
            print(f"   action: {form.get('action')}")
            print(f"   method: {form.get('method')}")
            print(f"   id    : {form.get('id')}    name: {form.get('name')}")
            print("   فیلدها:")
            for f in form.get("fields", []):
                parts = [f"<{f['tag']}>"]
                if f.get("type"):        parts.append(f"type={f['type']}")
                if f.get("name"):        parts.append(f"name='{f['name']}'")
                if f.get("id"):          parts.append(f"id='{f['id']}'")
                if f.get("required"):    parts.append("required")
                if f.get("placeholder"): parts.append(f"placeholder='{f['placeholder']}'")
                print("     • " + " | ".join(parts))

    print("\n" + "═" * 70)


def save_to_json(data: Dict[str, Any], path: str) -> None:
    out_path = Path(path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"\n💾 خروجی در فایل ذخیره شد: {out_path}")


# ------------------------------------------------------------------ #
#                            ورودی CLI
# ------------------------------------------------------------------ #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="استخراج جامع متادیتا و ساختار یک صفحه وب با Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "مثال‌ها:\n"
            "  python3 main.py\n"
            "  python3 main.py https://example.com\n"
            "  python3 main.py example.com --headed -o out.json\n"
            "  python3 main.py example.com --proxy http://127.0.0.1:8080\n"
        ),
    )
    parser.add_argument(
        "url", nargs="?", default=None,
        help="آدرس سایت (اختیاری؛ در صورت خالی بودن، تعاملی پرسیده می‌شود)",
    )
    parser.add_argument(
        "--headed", action="store_true",
        help="اجرای مرورگر در حالت قابل مشاهده",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="مسیر فایل JSON برای ذخیره خروجی",
    )
    parser.add_argument(
        "--proxy", default=None,
        help="آدرس پروکسی (مثال: http://127.0.0.1:8080)",
    )
    return parser.parse_args()


# ------------------------------------------------------------------ #
#                            اجرای اصلی
# ------------------------------------------------------------------ #
async def async_main() -> None:
    args = parse_args()

    if args.url:
        url = args.url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        config = {
            "url": url,
            "headed": args.headed,
            "output": args.output,
            "proxy": args.proxy,
        }
    else:
        config = prompt_user()
        if args.output:
            config["output"] = args.output
        if args.proxy:
            config["proxy"] = args.proxy
        else:
            config["proxy"] = None

    print(f"\n⏳ در حال بارگذاری {config['url']} ...")
    try:
        data = await run_extraction(
            config["url"],
            headless=not config["headed"],
            proxy=config.get("proxy"),
        )
    except Exception as exc:
        print(f"\n❌ خطا: {exc}", file=sys.stderr)
        sys.exit(1)

    data["_extracted_at"] = datetime.now().isoformat(timespec="seconds")

    print_summary(data)

    if config.get("output"):
        save_to_json(data, config["output"])


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n\n⛔ لغو شد توسط کاربر.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
