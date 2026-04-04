"""Browser control tools — Playwright-based web automation for the agent.

Provides browse_url, click_element, fill_form, screenshot, and extract_data
tools that let the agent interact with web pages like a human.

Requires: pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Shared browser instance (reused across tool calls within a session).
# Protected by _browser_lock to prevent concurrent initialization races.
_browser = None
_context = None
_page = None
_browser_lock = asyncio.Lock()


async def _ensure_browser():
    """Lazy-init a shared browser instance (lock-protected)."""
    global _browser, _context, _page
    if _page is not None:
        return _page

    async with _browser_lock:
        # Double-check after acquiring lock (another coroutine may have initialized)
        if _page is not None:
            return _page

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Browser tools require playwright. Install with: "
                "pip install playwright && playwright install chromium"
            )

        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True)
        _context = await _browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        _page = await _context.new_page()
        logger.info("Browser initialized (headless Chromium)")
        return _page


async def _close_browser():
    """Close the shared browser instance (lock-protected)."""
    global _browser, _context, _page
    async with _browser_lock:
        if _browser:
            try:
                await _browser.close()
            except Exception:
                pass
        _browser = None
        _context = None
        _page = None


def register_browser_tools(registry, workspace_dir: str) -> None:
    """Register browser control tools."""

    async def browse_url(params: dict) -> str:
        """Navigate to a URL and return page content."""
        url = params.get("url", "")
        if not url:
            return json.dumps({"error": "url is required"})

        wait_for = params.get("wait_for", "load")  # load, domcontentloaded, networkidle

        try:
            page = await _ensure_browser()
            await page.goto(url, wait_until=wait_for, timeout=30000)

            title = await page.title()
            # Extract visible text content
            text = await page.evaluate("() => document.body.innerText")
            # Truncate if too long
            if len(text) > 15000:
                text = text[:15000] + "\n... [truncated]"

            current_url = page.url

            return json.dumps({
                "url": current_url,
                "title": title,
                "content": text,
            }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"error": f"Failed to browse {url}: {str(e)}"})

    registry.register(
        name="browse_url",
        description="Open a URL in a headless browser and return page content. For dynamic sites requiring JS rendering.",
        parameters={
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "description": "Ochmoqchi bo'lgan URL"},
                "wait_for": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                    "description": "Kutish strategiyasi (default: load)",
                },
            },
        },
        handler=browse_url,
        category="browser",
    )

    async def click_element(params: dict) -> str:
        """Click an element on the current page."""
        selector = params.get("selector", "")
        text = params.get("text", "")
        if not selector and not text:
            return json.dumps({"error": "selector or text is required"})

        try:
            page = await _ensure_browser()

            if text and not selector:
                # Click by visible text
                locator = page.get_by_text(text, exact=False)
                await locator.first.click(timeout=10000)
            else:
                await page.click(selector, timeout=10000)

            # Wait for navigation/response
            await page.wait_for_load_state("domcontentloaded", timeout=10000)

            title = await page.title()
            return json.dumps({
                "success": True,
                "url": page.url,
                "title": title,
            })

        except Exception as e:
            return json.dumps({"error": f"Click failed: {str(e)}"})

    registry.register(
        name="click_element",
        description="Click an element on the current page by CSS selector or visible text.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector (masalan: 'button.submit', '#login-btn')"},
                "text": {"type": "string", "description": "Ko'rinadigan matn bo'yicha bosish (masalan: 'Kirish', 'Submit')"},
            },
        },
        handler=click_element,
        category="browser",
    )

    async def fill_form(params: dict) -> str:
        """Fill form fields on the current page."""
        fields = params.get("fields", {})
        submit = params.get("submit", False)
        submit_selector = params.get("submit_selector", "")

        if not fields:
            return json.dumps({"error": "fields is required (dict of selector: value)"})

        try:
            page = await _ensure_browser()

            filled = []
            for selector, value in fields.items():
                await page.fill(selector, str(value), timeout=10000)
                filled.append(selector)

            result = {"success": True, "filled": filled}

            if submit:
                if submit_selector:
                    await page.click(submit_selector, timeout=10000)
                else:
                    await page.keyboard.press("Enter")
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                result["submitted"] = True
                result["url"] = page.url
                result["title"] = await page.title()

            return json.dumps(result)

        except Exception as e:
            return json.dumps({"error": f"Form fill failed: {str(e)}"})

    registry.register(
        name="fill_form",
        description="Fill form fields on the current page. Provide CSS selector and value for each field.",
        parameters={
            "type": "object",
            "required": ["fields"],
            "properties": {
                "fields": {
                    "type": "object",
                    "description": "Selector → qiymat juftliklari. Masalan: {'#email': 'user@mail.com', '#password': '123'}",
                },
                "submit": {"type": "boolean", "description": "Formani yuborish (Enter bosish)"},
                "submit_selector": {"type": "string", "description": "Yuborish tugmasi selectori (agar oddiy Enter ishlamasa)"},
            },
        },
        handler=fill_form,
        category="browser",
    )

    async def screenshot(params: dict) -> str:
        """Take a screenshot of the current page."""
        full_page = params.get("full_page", False)
        selector = params.get("selector", "")

        try:
            page = await _ensure_browser()

            # Save screenshot to workspace
            screenshots_dir = os.path.join(workspace_dir, "screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)

            import time
            filename = f"screenshot_{int(time.time())}.png"
            filepath = os.path.join(screenshots_dir, filename)

            if selector:
                element = page.locator(selector)
                await element.screenshot(path=filepath, timeout=10000)
            else:
                await page.screenshot(path=filepath, full_page=full_page)

            # Also return base64 for inline display
            with open(filepath, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")

            file_size = os.path.getsize(filepath)

            return json.dumps({
                "success": True,
                "path": f"screenshots/{filename}",
                "size": file_size,
                "url": page.url,
                "title": await page.title(),
                "base64_preview": b64[:200] + "..." if len(b64) > 200 else b64,
            })

        except Exception as e:
            return json.dumps({"error": f"Screenshot failed: {str(e)}"})

    registry.register(
        name="screenshot",
        description="Take a screenshot of the current page. Can be sent to user via send_file.",
        parameters={
            "type": "object",
            "properties": {
                "full_page": {"type": "boolean", "description": "To'liq sahifa (scroll bilan)"},
                "selector": {"type": "string", "description": "Faqat ma'lum element (CSS selector)"},
            },
        },
        handler=screenshot,
        category="browser",
    )

    async def extract_data(params: dict) -> str:
        """Extract structured data from the current page."""
        selector = params.get("selector", "body")
        attribute = params.get("attribute", "")
        all_matches = params.get("all", False)

        try:
            page = await _ensure_browser()

            if all_matches:
                elements = page.locator(selector)
                count = await elements.count()
                results = []
                for i in range(min(count, 50)):  # Cap at 50
                    el = elements.nth(i)
                    if attribute:
                        val = await el.get_attribute(attribute)
                    else:
                        val = await el.inner_text()
                    results.append(val)
                return json.dumps({"count": count, "data": results}, ensure_ascii=False)
            else:
                element = page.locator(selector).first
                if attribute:
                    val = await element.get_attribute(attribute)
                else:
                    val = await element.inner_text()

                if isinstance(val, str) and len(val) > 10000:
                    val = val[:10000] + "\n... [truncated]"

                return json.dumps({"data": val}, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"error": f"Extract failed: {str(e)}"})

    registry.register(
        name="extract_data",
        description="Extract data from the current page. Get element text content or attributes by CSS selector.",
        parameters={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector (default: body)"},
                "attribute": {"type": "string", "description": "Element atributi (masalan: href, src). Bo'sh = innerText"},
                "all": {"type": "boolean", "description": "Barcha mos elementlarni olish (max 50)"},
            },
        },
        handler=extract_data,
        category="browser",
    )

    async def browser_back(params: dict) -> str:
        """Go back in browser history."""
        try:
            page = await _ensure_browser()
            await page.go_back(timeout=10000)
            return json.dumps({
                "url": page.url,
                "title": await page.title(),
            })
        except Exception as e:
            return json.dumps({"error": f"Back navigation failed: {str(e)}"})

    registry.register(
        name="browser_back",
        description="Go back in browser history.",
        parameters={"type": "object", "properties": {}},
        handler=browser_back,
        category="browser",
    )

    logger.info("Browser tools registered: browse_url, click_element, fill_form, screenshot, extract_data, browser_back")
