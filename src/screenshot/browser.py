"""Playwright browser management and post screenshots."""

import io
import time
from src.utils.LoggerManager import logger as log
from typing import Optional

from playwright.sync_api import sync_playwright

from src.core.config import PW_CONTEXT_MAX_PAGES
from src.core.utils import mid_to_base62
from src.api.client import parse_cookies
from src.api.cookie_store import CookieStore


# ---------------------------------------------------------------------------
# Playwright Screenshots
# ---------------------------------------------------------------------------

_pw = None
_pw_browser = None
_pw_context = None
_pw_page_count = 0
_PW_MAX_PAGES = PW_CONTEXT_MAX_PAGES
_cookie_store: Optional[CookieStore] = None


def configure_cookie_store(store: CookieStore) -> None:
    """Set the shared cookie store before the browser is first used."""
    global _cookie_store
    _cookie_store = store


def _save_context_cookies() -> None:
    if not _pw_context or not _cookie_store:
        return
    try:
        _cookie_store.merge_from_playwright(_pw_context.cookies())
    except Exception as e:
        log.warning(f"Failed to persist Playwright cookies: {e}")


def _get_browser(force_new=False):
    """Lazy-init Playwright browser. Recreate context periodically."""
    global _pw, _pw_browser, _pw_context, _pw_page_count

    if _cookie_store:
        pw_cookies = _cookie_store.playwright_cookies()
    else:
        _, pw_cookies = parse_cookies()

    if force_new and _pw_context:
        try:
            _save_context_cookies()
            _pw_context.close()
        except Exception as e:
            log.debug(f"Closing old Playwright context: {e}")
        _pw_context = None
        _pw_page_count = 0

    if _pw_context and _pw_page_count < _PW_MAX_PAGES:
        return _pw_context

    if _pw_context:
        try:
            _save_context_cookies()
            _pw_context.close()
        except Exception as e:
            log.debug(f"Closing expired Playwright context: {e}")
        _pw_context = None

    if not _pw:
        _pw = sync_playwright().start()
    if not _pw_browser or not _pw_browser.is_connected():
        _pw_browser = _pw.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage"],
        )

    _pw_context = _pw_browser.new_context(
        viewport={"width": 1000, "height": 900},
        device_scale_factor=2,
        locale="zh-CN",
    )

    # Inject cookies
    if pw_cookies:
        _pw_context.add_cookies(pw_cookies)

    _pw_page_count = 0
    log.info("Created new Playwright browser context")
    time.sleep(2)
    return _pw_context


def cleanup_playwright():
    """Clean up Playwright resources. Called during shutdown."""
    global _pw, _pw_browser, _pw_context
    try:
        if _pw_context:
            _save_context_cookies()
            _pw_context.close()
        if _pw_browser:
            _pw_browser.close()
        if _pw:
            _pw.stop()
        log.info("Playwright cleaned up")
    except Exception as e:
        log.warning(f"Playwright cleanup error: {e}")


def refresh_weibo_session() -> bool:
    """Visit a first-party page so Weibo can rotate session cookies."""
    global _pw_page_count
    page = None
    try:
        context = _get_browser()
        page = context.new_page()
        response = page.goto(
            "https://weibo.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(5000)
        final_url = page.url.lower()
        if "passport.weibo" in final_url or "login.sina" in final_url:
            log.warning("Weibo session refresh reached a login page")
            return False
        if response and response.status >= 400:
            log.warning(f"Weibo session refresh returned HTTP {response.status}")
            return False
        _save_context_cookies()
        _pw_page_count += 1
        log.info("Weibo browser session refreshed")
        return True
    except Exception as e:
        log.warning(f"Weibo session refresh failed: {e}")
        return False
    finally:
        if page:
            try:
                page.close()
            except Exception as e:
                log.debug(f"Closing session refresh page: {e}")
        _save_context_cookies()


def screenshot_post(mid: str, uid: str = "", _retry=0) -> Optional[io.BytesIO]:
    """Take a screenshot of a Weibo post on desktop weibo.com.
    Navigates to the post's permalink and screenshots the article element."""
    global _pw_page_count

    # Desktop post URL: weibo.com/{uid}/{mid_base62}
    mid_b62 = mid_to_base62(mid)
    url = f"https://weibo.com/{uid}/{mid_b62}" if uid else f"https://weibo.com/detail/{mid}"
    page = None

    try:
        ctx = _get_browser()
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Wait for the post article to render
        try:
            page.wait_for_selector(
                "article",
                timeout=15000,
                state="visible",
            )
        except Exception:
            log.warning(f"Article not found for {mid}, waiting fallback...")
            page.wait_for_timeout(5000)

        # Wait for images to load
        page.evaluate("""() => {
            const article = document.querySelector('article');
            if (!article) return Promise.resolve();
            return Promise.all(
                Array.from(article.querySelectorAll('img[src]')).map(img => {
                    if (img.complete && img.naturalHeight > 0) return Promise.resolve();
                    return new Promise(resolve => {
                        img.addEventListener('load', resolve);
                        img.addEventListener('error', resolve);
                        setTimeout(resolve, 8000);
                    });
                })
            );
        }""")

        page.wait_for_timeout(1500)

        # Remove distracting overlays and junk elements
        page.evaluate("""() => {
            // Remove fixed/sticky overlays
            document.querySelectorAll('*').forEach(el => {
                const style = getComputedStyle(el);
                if ((style.position === 'fixed' || style.position === 'sticky') &&
                    el.tagName !== 'HTML' && el.tagName !== 'BODY') {
                    el.remove();
                }
            });
            // Remove login/download dialogs
            document.querySelectorAll('[class*="login"], [class*="mask"], [class*="dialog"]').forEach(el => el.remove());

            // Inside article: clean up junk while keeping footer toolbar
            // Structure: article > [div.公开, div.body, div.点赞是美意, footer.转发评论点赞]
            const article = document.querySelector('article');
            if (article) {
                // 1. Remove direct children that are junk (公开, 点赞是美意/为TA助威)
                //    Keep: body div (has avatar/text) and footer (has toolbar)
                Array.from(article.children).forEach(child => {
                    const text = (child.textContent || '').trim();
                    const tag = child.tagName;
                    if (tag === 'FOOTER') return; // keep toolbar
                    if (text.includes('点赞是美意') || text.includes('为TA助威')) {
                        child.remove();
                        return;
                    }
                    // Remove "公开" wrapper (usually first div, short text)
                    if (text === '公开' || (text.length < 10 && text.includes('公开'))) {
                        child.remove();
                        return;
                    }
                });
                // 2. Inside footer: remove "分享这条博文" text element only
                const footer = article.querySelector('footer');
                if (footer) {
                    footer.querySelectorAll('*').forEach(el => {
                        const text = (el.textContent || '').trim();
                        if (text === '分享这条博文' && el.children.length === 0) {
                            // Remove this leaf + its parent wrapper if it's small
                            const parent = el.parentElement;
                            if (parent && parent.tagName !== 'FOOTER' &&
                                (parent.textContent || '').trim() === '分享这条博文') {
                                parent.remove();
                            } else {
                                el.remove();
                            }
                        }
                    });
                }
                // 3. Force ALL footer toolbar icons/text to grey (detail page highlights comment)
                if (footer) {
                    const grey = 'rgb(153, 162, 170)';
                    footer.querySelectorAll('*').forEach(el => {
                        el.style.color = grey;
                        if (el.tagName === 'svg' || el.tagName === 'SVG' ||
                            el.tagName === 'path' || el.tagName === 'PATH') {
                            el.style.fill = grey;
                            el.setAttribute('fill', grey);
                        }
                    });
                    footer.style.color = grey;
                }
            }
        }""")

        page.wait_for_timeout(500)

        # Screenshot the first article element (the post card)
        article = page.query_selector("article")
        if article:
            screenshot_bytes = article.screenshot(type="png")
        else:
            log.warning(f"No article element found for {mid}, full page fallback")
            screenshot_bytes = page.screenshot(type="png", full_page=True)

        _pw_page_count += 1
        page.close()
        _save_context_cookies()
        return io.BytesIO(screenshot_bytes)

    except Exception as e:
        log.error(f"Screenshot failed for {mid}: {e}")
        if page:
            try:
                page.close()
            except Exception as e:
                log.debug(f"Closing page after screenshot error: {e}")
        if _retry < 2:
            log.info(f"Retrying screenshot for {mid} with fresh context (attempt {_retry + 2})...")
            time.sleep(3)
            _get_browser(force_new=True)
            return screenshot_post(mid, uid, _retry=_retry + 1)
        return None
