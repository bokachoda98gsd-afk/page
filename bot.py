import os
import re
import json
import uuid
import random
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)
from telegram.error import Conflict, RetryAfter, TimedOut, NetworkError
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ============================================================
#  LOGGING
# ============================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", 0))

# ============================================================
#  PROXY SUPPORT  ⭐ REQUIRED FOR RAILWAY
#  Set PROXY_URL in Railway env vars.
#  Formats accepted:
#    http://user:pass@ip:port
#    socks5://user:pass@ip:port
#    ip:port
# ============================================================
PROXY_URL = os.getenv("PROXY_URL", "").strip()
PROXY_USER = os.getenv("PROXY_USER", "").strip()
PROXY_PASS = os.getenv("PROXY_PASS", "").strip()


def get_proxy_config():
    """Return Playwright proxy dict or None."""
    if not PROXY_URL:
        return None
    url = PROXY_URL
    # If user provided "ip:port" style, assume http
    if not url.startswith(("http://", "https://", "socks5://", "socks4://")):
        url = f"http://{url}"
    # Strip embedded credentials (Playwright wants them separately)
    if "@" in url:
        scheme_rest = url.split("://", 1)
        scheme = scheme_rest[0]
        rest = scheme_rest[1]
        creds, host = rest.split("@", 1)
        if ":" in creds:
            user, pwd = creds.split(":", 1)
            return {"server": f"{scheme}://{host}", "username": user, "password": pwd}
    cfg = {"server": url}
    if PROXY_USER:
        cfg["username"] = PROXY_USER
    if PROXY_PASS:
        cfg["password"] = PROXY_PASS
    return cfg


# ============================================================
#  HEADLESS
# ============================================================
HEADLESS = True

# ============================================================
#  CONCURRENCY
# ============================================================
MAX_CONCURRENT_BROWSERS = int(os.getenv("MAX_CONCURRENT_BROWSERS", 1))

# ============================================================
#  CHROMIUM ARGS (Railway-optimized, low-memory)
# ============================================================
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-component-update",
    "--disable-client-side-phishing-detection",
    "--mute-audio",
    "--no-first-run",
    "--no-zygote",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
    "--metrics-recording-only",
    "--single-process",
    "--js-flags=--max-old-space-size=256",
    "--window-size=1280,800",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# ============================================================
#  STORAGE
# ============================================================
DATA_DIR = os.getenv("DATA_DIR", ".")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SCREENSHOT_DIR = os.path.join(DATA_DIR, "screenshots")
Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)

running_tasks = {}
BROWSER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)

MAX_MSG = 3800
MAX_ERR = 300
POST_CREATE_WAIT_MS = 20000

(ASK_COOKIE, ASK_COUNT, ASK_INTERVAL,
 EDIT_CHOICE, EDIT_INTERVAL, EDIT_TARGET) = range(6)


# ============================================================
#  PLAYWRIGHT HEALTH CHECK
# ============================================================
async def check_playwright():
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            version = browser.version
            await browser.close()
            logger.info(f"✅ Playwright OK — Chromium {version}")
            return True, version
    except Exception as e:
        logger.error(f"❌ Playwright FAILED: {e}")
        return False, str(e)


# ============================================================
#  USER WHITELIST
# ============================================================
def load_users() -> set:
    if not Path(USERS_FILE).exists():
        return set()
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {int(x) for x in data if str(x).lstrip("-").isdigit()}
        return set()
    except Exception:
        return set()


def save_users(users: set):
    try:
        Path(USERS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(USERS_FILE, "w") as f:
            json.dump(sorted(int(u) for u in users), f, indent=2)
    except Exception as e:
        logger.error(f"save_users: {e}")


def is_admin(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID


def is_authorized(user_id: int) -> bool:
    if user_id is None:
        return False
    if is_admin(user_id):
        return True
    return user_id in load_users()


def authorized(update: Update) -> bool:
    return bool(update.effective_user and is_authorized(update.effective_user.id))


def cb_authorized(update: Update) -> bool:
    q = update.callback_query
    return bool(q and q.from_user and is_authorized(q.from_user.id))


async def deny_message(update: Update):
    try:
        await update.effective_message.reply_text(
            "⛔ Access denied.\nThis bot is private."
        )
    except Exception:
        pass


async def deny_callback(update: Update):
    try:
        await update.callback_query.answer("⛔ Access denied.", show_alert=True)
    except Exception:
        pass


# ============================================================
#  HELPERS
# ============================================================
def truncate(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def sanitize_error(err) -> str:
    if not err:
        return ""
    s = str(err).replace("`", "'").replace("\n", " | ").replace("\r", " ")
    return truncate(s, MAX_ERR)


def truncate_msg(text: str) -> str:
    return text if len(text) <= MAX_MSG else text[: MAX_MSG - 20] + "\n\n…(truncated)"


def extract_c_user(cookie_str: str) -> str:
    try:
        m = re.search(r"(?:^|;\s*)c_user=(\d+)", cookie_str)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


async def safe_edit(q, text, reply_markup=None, parse_mode="Markdown"):
    text = truncate_msg(text)
    try:
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            await q.edit_message_text(text, reply_markup=reply_markup)
        except Exception:
            pass
    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ["parse", "entit", "bad request", "not modified"]):
            return
        try:
            await q.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            pass


# ============================================================
#  ADMIN COMMANDS
# ============================================================
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            f"🆔 Your Telegram ID: `{update.effective_user.id}`",
            parse_mode="Markdown")
    except Exception:
        pass


async def cmd_headless(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global HEADLESS
    if not authorized(update) or not is_admin(update.effective_user.id):
        await deny_message(update)
        return
    raw = (update.message.text or "").strip().lower()
    val = raw.split("=", 1)[1].strip() if "=" in raw else (ctx.args[0] if ctx.args else "")
    if val in ("true", "1", "on", "yes"):
        HEADLESS = True
        await update.message.reply_text("✅ HEADLESS = True")
    elif val in ("false", "0", "off", "no"):
        HEADLESS = False
        await update.message.reply_text("✅ HEADLESS = False (needs Xvfb)")
    else:
        await update.message.reply_text(f"HEADLESS = {HEADLESS}")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update) or not is_admin(update.effective_user.id):
        await deny_message(update)
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/add <user_id>`", parse_mode="Markdown")
        return
    try:
        new_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if is_admin(new_id):
        await update.message.reply_text("ℹ️ That's the admin.")
        return
    users = load_users()
    if new_id in users:
        await update.message.reply_text(f"ℹ️ Already authorized.")
        return
    users.add(new_id)
    save_users(users)
    await update.message.reply_text(f"✅ Added `{new_id}`.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(new_id, "✅ Access granted. Send /start.")
    except Exception:
        pass


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update) or not is_admin(update.effective_user.id):
        await deny_message(update)
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/remove <user_id>`", parse_mode="Markdown")
        return
    try:
        rid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if is_admin(rid):
        await update.message.reply_text("❌ Cannot remove admin.")
        return
    users = load_users()
    users.discard(rid)
    save_users(users)
    await update.message.reply_text(f"🗑️ Removed `{rid}`.", parse_mode="Markdown")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update) or not is_admin(update.effective_user.id):
        await deny_message(update)
        return
    users = load_users()
    lines = [f"👑 *Admin:* `{ALLOWED_USER_ID}`", ""]
    if not users:
        lines.append("_No additional users._")
    else:
        lines.append(f"✅ *{len(users)} authorized:*")
        for u in sorted(users):
            lines.append(f"• `{u}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Full diagnostics."""
    if not authorized(update) or not is_admin(update.effective_user.id):
        await deny_message(update)
        return
    await update.message.reply_text("🧪 Running diagnostics...")

    # 1. Playwright test
    ok, info = await check_playwright()
    lines = ["🔍 *Diagnostics*", ""]

    if ok:
        lines.append(f"✅ Playwright: Chromium `{info}`")
    else:
        lines.append(f"❌ Playwright: `{info[:150]}`")

    # 2. Proxy status
    proxy = get_proxy_config()
    if proxy:
        lines.append(f"✅ Proxy: `{proxy['server']}`")
    else:
        lines.append("⚠️ *No proxy set* — FB will likely block Railway's IP.")
        lines.append("Set `PROXY_URL` in Railway env vars.")

    # 3. Memory / storage
    lines.append(f"📂 Data dir: `{DATA_DIR}`")
    lines.append(f"🔒 Max browsers: `{MAX_CONCURRENT_BROWSERS}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # 4. Live browser test (navigate to FB)
    await update.message.reply_text("🌐 Testing Facebook reachability...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            ctx_p = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx_p.new_page()
            await page.goto("https://www.facebook.com/",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            url = page.url
            title = await page.title()
            shot = os.path.join(SCREENSHOT_DIR, f"debug_{int(datetime.now().timestamp())}.png")
            await page.screenshot(path=shot)
            await browser.close()
            await update.message.reply_text(
                f"📍 Final URL: `{url}`\n📄 Title: `{title}`\n\n"
                f"📸 Screenshot saved on server.",
                parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ FB test failed:\n`{sanitize_error(e)}`",
                                        parse_mode="Markdown")


# ============================================================
#  STORAGE (tasks)
# ============================================================
def load_tasks() -> dict:
    if not Path(TASKS_FILE).exists():
        return {}
    try:
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_tasks(tasks: dict):
    try:
        Path(TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(TASKS_FILE, "w") as f:
            json.dump(tasks, f, indent=2)
    except Exception as e:
        logger.error(f"save_tasks: {e}")


def get_task(tid):
    return load_tasks().get(tid)


def update_task(tid, **kwargs):
    tasks = load_tasks()
    if tid in tasks:
        tasks[tid].update(kwargs)
        save_tasks(tasks)


def delete_task(tid):
    tasks = load_tasks()
    tasks.pop(tid, None)
    save_tasks(tasks)


# ============================================================
#  COOKIES / NAME
# ============================================================
def parse_cookies(cookie_str: str):
    cookies = []
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip()
        cookies.append({
            "name": name, "value": value,
            "domain": ".facebook.com", "path": "/",
            "http_only": name in ("xs", "c_user"),
            "secure": True, "same_site": "None",
        })
    return cookies


ADJ = ["Cosmic", "Vibrant", "Prime", "Nova", "Urban", "Wild",
       "Golden", "Silent", "Royal", "Bright", "Neon", "Solar"]
NOUN = ["Media", "Vibes", "Studios", "Hub", "Zone", "World",
        "Realm", "Wave", "Pulse", "Circle", "Sphere", "Nest"]


def random_page_name() -> str:
    return f"{random.choice(ADJ)} {random.choice(NOUN)} {random.randint(100, 999)}"


STATUS_ICON = {"idle": "⚪", "running": "🟢", "paused": "⏸️",
               "completed": "✅", "failed": "❌"}


def humanize_remaining(seconds):
    if seconds <= 0:
        return "starting..."
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# ============================================================
#  CATEGORY
# ============================================================
async def is_category_valid(page, input_loc) -> bool:
    try:
        if (await input_loc.get_attribute("aria-invalid")) == "true":
            return False
    except Exception:
        pass
    try:
        footer = page.locator('[aria-label="Create Page"]').last
        if (await footer.get_attribute("aria-disabled")) == "true":
            return False
    except Exception:
        pass
    return True


async def fill_category(page, input_loc, category_text="Entertainment", max_retries=3) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            wrapper = page.locator('label:has(input[aria-label="Category (required)"])').first
            try:
                await wrapper.scroll_into_view_if_needed()
                await wrapper.click(timeout=4000)
            except Exception:
                await input_loc.click(force=True, timeout=4000)
            await page.wait_for_timeout(500)
            try:
                await input_loc.fill("")
            except Exception:
                await input_loc.press("Control+A")
                await input_loc.press("Delete")
            await page.wait_for_timeout(400)
            await input_loc.focus()
            await page.wait_for_timeout(200)
            await input_loc.type(category_text, delay=140)

            ok = False
            for _ in range(25):
                if await page.locator('[role="option"]').count() > 0:
                    ok = True
                    break
                await page.wait_for_timeout(300)
            if not ok:
                continue
            await page.wait_for_timeout(600)

            try:
                await input_loc.press("ArrowDown")
                await page.wait_for_timeout(600)
                await input_loc.press("Enter")
                await page.wait_for_timeout(1800)
                if await is_category_valid(page, input_loc):
                    return True
            except Exception:
                pass

            for text in ["Entertainment website", "Entertainment Website", "Entertainment"]:
                try:
                    opt = page.locator(f'[role="option"]:text-matches("{text}", "i")').first
                    if await opt.count() > 0 and await opt.is_visible():
                        await opt.click(timeout=5000)
                        await page.wait_for_timeout(1800)
                        if await is_category_valid(page, input_loc):
                            return True
                except Exception:
                    continue

            try:
                first_opt = page.locator('[role="option"]').first
                if await first_opt.count() > 0 and await first_opt.is_visible():
                    await first_opt.click(timeout=5000)
                    await page.wait_for_timeout(1800)
                    if await is_category_valid(page, input_loc):
                        return True
            except Exception:
                pass

            await page.wait_for_timeout(800)
        except Exception as e:
            logger.warning(f"[fill_category] attempt {attempt}: {e}")
            await page.wait_for_timeout(1000)
    return False


# ============================================================
#  CREATE ONE PAGE  (with detailed step logging + proxy)
# ============================================================
async def create_one_page(cookie: str, log=None, tid="unknown"):
    async def note(msg):
        logger.info(f"[{tid}] {msg}")
        if log:
            try:
                await log(msg)
            except Exception:
                pass

    page_name = random_page_name()
    proxy = get_proxy_config()
    if proxy:
        await note(f"🌐 Using proxy: {proxy['server']}")
    else:
        await note("⚠️ No proxy — datacenter IP may be blocked")

    async with BROWSER_SEMAPHORE:
        async with async_playwright() as p:
            await note("🚀 Launching Chromium...")
            try:
                browser = await p.chromium.launch(
                    headless=HEADLESS,
                    slow_mo=0,
                    args=CHROMIUM_ARGS,
                    proxy=proxy,
                )
            except Exception as e:
                await note(f"❌ Launch failed: {sanitize_error(e)}")
                raise

            page = None
            context = None
            try:
                context = await browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                await context.add_cookies(parse_cookies(cookie))
                page = await context.new_page()
                page.set_default_timeout(30000)

                # 1. LOGIN CHECK
                await note("🌐 Loading facebook.com...")
                try:
                    await page.goto("https://www.facebook.com/",
                                    wait_until="domcontentloaded", timeout=60000)
                except PWTimeout:
                    raise RuntimeError("Timeout loading facebook.com (proxy slow or blocked)")
                await page.wait_for_timeout(4000)

                current_url = page.url
                await note(f"📍 URL after load: {current_url[:80]}")

                if "/login" in current_url:
                    raise RuntimeError("COOKIE EXPIRED (redirected to /login)")
                if "checkpoint" in current_url:
                    raise RuntimeError("CHECKPOINT required (FB flagged this session/IP)")
                if "captcha" in current_url.lower():
                    raise RuntimeError("CAPTCHA challenge (FB flagged this IP)")

                # 2. PAGES HOME
                await note("📄 Opening Pages...")
                await page.goto(
                    "https://www.facebook.com/pages/?category=top&ref=bookmarks",
                    wait_until="domcontentloaded", timeout=60000,
                )
                await page.wait_for_timeout(4000)

                if "/login" in page.url or "checkpoint" in page.url:
                    raise RuntimeError(f"Redirected after Pages: {page.url[:60]}")

                # 3. CREATE PAGE
                await note("🖱️ Clicking Create Page...")
                btn = page.locator('[aria-label="Create Page"]').first
                await btn.wait_for(state="visible", timeout=20000)
                await btn.click()
                await page.wait_for_timeout(3500)

                # 4. PUBLIC PAGE
                await note("🖱️ Selecting Public Page...")
                public_label = page.locator('label:has-text("Public Page")').first
                await public_label.wait_for(state="visible", timeout=15000)
                await public_label.click()
                await page.wait_for_timeout(1200)

                # 5. NEXT
                await note("🖱️ Next...")
                await page.locator('[aria-label="Next"]').first.click()
                await page.wait_for_timeout(3000)

                # 6. GET STARTED
                await note("🖱️ Get started...")
                try:
                    await page.locator('[aria-label="Get started"]').first.click(timeout=10000)
                except PWTimeout:
                    await page.locator('a:has-text("Get started")').first.click(timeout=10000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(4500)

                # 7. PAGE NAME
                await note(f"✍️ Name: {page_name}")
                name_input = page.locator('input[type="text"]').first
                await name_input.wait_for(state="visible", timeout=15000)
                try:
                    await page.locator('label:has-text("Page name")').first.click(timeout=4000)
                except Exception:
                    await name_input.click(force=True)
                await page.wait_for_timeout(400)
                await name_input.fill(page_name)
                await page.wait_for_timeout(1000)

                # 8. CATEGORY
                await note("🔍 Category: Entertainment...")
                cat_input = page.locator('input[aria-label="Category (required)"]').first
                await cat_input.wait_for(state="attached", timeout=20000)
                if not await fill_category(page, cat_input, "Entertainment", max_retries=3):
                    raise RuntimeError("Category selection failed.")

                # 9. FOOTER CREATE
                await note("🖱️ Creating page...")
                footer_btn = page.locator('[aria-label="Create Page"]').last
                for _ in range(15):
                    if (await footer_btn.get_attribute("aria-disabled")) != "true":
                        break
                    await page.wait_for_timeout(500)
                await footer_btn.click()

                await note("⏳ Waiting 20s...")
                await page.wait_for_timeout(POST_CREATE_WAIT_MS)

                await note("🔄 Refreshing...")
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    logger.warning(f"[reload] {e}")
                await page.wait_for_timeout(4000)

                final_url = page.url
                await note("✅ Page created.")

                return {"name": page_name, "url": final_url, "success": True}

            except Exception as e:
                # Debug screenshot on failure
                try:
                    if page:
                        shot = os.path.join(
                            SCREENSHOT_DIR,
                            f"{tid}_{int(datetime.now().timestamp())}.png"
                        )
                        await page.screenshot(path=shot, full_page=True)
                        logger.error(f"📸 Screenshot: {shot}")
                except Exception:
                    pass
                raise
            finally:
                try:
                    if context:
                        await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass


# ============================================================
#  TASK RUNNER
# ============================================================
async def run_task(bot, tid: str):
    chat_id = None
    try:
        t = get_task(tid)
        if not t:
            return
        chat_id = t["chat_id"]
        user_id = t.get("user_id") or extract_c_user(t.get("cookie", "")) or "unknown"

        while True:
            t = get_task(tid)
            if not t:
                return
            interval_sec = max(1, int(t.get("interval_min", 15))) * 60

            if t.get("status") == "paused":
                await asyncio.sleep(5)
                continue
            if t.get("created", 0) >= t.get("target", 0):
                update_task(tid, status="completed", next_run_at=None)
                try:
                    await bot.send_message(
                        chat_id,
                        f"🎉 *{t.get('name')}* completed — {t.get('target')} pages.",
                        parse_mode="Markdown")
                except Exception:
                    pass
                return

            update_task(tid, status="running", current_step="Starting...", error=None)

            async def log(msg):
                update_task(tid, current_step=msg)

            try:
                result = await create_one_page(t["cookie"], log=log, tid=tid)
            except Exception as e:
                err = sanitize_error(e)
                update_task(tid, status="failed", error=err, current_step="Failed")
                try:
                    await bot.send_message(
                        chat_id,
                        f"💥 *{t.get('name')}* failed:\n`{err}`\n\n"
                        f"Tap 📋 Task → select to retry, edit, or delete.",
                        parse_mode="Markdown")
                except Exception:
                    pass
                return

            allt = load_tasks()
            t2 = allt.get(tid)
            if not t2:
                return
            t2["created"] = t2.get("created", 0) + 1
            t2["history"] = (t2.get("history") or []) + [{
                "name": result["name"], "url": result["url"],
                "time": datetime.now().isoformat(),
            }]
            t2["status"] = "running" if t2["created"] < t2["target"] else "completed"
            t2["current_step"] = "Idle"
            t2["last_page"] = result["name"]
            t2["error"] = None
            t2["next_run_at"] = (datetime.now() + timedelta(seconds=interval_sec)).isoformat()
            allt[tid] = t2
            save_tasks(allt)

            try:
                await bot.send_message(
                    chat_id,
                    f"✅ Page {t2['created']}/{t2['target']} created!\n"
                    f"👤 user : {user_id}\n"
                    f"📛 {result['name']}")
            except Exception:
                pass

            if t2["created"] >= t2["target"]:
                update_task(tid, status="completed", next_run_at=None)
                try:
                    await bot.send_message(
                        chat_id,
                        f"🎉 *{t2['name']}* completed — all {t2['target']} pages done!",
                        parse_mode="Markdown")
                except Exception:
                    pass
                return

            end_time = datetime.now() + timedelta(seconds=interval_sec)
            paused_at = None
            while True:
                t3 = get_task(tid)
                if not t3:
                    return
                if t3.get("status") == "paused":
                    if paused_at is None:
                        paused_at = datetime.now()
                    await asyncio.sleep(5)
                    continue
                if paused_at is not None:
                    end_time += (datetime.now() - paused_at)
                    paused_at = None
                if datetime.now() >= end_time:
                    break
                rem = int((end_time - datetime.now()).total_seconds())
                update_task(tid, next_run_at=(datetime.now() + timedelta(seconds=rem)).isoformat())
                await asyncio.sleep(10)

    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"[run_task] {e}")
        if chat_id:
            try:
                await bot.send_message(chat_id, f"💥 Task crashed: {sanitize_error(e)}")
            except Exception:
                pass
    finally:
        running_tasks.pop(tid, None)


# ============================================================
#  KEYBOARDS / TEXT
# ============================================================
def main_menu_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("📋 Task")]], resize_keyboard=True)


def task_list_kb(tasks):
    rows = []
    for tid, t in tasks.items():
        icon = STATUS_ICON.get(t.get("status", "idle"), "⚪")
        rows.append([InlineKeyboardButton(
            f"{icon} {t.get('name', 'Task')} ({t.get('created', 0)}/{t.get('target', '?')})",
            callback_data=f"task:{tid}")])
    rows.append([InlineKeyboardButton("➕ Create a Task", callback_data="create_task")])
    rows.append([InlineKeyboardButton("🔄 Refresh Now", callback_data="refresh_tasks")])
    return InlineKeyboardMarkup(rows)


def task_detail_kb(tid, status):
    rows = []
    if status in ("idle", "failed", "paused"):
        rows.append([InlineKeyboardButton("▶️ Start", callback_data=f"start:{tid}")])
    if status == "running":
        rows.append([InlineKeyboardButton("⏸️ Pause", callback_data=f"pause:{tid}")])
    rows.append([InlineKeyboardButton("✏️ Edit Config", callback_data=f"edit:{tid}")])
    rows.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"delete:{tid}")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_tasks")])
    return InlineKeyboardMarkup(rows)


def task_list_text(tasks):
    try:
        if not tasks:
            return "📋 *Your Tasks*\n\n_No tasks yet._"
        lines = [f"📋 *Your Tasks* — {len(tasks)} total", ""]
        for tid, t in tasks.items():
            icon = STATUS_ICON.get(t.get("status", "idle"), "⚪")
            status = t.get("status", "unknown")
            base_min = t.get("interval_min", "?")
            uid = t.get("user_id") or extract_c_user(t.get("cookie", "")) or "?"
            if status == "running" and t.get("next_run_at"):
                try:
                    nxt = datetime.fromisoformat(t["next_run_at"])
                    rem = int((nxt - datetime.now()).total_seconds())
                    disp = (f"{base_min} min (next in {humanize_remaining(rem)})"
                            if rem > 0 else f"{base_min} min (starting...)")
                except Exception:
                    disp = f"{base_min} min"
            else:
                disp = f"{base_min} min"
            lines.append(f"{icon} *{t.get('name', 'Task')}*")
            lines.append(f"   • User: {uid}")
            lines.append(f"   • Progress: {t.get('created', 0)} / {t.get('target', '?')}")
            lines.append(f"   • Interval: {disp}")
            lines.append(f"   • Status: {status}")
            lines.append("")
        return truncate_msg("\n".join(lines))
    except Exception as e:
        return f"📋 *Your Tasks*\n\n_Error: {sanitize_error(e)}_"


def task_detail_text(t):
    try:
        icon = STATUS_ICON.get(t.get("status", "idle"), "⚪")
        uid = t.get("user_id") or extract_c_user(t.get("cookie", "")) or "?"
        lines = [
            f"{icon} *{t.get('name', 'Unnamed')}*",
            "",
            f"👤 User: `{uid}`",
            f"📊 Progress: `{t.get('created', 0)} / {t.get('target', '?')}`",
            f"⏱️ Interval: `{t.get('interval_min', '?')} min`",
            f"📌 Status: `{t.get('status', 'unknown')}`",
        ]
        if t.get("status") == "running" and t.get("next_run_at"):
            try:
                nxt = datetime.fromisoformat(t["next_run_at"])
                rem = int((nxt - datetime.now()).total_seconds())
                lines.append(f"⏳ Next page in: `{humanize_remaining(rem)}`" if rem > 0
                             else "⏳ Next page: `starting...`")
            except Exception:
                pass
        if t.get("current_step") and t.get("status") == "running":
            lines.append(f"🔧 Current step: _{sanitize_error(t['current_step'])}_")
        if t.get("error"):
            lines.append("")
            lines.append("❌ *Last Error:*")
            lines.append(f"`{sanitize_error(t['error'])}`")
        history = t.get("history") or []
        if history:
            lines.append("")
            shown = history[-5:]
            lines.append(f"📜 *Last {len(shown)} pages:*")
            for h in shown:
                lines.append(f"• {sanitize_error(h.get('name', '?'))}")
        return truncate_msg("\n".join(lines))
    except Exception as e:
        return f"⚠️ Render error: {sanitize_error(e)}"


# ============================================================
#  HANDLERS
# ============================================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return
    await update.message.reply_text(
        "👋 Welcome to *FB Page Creator Bot*\n\n"
        "Tap *📋 Task* below to manage your tasks.",
        reply_markup=main_menu_kb(), parse_mode="Markdown")


async def show_task_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return
    tasks = load_tasks()
    await update.message.reply_text(
        task_list_text(tasks), reply_markup=task_list_kb(tasks), parse_mode="Markdown")


async def cb_show_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    try:
        tid = q.data.split(":", 1)[1]
    except Exception:
        await safe_edit(q, "⚠️ Malformed callback.")
        return
    t = get_task(tid)
    if not t:
        await safe_edit(q, "⚠️ *Task not found.*",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_tasks")]]),
                        parse_mode="Markdown")
        return
    await safe_edit(q, task_detail_text(t),
                    reply_markup=task_detail_kb(tid, t.get("status", "idle")),
                    parse_mode="Markdown")


async def cb_back_to_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    tasks = load_tasks()
    await safe_edit(q, task_list_text(tasks), reply_markup=task_list_kb(tasks),
                    parse_mode="Markdown")


async def cb_refresh_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cb_back_to_tasks(update, ctx)


async def cb_start_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    tid = q.data.split(":", 1)[1]
    t = get_task(tid)
    if not t:
        await safe_edit(q, "⚠️ Task not found.")
        return
    if tid in running_tasks and not running_tasks[tid].done():
        await safe_edit(q, task_detail_text(t),
                        reply_markup=task_detail_kb(tid, t.get("status", "idle")),
                        parse_mode="Markdown")
        return
    update_task(tid, status="idle", error=None, current_step="Starting...")
    running_tasks[tid] = asyncio.create_task(run_task(ctx.bot, tid))
    tasks = load_tasks()
    await safe_edit(q, task_list_text(tasks), reply_markup=task_list_kb(tasks),
                    parse_mode="Markdown")


async def cb_pause_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    tid = q.data.split(":", 1)[1]
    update_task(tid, status="paused")
    t = get_task(tid)
    if t:
        await safe_edit(q, task_detail_text(t),
                        reply_markup=task_detail_kb(tid, t.get("status", "paused")),
                        parse_mode="Markdown")


async def cb_delete_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    tid = q.data.split(":", 1)[1]
    if tid in running_tasks:
        try:
            running_tasks[tid].cancel()
        except Exception:
            pass
        running_tasks.pop(tid, None)
    delete_task(tid)
    tasks = load_tasks()
    await safe_edit(q, task_list_text(tasks), reply_markup=task_list_kb(tasks),
                    parse_mode="Markdown")


# ============================================================
#  CONVERSATIONS
# ============================================================
async def conv_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        if not cb_authorized(update):
            await deny_callback(update)
            return ConversationHandler.END
        q = update.callback_query
        try:
            await q.answer()
        except Exception:
            pass
        target_msg = q.message
    else:
        if not authorized(update):
            await deny_message(update)
            return ConversationHandler.END
        target_msg = update.message
    try:
        await target_msg.reply_text(
            "📝 *Step 1/3 — Cookie*\n\nSend your Facebook cookie:\n"
            "`c_user=...; xs=...; datr=...`\n\n/cancel to abort.",
            parse_mode="Markdown")
    except Exception as e:
        logger.error(f"conv_entry: {e}")
    return ASK_COOKIE


async def conv_got_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    text = update.message.text.strip()
    if "c_user=" not in text or "xs=" not in text:
        await update.message.reply_text(
            "❌ Invalid. Must contain `c_user=` and `xs=`.", parse_mode="Markdown")
        return ASK_COOKIE
    ctx.user_data["cookie"] = text
    await update.message.reply_text(
        "📝 *Step 2/3 — Pages*\n\nHow many pages? (1-100)", parse_mode="Markdown")
    return ASK_COUNT


async def conv_got_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a number 1-100.")
        return ASK_COUNT
    ctx.user_data["count"] = n
    await update.message.reply_text(
        "📝 *Step 3/3 — Interval*\n\nHow long between pages?\n"
        "Examples: `10m`, `15m`, `1h`, or `10`.\n\n"
        "⚠️ Minimum 5m recommended.", parse_mode="Markdown")
    return ASK_INTERVAL


def parse_interval(raw):
    raw = raw.strip().lower()
    if raw.endswith("h"):
        return int(float(raw[:-1]) * 60)
    if raw.endswith("m"):
        return int(float(raw[:-1]))
    return int(float(raw))


async def conv_got_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    try:
        mins = parse_interval(update.message.text)
        if mins < 1 or mins > 24 * 60:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid. Use `10m`, `1h`, or `10`.", parse_mode="Markdown")
        return ASK_INTERVAL

    cookie = ctx.user_data["cookie"]
    count = ctx.user_data["count"]
    chat_id = update.effective_chat.id
    user_id = extract_c_user(cookie) or "unknown"

    tasks = load_tasks()
    tid = uuid.uuid4().hex[:8]
    name = f"Task {len(tasks) + 1}"
    tasks[tid] = {
        "id": tid, "name": name, "cookie": cookie,
        "user_id": user_id,
        "target": count, "interval_min": mins,
        "created": 0, "status": "idle", "history": [],
        "created_at": datetime.now().isoformat(),
        "chat_id": chat_id, "current_step": "Idle",
        "error": None, "next_run_at": None,
        "tg_user_id": update.effective_user.id,
    }
    save_tasks(tasks)

    await update.message.reply_text(
        f"✅ Task *{name}* created!\n"
        f"👤 User: `{user_id}`\n"
        f"🎯 Target: {count} pages\n⏱️ Interval: {mins} min\n\n▶️ Starting now...",
        parse_mode="Markdown")
    running_tasks[tid] = asyncio.create_task(run_task(ctx.bot, tid))

    tasks = load_tasks()
    await update.message.reply_text(
        task_list_text(tasks), reply_markup=task_list_kb(tasks), parse_mode="Markdown")
    return ConversationHandler.END


async def edit_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return ConversationHandler.END
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    tid = q.data.split(":", 1)[1]
    t = get_task(tid)
    if not t:
        await q.message.reply_text("⚠️ Task not found.")
        return ConversationHandler.END
    ctx.user_data["edit_tid"] = tid
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱️ Edit Interval", callback_data="edit_field:interval")],
        [InlineKeyboardButton("🎯 Edit Target", callback_data="edit_field:target")],
        [InlineKeyboardButton("❌ Cancel", callback_data="edit_field:cancel")],
    ])
    await q.message.reply_text(
        f"✏️ *Editing:* {t.get('name')}\n\n"
        f"• Interval: `{t.get('interval_min')} min`\n"
        f"• Target:   `{t.get('target')} pages`",
        reply_markup=kb, parse_mode="Markdown")
    return EDIT_CHOICE


async def edit_choose_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return ConversationHandler.END
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    await q.edit_message_text("⏱️ Send new interval (e.g. `10m`, `1h`).", parse_mode="Markdown")
    return EDIT_INTERVAL


async def edit_choose_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return ConversationHandler.END
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    await q.edit_message_text("🎯 Send new target (1-100).", parse_mode="Markdown")
    return EDIT_TARGET


async def edit_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cb_authorized(update):
        await deny_callback(update)
        return ConversationHandler.END
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    await q.edit_message_text("❌ Cancelled.")
    return ConversationHandler.END


async def edit_apply_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    tid = ctx.user_data.get("edit_tid")
    if not tid:
        await update.message.reply_text("⚠️ No task.")
        return ConversationHandler.END
    try:
        mins = parse_interval(update.message.text)
        if mins < 1 or mins > 24 * 60:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid. Use `10m` or `1h`.", parse_mode="Markdown")
        return EDIT_INTERVAL
    update_task(tid, interval_min=mins)
    t = get_task(tid)
    await update.message.reply_text(
        f"✅ Interval → *{mins} min* for _{t.get('name')}_.", parse_mode="Markdown")
    return ConversationHandler.END


async def edit_apply_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    tid = ctx.user_data.get("edit_tid")
    if not tid:
        await update.message.reply_text("⚠️ No task.")
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send 1-100.")
        return EDIT_TARGET
    t = get_task(tid)
    if n < t.get("created", 0):
        await update.message.reply_text(
            f"❌ Target ≥ {t.get('created', 0)} (already created).")
        return EDIT_TARGET
    update_task(tid, target=n)
    await update.message.reply_text(f"✅ Target → *{n} pages*.", parse_mode="Markdown")
    return ConversationHandler.END


async def conv_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ============================================================
#  GLOBAL ERROR HANDLER
# ============================================================
async def global_error_handler(update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, Conflict):
        logger.warning("⚠️ Conflict: another bot instance. Retry in 5s...")
        await asyncio.sleep(5)
        return
    if isinstance(err, RetryAfter):
        logger.warning(f"⚠️ Rate limited. Waiting {err.retry_after}s")
        await asyncio.sleep(err.retry_after + 1)
        return
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"⚠️ Network: {err}")
        return
    logger.error(f"Unhandled: {err}", exc_info=err)


# ============================================================
#  POST INIT
# ============================================================
async def post_init(app: Application):
    logger.info("=" * 60)
    logger.info("🚀 STARTUP DIAGNOSTICS")
    logger.info(f"   DATA_DIR   : {DATA_DIR}")
    logger.info(f"   TASKS_FILE : {TASKS_FILE}")
    logger.info(f"   USERS_FILE : {USERS_FILE}")
    logger.info(f"   HEADLESS   : {HEADLESS}")
    logger.info(f"   MAX_BROWSERS: {MAX_CONCURRENT_BROWSERS}")
    if PROXY_URL:
        logger.info(f"   PROXY      : {PROXY_URL.split('@')[-1]}")
    else:
        logger.warning("   PROXY      : ❌ NOT SET (FB may block Railway IP)")
    logger.info("=" * 60)

    # Verify Playwright
    ok, info = await check_playwright()
    if not ok:
        logger.error("❌ Playwright unavailable. Tasks will fail!")
        logger.error(f"   Error: {info}")
    logger.info("=" * 60)


# ============================================================
#  MAIN
# ============================================================
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set!")
        sys.exit(1)
    if not ALLOWED_USER_ID:
        logger.error("ALLOWED_USER_ID not set!")
        sys.exit(1)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(conv_entry, pattern="^create_task$"),
            CallbackQueryHandler(edit_entry, pattern="^edit:"),
            CommandHandler("create", conv_entry),
        ],
        states={
            ASK_COOKIE: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_got_cookie)],
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_got_count)],
            ASK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_got_interval)],
            EDIT_CHOICE: [
                CallbackQueryHandler(edit_choose_interval, pattern="^edit_field:interval$"),
                CallbackQueryHandler(edit_choose_target, pattern="^edit_field:target$"),
                CallbackQueryHandler(edit_cancel_cb, pattern="^edit_field:cancel$"),
            ],
            EDIT_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_apply_interval)],
            EDIT_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_apply_target)],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
        allow_reentry=True,
        per_message=True,
    )

    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("headless", cmd_headless))
    app.add_handler(CommandHandler("debug", cmd_debug))

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"^📋 Task$"), show_task_menu))

    app.add_handler(CallbackQueryHandler(cb_show_task, pattern="^task:"))
    app.add_handler(CallbackQueryHandler(cb_back_to_tasks, pattern="^back_to_tasks$"))
    app.add_handler(CallbackQueryHandler(cb_refresh_tasks, pattern="^refresh_tasks$"))
    app.add_handler(CallbackQueryHandler(cb_start_task, pattern="^start:"))
    app.add_handler(CallbackQueryHandler(cb_pause_task, pattern="^pause:"))
    app.add_handler(CallbackQueryHandler(cb_delete_task, pattern="^delete:"))

    app.add_error_handler(global_error_handler)

    logger.info(f"🤖 Bot running... Admin: {ALLOWED_USER_ID}")

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()