import os
import re
import json
import uuid
import random
import asyncio
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
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))   # ← admin

# ============================================================
#  TOGGLE: True = silent (production)
# ============================================================
HEADLESS = True

TASKS_FILE = "tasks.json"
USERS_FILE = "users.json"

running_tasks = {}
active_message = {}
auto_refresh_task = {}

MAX_MSG = 3800
MAX_ERR = 300
REFRESH_INTERVAL = 8
REFRESH_MAX_AGE = 30 * 60

POST_CREATE_WAIT_MS = 15000

(ASK_COOKIE, ASK_COUNT, ASK_INTERVAL,
 EDIT_CHOICE, EDIT_INTERVAL, EDIT_TARGET) = range(6)


# ============================================================
#  USER WHITELIST (admin access control)
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
    with open(USERS_FILE, "w") as f:
        json.dump(sorted(int(u) for u in users), f, indent=2)


def is_admin(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID


def is_authorized(user_id: int) -> bool:
    if user_id is None:
        return False
    if is_admin(user_id):
        return True
    return user_id in load_users()


def authorized(update: Update) -> bool:
    """Used by message-based handlers."""
    return bool(update.effective_user and is_authorized(update.effective_user.id))


def cb_authorized(update: Update) -> bool:
    """Used by callback-query handlers."""
    q = update.callback_query
    return bool(q and q.from_user and is_authorized(q.from_user.id))


async def deny_message(update: Update):
    """Reply to a non-authorized user politely."""
    try:
        await update.effective_message.reply_text(
            "⛔ Access denied.\n"
            "This bot is private. Contact the admin to get access."
        )
    except Exception:
        pass


async def deny_callback(update: Update):
    try:
        await update.callback_query.answer("⛔ Access denied.", show_alert=True)
    except Exception:
        pass


# ============================================================
#  SAFE HELPERS
# ============================================================
def truncate(s, n: int) -> str:
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


async def safe_edit(q, text: str, reply_markup=None, parse_mode="Markdown"):
    text = truncate_msg(text)
    try:
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ["parse", "entit", "bad request", "can't find"]):
            try:
                await q.edit_message_text(text, reply_markup=reply_markup)
                return
            except Exception:
                pass
        try:
            await q.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            pass


# ============================================================
#  ADMIN COMMANDS
# ============================================================
async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Anyone can see their own ID (needed to request access)."""
    try:
        await update.message.reply_text(
            f"🆔 Your Telegram ID: `{update.effective_user.id}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass


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
        await update.message.reply_text("ℹ️ That ID is the admin (already authorized).")
        return
    users = load_users()
    if new_id in users:
        await update.message.reply_text(f"ℹ️ `{new_id}` already authorized.", parse_mode="Markdown")
        return
    users.add(new_id)
    save_users(users)
    await update.message.reply_text(
        f"✅ Added `{new_id}` to the whitelist.", parse_mode="Markdown"
    )
    # Notify the new user (best effort)
    try:
        await ctx.bot.send_message(
            new_id,
            "✅ You've been granted access to this bot. Send /start to begin."
        )
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
        await update.message.reply_text("❌ Cannot remove the admin.")
        return
    users = load_users()
    if rid not in users:
        await update.message.reply_text(f"ℹ️ `{rid}` is not in the whitelist.", parse_mode="Markdown")
        return
    users.discard(rid)
    save_users(users)
    await update.message.reply_text(
        f"🗑️ Removed `{rid}` from the whitelist.", parse_mode="Markdown"
    )
    try:
        await ctx.bot.send_message(rid, "⛔ Your access to this bot has been revoked.")
    except Exception:
        pass


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update) or not is_admin(update.effective_user.id):
        await deny_message(update)
        return
    users = load_users()
    lines = [f"👑 *Admin:* `{ALLOWED_USER_ID}`", ""]
    if not users:
        lines.append("_No additional users._")
    else:
        lines.append(f"✅ *Authorized users ({len(users)}):*")
        for u in sorted(users):
            lines.append(f"• `{u}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ============================================================
#  AUTO-REFRESH
# ============================================================
def _build_refresh_payload(chat_id):
    info = active_message.get(chat_id)
    if not info:
        return None
    kind = info.get("kind")
    if kind == "list":
        tasks = load_tasks()
        return task_list_text(tasks), task_list_kb(tasks)
    if kind == "detail":
        t = get_task(info.get("tid"))
        if not t:
            return None
        return task_detail_text(t), task_detail_kb(info["tid"], t.get("status", "idle"))
    return None


async def _auto_refresh_worker(bot, chat_id):
    started = datetime.now()
    try:
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            if (datetime.now() - started).total_seconds() > REFRESH_MAX_AGE:
                return
            info = active_message.get(chat_id)
            if not info:
                return
            payload = _build_refresh_payload(chat_id)
            if not payload:
                return
            text, kb = payload
            edited = False
            for pm in ("Markdown", None):
                try:
                    await bot.edit_message_text(
                        truncate_msg(text),
                        chat_id=chat_id,
                        message_id=info["msg_id"],
                        reply_markup=kb,
                        parse_mode=pm,
                    )
                    edited = True
                    break
                except Exception as e:
                    if "not modified" in str(e).lower():
                        edited = True
                        break
                    continue
            if not edited:
                return
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[refresh] {e}")
    finally:
        auto_refresh_task.pop(chat_id, None)


def set_active_message(chat_id: int, msg_id: int, kind: str, tid: str = None, bot=None):
    active_message[chat_id] = {"msg_id": msg_id, "kind": kind, "tid": tid}
    if bot is None:
        return
    existing = auto_refresh_task.get(chat_id)
    if existing and not existing.done():
        existing.cancel()
    auto_refresh_task[chat_id] = asyncio.create_task(_auto_refresh_worker(bot, chat_id))


# ============================================================
#  STORAGE
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
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2)


def get_task(tid: str):
    return load_tasks().get(tid)


def update_task(tid: str, **kwargs):
    tasks = load_tasks()
    if tid in tasks:
        tasks[tid].update(kwargs)
        save_tasks(tasks)


def delete_task(tid: str):
    tasks = load_tasks()
    tasks.pop(tid, None)
    save_tasks(tasks)


# ============================================================
#  MISC
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


def humanize_remaining(seconds: int) -> str:
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
#  CATEGORY SELECTION
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
            print(f"[fill_category] attempt {attempt}: {e}")
            await page.wait_for_timeout(1000)
    return False


# ============================================================
#  CREATE ONE PAGE
# ============================================================
async def create_one_page(cookie: str, log=None):
    async def note(msg):
        if log:
            try:
                await log(msg)
            except Exception:
                pass

    page_name = random_page_name()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            slow_mo=250 if not HEADLESS else 0,
            args=["--disable-blink-features=AutomationControlled", "--disable-gpu"],
        )
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 850},
                locale="en-US",
            )
            await context.add_cookies(parse_cookies(cookie))
            page = await context.new_page()
            page.set_default_timeout(30000)

            # 1. LOGIN
            await note("🌐 Loading facebook.com...")
            await page.goto("https://www.facebook.com/",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)
            if "/login" in page.url or "checkpoint" in page.url:
                raise RuntimeError("Cookie expired or checkpoint required.")

            # 2. PAGES HOME
            await note("📄 Opening Pages...")
            await page.goto(
                "https://www.facebook.com/pages/?category=top&ref=bookmarks",
                wait_until="domcontentloaded", timeout=60000,
            )
            await page.wait_for_timeout(4000)

            # 3. CREATE PAGE (top)
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

            # 5. NEXT (option modal)
            await note("🖱️ Next (option step)...")
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
            await note("🔍 Selecting category: Entertainment...")
            cat_input = page.locator('input[aria-label="Category (required)"]').first
            await cat_input.wait_for(state="attached", timeout=20000)
            if not await fill_category(page, cat_input, "Entertainment", max_retries=3):
                raise RuntimeError("Category selection failed after 3 attempts.")

            # ============================================================
            # 9. FOOTER CREATE PAGE → WAIT 15s → RELOAD
            # ============================================================
            await note("🖱️ Clicking footer Create Page...")
            footer_btn = page.locator('[aria-label="Create Page"]').last
            for _ in range(15):
                if (await footer_btn.get_attribute("aria-disabled")) != "true":
                    break
                await page.wait_for_timeout(500)
            await footer_btn.click()

            await note("⏳ Waiting 15s for page to be created...")
            await page.wait_for_timeout(POST_CREATE_WAIT_MS)

            await note("🔄 Refreshing page...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"[reload] {e}")
            await page.wait_for_timeout(4000)

            final_url = page.url
            await note("✅ Page created.")

            return {"name": page_name, "url": final_url, "success": True}
        finally:
            await browser.close()


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
                        f"🎉 *{t.get('name')}* completed — {t.get('target')} pages created.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                return

            update_task(tid, status="running", current_step="Starting...", error=None)

            async def log(msg):
                update_task(tid, current_step=msg)

            try:
                result = await create_one_page(t["cookie"], log=log)
            except Exception as e:
                err = sanitize_error(e)
                update_task(tid, status="failed", error=err, current_step="Failed")
                try:
                    await bot.send_message(
                        chat_id,
                        f"💥 *{t.get('name')}* failed:\n`{err}`\n\n"
                        f"Tap 📋 Task → select this task to retry, edit, or delete.",
                        parse_mode="Markdown",
                    )
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

            # ---- Success message: progress + user + page name ----
            try:
                await bot.send_message(
                    chat_id,
                    f"✅ Page {t2['created']}/{t2['target']} created!\n"
                    f"✅ user : {user_id}\n"
                    f"📛 {result['name']}",
                )
            except Exception:
                pass

            if t2["created"] >= t2["target"]:
                update_task(tid, status="completed", next_run_at=None)
                try:
                    await bot.send_message(
                        chat_id,
                        f"🎉 *{t2['name']}* completed — all {t2['target']} pages done!",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                return

            # Wait interval (pause-aware)
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
        print(f"[run_task] {e}")
        if chat_id:
            try:
                await bot.send_message(chat_id, f"💥 Task runner crashed: {sanitize_error(e)}")
            except Exception:
                pass
    finally:
        running_tasks.pop(tid, None)


# ============================================================
#  KEYBOARDS / TEXT
# ============================================================
def main_menu_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("📋 Task")]], resize_keyboard=True)


def task_list_kb(tasks: dict) -> InlineKeyboardMarkup:
    rows = []
    for tid, t in tasks.items():
        icon = STATUS_ICON.get(t.get("status", "idle"), "⚪")
        rows.append([InlineKeyboardButton(
            f"{icon} {t.get('name', 'Task')} ({t.get('created', 0)}/{t.get('target', '?')})",
            callback_data=f"task:{tid}")])
    rows.append([InlineKeyboardButton("➕ Create a Task", callback_data="create_task")])
    rows.append([InlineKeyboardButton("🔄 Refresh Now", callback_data="refresh_tasks")])
    return InlineKeyboardMarkup(rows)


def task_detail_kb(tid: str, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status in ("idle", "failed", "paused"):
        rows.append([InlineKeyboardButton("▶️ Start", callback_data=f"start:{tid}")])
    if status == "running":
        rows.append([InlineKeyboardButton("⏸️ Pause", callback_data=f"pause:{tid}")])
    rows.append([InlineKeyboardButton("✏️ Edit Config", callback_data=f"edit:{tid}")])
    rows.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"delete:{tid}")])
    rows.append([InlineKeyboardButton("⬅️ Back to Tasks", callback_data="back_to_tasks")])
    return InlineKeyboardMarkup(rows)


def task_list_text(tasks: dict) -> str:
    try:
        if not tasks:
            return "📋 *Your Tasks*\n\n_No tasks yet. Tap below to create one._"
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


def task_detail_text(t: dict) -> str:
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
        return f"⚠️ Could not render task.\n\nError: {sanitize_error(e)}"


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
        reply_markup=main_menu_kb(), parse_mode="Markdown",
    )


async def show_task_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return
    tasks = load_tasks()
    msg = await update.message.reply_text(
        task_list_text(tasks), reply_markup=task_list_kb(tasks), parse_mode="Markdown",
    )
    set_active_message(update.effective_chat.id, msg.message_id, "list", bot=ctx.bot)


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
                            [[InlineKeyboardButton("⬅️ Back to Tasks",
                                                   callback_data="back_to_tasks")]]),
                        parse_mode="Markdown")
        return
    await safe_edit(q, task_detail_text(t),
                    reply_markup=task_detail_kb(tid, t.get("status", "idle")),
                    parse_mode="Markdown")
    set_active_message(q.message.chat_id, q.message.message_id, "detail", tid=tid, bot=ctx.bot)


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
    set_active_message(q.message.chat_id, q.message.message_id, "list", bot=ctx.bot)


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
        set_active_message(q.message.chat_id, q.message.message_id, "detail", tid=tid, bot=ctx.bot)
        return
    update_task(tid, status="idle", error=None, current_step="Starting...")
    running_tasks[tid] = asyncio.create_task(run_task(ctx.bot, tid))
    tasks = load_tasks()
    await safe_edit(q, task_list_text(tasks), reply_markup=task_list_kb(tasks),
                    parse_mode="Markdown")
    set_active_message(q.message.chat_id, q.message.message_id, "list", bot=ctx.bot)


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
        set_active_message(q.message.chat_id, q.message.message_id, "detail", tid=tid, bot=ctx.bot)


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
    set_active_message(q.message.chat_id, q.message.message_id, "list", bot=ctx.bot)


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
    await target_msg.reply_text(
        "📝 *Step 1/3 — Cookie*\n\nSend me your Facebook cookie string:\n"
        "`c_user=...; xs=...; datr=...`\n\nSend /cancel to abort.",
        parse_mode="Markdown")
    return ASK_COOKIE


async def conv_got_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    text = update.message.text.strip()
    if "c_user=" not in text or "xs=" not in text:
        await update.message.reply_text(
            "❌ Invalid. Must contain `c_user=` and `xs=`. Try again or /cancel.",
            parse_mode="Markdown")
        return ASK_COOKIE
    ctx.user_data["cookie"] = text
    await update.message.reply_text(
        "📝 *Step 2/3 — Pages*\n\nHow many pages do you want to create?\n"
        "Send a number between `1` and `100`.", parse_mode="Markdown")
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
        await update.message.reply_text("❌ Send a number between 1 and 100.")
        return ASK_COUNT
    ctx.user_data["count"] = n
    await update.message.reply_text(
        "📝 *Step 3/3 — Interval*\n\nHow long to wait between pages?\n"
        "Examples: `10m`, `15m`, `20m`, `1h`, or just `10`.\n\n"
        "⚠️ FB blocks fast creation. Minimum 5m recommended.",
        parse_mode="Markdown")
    return ASK_INTERVAL


def parse_interval(raw: str):
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
            "❌ Invalid. Use e.g. `10m`, `1h`, or `10`.", parse_mode="Markdown")
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
    msg = await update.message.reply_text(
        task_list_text(tasks), reply_markup=task_list_kb(tasks), parse_mode="Markdown")
    set_active_message(chat_id, msg.message_id, "list", bot=ctx.bot)
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
        [InlineKeyboardButton("🎯 Edit Target Pages", callback_data="edit_field:target")],
        [InlineKeyboardButton("❌ Cancel", callback_data="edit_field:cancel")],
    ])
    await q.message.reply_text(
        f"✏️ *Editing:* {t.get('name')}\n\nCurrent:\n"
        f"• Interval: `{t.get('interval_min')} min`\n"
        f"• Target:   `{t.get('target')} pages`\n\nWhat do you want to change?",
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
    await q.edit_message_text(
        "⏱️ Send the *new interval* (e.g. `10m`, `30m`, `1h`).\n\nSend /cancel to abort.",
        parse_mode="Markdown")
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
    await q.edit_message_text(
        "🎯 Send the *new target* (pages count, 1-100).\n\nSend /cancel to abort.",
        parse_mode="Markdown")
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
    await q.edit_message_text("❌ Edit cancelled.")
    return ConversationHandler.END


async def edit_apply_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    tid = ctx.user_data.get("edit_tid")
    if not tid:
        await update.message.reply_text("⚠️ No task selected.")
        return ConversationHandler.END
    try:
        mins = parse_interval(update.message.text)
        if mins < 1 or mins > 24 * 60:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid. Use e.g. `10m` or `1h`.", parse_mode="Markdown")
        return EDIT_INTERVAL
    update_task(tid, interval_min=mins)
    t = get_task(tid)
    await update.message.reply_text(
        f"✅ Interval updated to *{mins} min* for _{t.get('name')}_.\n"
        f"Takes effect on the next cycle.", parse_mode="Markdown")
    return ConversationHandler.END


async def edit_apply_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    tid = ctx.user_data.get("edit_tid")
    if not tid:
        await update.message.reply_text("⚠️ No task selected.")
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a number between 1 and 100.")
        return EDIT_TARGET
    t = get_task(tid)
    if n < t.get("created", 0):
        await update.message.reply_text(
            f"❌ Target must be ≥ already-created count ({t.get('created', 0)}).")
        return EDIT_TARGET
    update_task(tid, target=n)
    await update.message.reply_text(
        f"✅ Target updated to *{n} pages* for _{t.get('name')}_.",
        parse_mode="Markdown")
    return ConversationHandler.END


async def conv_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await deny_message(update)
        return ConversationHandler.END
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ============================================================
#  MAIN
# ============================================================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

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
    )

    # Admin commands (must be registered BEFORE the conversation handler
    # so that /add, /remove, /users aren't swallowed)
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("id", cmd_id))

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex(r"^📋 Task$"), show_task_menu))

    app.add_handler(CallbackQueryHandler(cb_show_task, pattern="^task:"))
    app.add_handler(CallbackQueryHandler(cb_back_to_tasks, pattern="^back_to_tasks$"))
    app.add_handler(CallbackQueryHandler(cb_refresh_tasks, pattern="^refresh_tasks$"))
    app.add_handler(CallbackQueryHandler(cb_start_task, pattern="^start:"))
    app.add_handler(CallbackQueryHandler(cb_pause_task, pattern="^pause:"))
    app.add_handler(CallbackQueryHandler(cb_delete_task, pattern="^delete:"))

    print(f"🤖 Bot running... HEADLESS={HEADLESS}")
    print(f"👑 Admin ID: {ALLOWED_USER_ID}")
    print(f"📂 Storage: {TASKS_FILE} | {USERS_FILE}")
    print(f"👥 Users: {len(load_users())} authorized")
    app.run_polling()


if __name__ == "__main__":
    main()