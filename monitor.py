"""
MSD Service Obligation Schedule Monitor
Automatically logs in, checks for open slots, and sends Telegram alerts.
"""

import os
import sys
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SCHOLARSHIP_CATEGORY = os.getenv("SCHOLARSHIP_CATEGORY", "")
CARECARD             = os.getenv("CARECARD", "")
PASSWORD             = os.getenv("PASSWORD", "")
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")

CALENDAR_URL = "https://msd.muntinlupacity.gov.ph/serveob/calendar"
LOGIN_URL    = "https://msd.muntinlupacity.gov.ph/serveob/login"

LOG_FILE = os.path.join(os.path.dirname(__file__), "monitor.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# Cloudflare / server error indicators
SITE_DOWN_MARKERS = [
    "origin is unreachable", "error 523", "error 522", "error 521",
    "error 502", "error 504", "this page isn't working",
    "web server is not reachable", "522: connection timed out",
]


class SiteDownError(Exception):
    """Raised when the MSD site is unreachable or returns an error page."""
    pass


class LoginFailedError(Exception):
    """Raised when login form loads but credentials are rejected."""
    pass


def validate_env():
    missing = [v for v in ["SCHOLARSHIP_CATEGORY","CARECARD","PASSWORD",
                            "TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"]
               if not os.getenv(v)]
    if missing:
        log.error("Missing required .env values: %s", ", ".join(missing))
        sys.exit(1)


async def send_telegram(message: str):
    import urllib.request, json
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("Telegram notification sent.")
            else:
                log.warning("Telegram returned non-ok: %s", result)
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)


async def login_and_get_slots():
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        # ── Navigate to login page ────────────────────────────────────────────
        log.info("Navigating to login page...")
        try:
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)
        except Exception as e:
            raise SiteDownError(f"Could not reach login page at all: {e}")

        # ── CHECK 1: Detect Cloudflare / server error pages ───────────────────
        page_text = await page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
        if any(marker in page_text for marker in SITE_DOWN_MARKERS):
            raise SiteDownError(
                "Login page returned a Cloudflare/server error. "
                f"Page preview: {page_text[:150].strip()}"
            )

        # ── CHECK 2: Login form must be present ───────────────────────────────
        try:
            await page.wait_for_selector("select", timeout=10_000)
            selects = await page.query_selector_all("select")
            if selects:
                await selects[0].select_option(label=SCHOLARSHIP_CATEGORY)
                log.info("Selected category: %s", SCHOLARSHIP_CATEGORY)
        except Exception:
            raise SiteDownError(
                "Login form did not load — dropdown not found within 10 seconds. "
                "Site is likely down or returning an incomplete page."
            )

        carecard_input = await page.query_selector(
            "input[placeholder*='arecard'], input[placeholder*='Carecard']"
        )
        if carecard_input:
            await carecard_input.fill(CARECARD)
        else:
            inputs = await page.query_selector_all("input[type='text'], input:not([type])")
            if inputs:
                await inputs[0].fill(CARECARD)

        pwd_input = await page.query_selector("input[type='password']")
        if not pwd_input:
            raise SiteDownError("Password field not found — login form is incomplete.")
        await pwd_input.fill(PASSWORD)

        sign_in = await page.query_selector(
            "button[type='submit'], input[type='submit'], "
            "button:has-text('Sign In'), a:has-text('Sign In')"
        )
        if sign_in:
            await sign_in.click()
        else:
            await page.keyboard.press("Enter")

        log.info("Login submitted, waiting for calendar...")

        try:
            await page.wait_for_url("**/calendar**", timeout=20_000)
        except PWTimeout:
            await page.goto(CALENDAR_URL, wait_until="networkidle", timeout=30_000)

        await page.wait_for_load_state("networkidle", timeout=20_000)
        current_url = page.url
        log.info("Landed on: %s", current_url)

        # ── CHECK 3: Confirm we left the login page ───────────────────────────
        if "login" in current_url.lower():
            raise LoginFailedError(
                "Still on login page after submitting — credentials may be wrong "
                "or the site rejected the login."
            )

        # ── CHECK 4: Calendar page must not be an error page ──────────────────
        cal_text = await page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
        if any(marker in cal_text for marker in SITE_DOWN_MARKERS):
            raise SiteDownError("Calendar page returned an error page — site is down.")

        # ── CHECK 5: Available Schedule panel must exist ──────────────────────
        if "available schedule" not in cal_text:
            raise SiteDownError(
                "Calendar page loaded but the 'Available Schedule' panel is missing — "
                "session may have been rejected or site is partially down."
            )

        log.info("Login and page load verified successfully.")
        await page.wait_for_timeout(4_000)

        # ── Step 1: Force-expand ALL collapse divs directly via JS ────────────
        expanded = await page.evaluate(r"""
() => {
    const DATE_RE = /(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}/;
    const dateLabels = [];
    const seen = new Set();

    const cards = document.querySelectorAll('#appointmentsAccordion > .card');
    let forcedOpen = 0;

    for (const card of cards) {
        const btn = card.querySelector('.card-header button');
        if (btn) {
            const m = (btn.innerText || '').trim().match(DATE_RE);
            if (m && !seen.has(m[0])) {
                seen.add(m[0]);
                dateLabels.push(m[0]);
            }
        }
        const collapseDiv = card.querySelector('.collapse');
        if (collapseDiv && !collapseDiv.classList.contains('show')) {
            collapseDiv.classList.add('show');
            forcedOpen++;
        }
    }

    return { dateLabels, forcedOpen, total: cards.length };
}
""")

        total_headers = len(expanded['dateLabels'])
        forced_open   = expanded['forcedOpen']

        log.info("=" * 50)
        log.info("Found %d date header(s) in the Available Schedule panel:", total_headers)
        for d in expanded['dateLabels']:
            log.info("  • %s", d)
        log.info("Force-expanded %d previously collapsed section(s).", forced_open)
        log.info("=" * 50)

        await page.wait_for_timeout(500)

        # ── Step 2: Read activity cards using precise selectors ───────────────
        slots_js = await page.evaluate(r"""
() => {
    const TIME_RE = /(\d{1,2}:\d{2}\s*(?:AM|PM))\s*[–\-]+\s*(\d{1,2}:\d{2}\s*(?:AM|PM))/i;
    const results = [];

    const dateCards = document.querySelectorAll('#appointmentsAccordion > .card');

    for (const dateCard of dateCards) {
        const btn = dateCard.querySelector('.card-header button');
        if (!btn) continue;
        const headerText = (btn.innerText || '').trim();
        const dateMatch = headerText.match(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}/);
        if (!dateMatch) continue;
        const dateStr = dateMatch[0];

        const headerHTML = dateCard.querySelector('.card-header').innerHTML || '';
        if (/cancelled/i.test(headerHTML)) continue;

        const collapseDiv = dateCard.querySelector('.collapse.show');
        if (!collapseDiv) continue;

        const activityCards = collapseDiv.querySelectorAll('.card-body .d-flex');

        for (const card of activityCards) {
            const mlDiv = card.querySelector('.ml-3');
            if (!mlDiv) continue;

            const h6 = mlDiv.querySelector('h6');
            if (!h6) continue;

            const nameText = Array.from(h6.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent.trim())
                .join(' ')
                .trim();

            if (!nameText || /^TEST\s*$/i.test(nameText)) continue;

            const timeSmall = h6.querySelector('small');
            const timeRaw = timeSmall ? (timeSmall.innerText || '').trim() : '';
            const timeMatch = timeRaw.match(TIME_RE);
            if (!timeMatch) continue;
            const timeStr = `${timeMatch[1]} – ${timeMatch[2]}`;

            let statusEl = null;
            for (const small of mlDiv.querySelectorAll('small.text-muted')) {
                if (small.parentElement === mlDiv) {
                    statusEl = small;
                    break;
                }
            }

            if (!statusEl) continue;

            const statusHTML = statusEl.innerHTML || '';
            const statusText = (statusEl.innerText || '').trim();

            if (/badge-danger/i.test(statusHTML) || /no slots available/i.test(statusText)) {
                continue;
            }

            const remainingMatch = statusText.match(/(\d+)\s+Remaining\s+Slot/i);
            const remaining = remainingMatch ? parseInt(remainingMatch[1]) : null;

            results.push({ date: dateStr, activity: nameText, time: timeStr, remaining });
        }
    }

    return results;
}
""")

        open_slots = slots_js if slots_js else []
        await browser.close()

    return open_slots, total_headers, forced_open


def format_slots_message(slots: list) -> str:
    lines = [
        "🔔 <b>MSD Schedule Alert!</b>",
        f"Checked: {datetime.now().strftime('%b %d, %Y %I:%M %p')}",
        "",
        f"✅ <b>{len(slots)} open slot(s) found!</b>",
        "",
    ]
    for s in slots:
        remaining_txt = f"  ({s['remaining']} slot(s) left)" if s.get("remaining") else ""
        lines.append(f"📅 <b>{s['date']}</b>")
        lines.append(f"🏢 {s['activity']}{remaining_txt}")
        lines.append(f"🕐 {s['time']}")
        lines.append("")
    lines.append(f"🔗 <a href='{CALENDAR_URL}'>Book now →</a>")
    return "\n".join(lines)


def format_error_message(error_type: str, detail: str) -> str:
    now = datetime.now().strftime('%b %d, %Y %I:%M %p')
    if error_type == "site_down":
        return (
            f"⚠️ <b>MSD Monitor — Site Down</b>\n"
            f"🕐 {now}\n\n"
            f"The MSD website appears to be unreachable or returning an error page.\n\n"
            f"<b>Detail:</b> {detail}\n\n"
            f"The script has stopped for this check. It will try again on the next scheduled run."
        )
    elif error_type == "login_failed":
        return (
            f"🔐 <b>MSD Monitor — Login Failed</b>\n"
            f"🕐 {now}\n\n"
            f"The script reached the login page but was not able to log in.\n\n"
            f"<b>Detail:</b> {detail}\n\n"
            f"Please check your Carecard number and password in the .env file."
        )
    else:
        return (
            f"❌ <b>MSD Monitor — Unexpected Error</b>\n"
            f"🕐 {now}\n\n"
            f"<b>Detail:</b> {detail}\n\n"
            f"The script has stopped for this check."
        )


async def main():
    validate_env()
    log.info("=" * 50)
    log.info("MSD Monitor starting — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    try:
        slots, total_headers, forced_open = await login_and_get_slots()

    except SiteDownError as e:
        log.error("SITE DOWN: %s", e)
        log.info("Sending Telegram alert about site being down...")
        await send_telegram(format_error_message("site_down", str(e)))
        log.info("=" * 50)
        log.info("Check aborted — site is down.")
        return

    except LoginFailedError as e:
        log.error("LOGIN FAILED: %s", e)
        log.info("Sending Telegram alert about login failure...")
        await send_telegram(format_error_message("login_failed", str(e)))
        log.info("=" * 50)
        log.info("Check aborted — login failed.")
        return

    except Exception as e:
        log.error("Unexpected error: %s", e, exc_info=True)
        await send_telegram(format_error_message("unknown", str(e)))
        log.info("=" * 50)
        log.info("Check aborted — unexpected error.")
        return

    log.info("=" * 50)
    log.info("SUMMARY")
    log.info("  Date headers found in panel : %d", total_headers)
    log.info("  Sections force-expanded     : %d", forced_open)

    if slots:
        log.info("  Open slots found            : %d  <-- ALERT SENT", len(slots))
        for s in slots:
            log.info("    -> %s | %s | %s | remaining=%s",
                     s["date"], s["activity"], s["time"], s.get("remaining"))
        await send_telegram(format_slots_message(slots))
    else:
        log.info("  Open slots found            : 0  (all say No Slots Available)")

    log.info("=" * 50)
    log.info("Check complete.")


if __name__ == "__main__":
    asyncio.run(main())