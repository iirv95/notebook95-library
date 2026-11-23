import os
import io
import re
import html
import json
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
)
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= الإعدادات الأساسية =================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
STORAGE_CHANNEL_RAW = os.environ.get("STORAGE_CHANNEL_ID", "").strip()
STORAGE_CHANNEL_ID = (
    int(STORAGE_CHANNEL_RAW) if STORAGE_CHANNEL_RAW.lstrip("-").isdigit() else None
)

FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "@Notebok95").strip()
CHANNEL_LINK = "https://t.me/Notebok95"
GROUP_LINK = "https://t.me/notebook_95"
BRAND_NAME = "Notebook | 95"

LOCAL_DB_FILE = "db.json"
PAGE_SIZE = 15

DEFAULT_SUBJECTS = [
    "الكيمياء العضوية",
    "علم العقاقير",
    "الأنسجة والتشريح",
    "الفيسيولوجي",
    "صيدلانيات",
    "صيدلة فيزيائية",
    "تحليلية",
]

DEFAULT_CATEGORIES = [
    "شيتات ومحاضرات",
    "معامل وعملي",
    "ملخصات",
    "امتحانات سابقة",
    "تسجيلات وشروحات",
]

DB: dict = {}
STORAGE_MSG_ID: int | None = None
SYNC_LOCK = asyncio.Lock()


# ================= خادم HTTP لـ Render و UptimeRobot =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Notebook | 95 Archive Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server running on port {port}")
    server.serve_forever()


# ================= إدارة قاعدة البيانات والمزامنة السحابية =================
def next_uid(prefix: str) -> str:
    DB["next_id"] = DB.get("next_id", 1) + 1
    return f"{prefix}{DB['next_id']}"


def build_default_db() -> dict:
    data = {"next_id": 1, "users": [], "subjects": {}}
    counter = 1
    for sub_name in DEFAULT_SUBJECTS:
        sid = f"s{counter}"
        counter += 1
        cats = {}
        for cat_name in DEFAULT_CATEGORIES:
            cid = f"c{counter}"
            counter += 1
            cats[cid] = {"name": cat_name, "files": []}
        data["subjects"][sid] = {"name": sub_name, "categories": cats}
    data["next_id"] = counter + 1
    return data


def count_total_files() -> int:
    total = 0
    for sub in DB.get("subjects", {}).values():
        for cat in sub.get("categories", {}).values():
            total += len(cat.get("files", []))
    return total


def write_local_db():
    with open(LOCAL_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)


async def sync_db_to_cloud(bot):
    global STORAGE_MSG_ID
    write_local_db()
    if not STORAGE_CHANNEL_ID:
        return

    async with SYNC_LOCK:
        raw_bytes = json.dumps(DB, ensure_ascii=False, indent=2).encode("utf-8")
        caption = (
            f"📦 نسخة احتياطية تلقائية | {BRAND_NAME}\n"
            f"👥 المستخدمون: {len(DB.get('users', []))}\n"
            f"📚 المواد: {len(DB.get('subjects', {}))}\n"
            f"📁 الملفات: {count_total_files()}"
        )
        bio = io.BytesIO(raw_bytes)
        bio.name = "notebook95_db.json"

        if STORAGE_MSG_ID:
            try:
                await bot.edit_message_media(
                    chat_id=STORAGE_CHANNEL_ID,
                    message_id=STORAGE_MSG_ID,
                    media=InputMediaDocument(
                        media=bio,
                        filename="notebook95_db.json",
                        caption=caption,
                    ),
                )
                return
            except BadRequest as e:
                if "message is not modified" in str(e).lower():
                    return
                logger.warning(f"تعذر تحديث النسخة المثبتة، سيتم إنشاء نسخة جديدة: {e}")
            except TelegramError as e:
                logger.warning(f"خطأ في المزامنة: {e}")

        try:
            bio.seek(0)
            msg = await bot.send_document(
                chat_id=STORAGE_CHANNEL_ID,
                document=bio,
                filename="notebook95_db.json",
                caption=caption,
            )
            STORAGE_MSG_ID = msg.message_id
            await bot.pin_chat_message(
                chat_id=STORAGE_CHANNEL_ID,
                message_id=STORAGE_MSG_ID,
                disable_notification=True,
            )
        except TelegramError as e:
            logger.error(f"فشل رفع النسخة الاحتياطية لقناة التخزين: {e}")


async def load_db_on_startup(app: Application):
    global DB, STORAGE_MSG_ID

    if STORAGE_CHANNEL_ID:
        try:
            chat = await app.bot.get_chat(STORAGE_CHANNEL_ID)
            pinned = chat.pinned_message
            if pinned and pinned.document:
                tg_file = await pinned.document.get_file()
                file_bytes = await tg_file.download_as_bytearray()
                DB = json.loads(file_bytes.decode("utf-8"))
                STORAGE_MSG_ID = pinned.message_id
                write_local_db()
                logger.info("تمت استعادة قاعدة البيانات من قناة التخزين بنجاح.")
                return
        except Exception as e:
            logger.warning(f"تعذرت الاستعادة من القناة (سيتم التهيئة): {e}")

    if os.path.exists(LOCAL_DB_FILE):
        with open(LOCAL_DB_FILE, "r", encoding="utf-8") as f:
            DB = json.load(f)
    else:
        DB = build_default_db()

    await sync_db_to_cloud(app.bot)


# ================= دوال مساعدة =================
def esc(text: str) -> str:
    return html.escape(str(text))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[إأآا]", "ا", text)
    text = text.replace("ة", "ه").replace("ى", "ي")
    return text


async def register_user(user_id: int, bot):
    if user_id not in DB.get("users", []):
        DB.setdefault("users", []).append(user_id)
        await sync_db_to_cloud(bot)


async def is_subscribed(user_id: int, bot) -> bool:
    if is_admin(user_id) or not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(chat_id=FORCE_SUB_CHANNEL, user_id=user_id)
        if member.status in {"member", "administrator", "creator"}:
            return True
        if member.status == "restricted" and getattr(member, "is_member", False):
            return True
        return False
    except TelegramError as e:
        logger.warning(f"تأكد أن البوت مشرف في قناة الاشتراك الإجباري {FORCE_SUB_CHANNEL}: {e}")
        return True


async def safe_edit(query, text: str, reply_markup=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(
            text=text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Edit error: {e}")


def force_sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 الاشتراك في قناة Notebook | 95", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ تحققت من الاشتراك", callback_data="check_sub")],
    ])


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = []
    subjects = list(DB.get("subjects", {}).items())
    for i in range(0, len(subjects), 2):
        row = [
            InlineKeyboardButton(f"📚 {sub['name']}", callback_data=f"sub:{sid}")
            for sid, sub in subjects[i:i + 2]
        ]
        buttons.append(row)

    buttons.append([InlineKeyboardButton("🔍 بحث سريع عن ملف", callback_data="search")])
    buttons.append([
        InlineKeyboardButton("📢 القناة", url=CHANNEL_LINK),
        InlineKeyboardButton("💬 المجموعة", url=GROUP_LINK),
    ])
    if is_admin(user_id):
        buttons.append([InlineKeyboardButton("⚙️ لوحة تحكم الأدمن", callback_data="adm:home")])
    return InlineKeyboardMarkup(buttons)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 رفع ملفات", callback_data="adm:up"),
            InlineKeyboardButton("🗑 حذف ملف", callback_data="adm:del"),
        ],
        [
            InlineKeyboardButton("➕ إضافة مادة", callback_data="adm:add_sub"),
            InlineKeyboardButton("➖ حذف مادة", callback_data="adm:del_sub"),
        ],
        [
            InlineKeyboardButton("➕ إضافة قسم فرعي", callback_data="adm:add_cat"),
            InlineKeyboardButton("➖ حذف قسم فرعي", callback_data="adm:del_cat"),
        ],
        [
            InlineKeyboardButton("📣 إذاعة للجميع", callback_data="adm:bc"),
            InlineKeyboardButton("📊 الإحصائيات", callback_data="adm:stats"),
        ],
        [InlineKeyboardButton("💾 تحميل نسخة احتياطية", callback_data="adm:backup")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")],
    ])


def welcome_text() -> str:
    return (
        f"أهلاً بك في أرشيف <b>{esc(BRAND_NAME)}</b> 📚\n\n"
        "اختر المادة المطلوبة من الأسفل للوصول السريع للشيتات، المعامل، الملخصات والامتحانات، "
        "أو استخدم زر <b>البحث السريع</b>."
    )


async def send_stored_file(chat_id: int, file_obj: dict, bot):
    caption = f"📄 <b>{esc(file_obj['title'])}</b>\n\n📚 {esc(BRAND_NAME)} | {esc(FORCE_SUB_CHANNEL)}"
    ftype = file_obj["file_type"]
    fid = file_obj["file_id"]

    if ftype == "document":
        await bot.send_document(chat_id=chat_id, document=fid, caption=caption, parse_mode="HTML")
    elif ftype == "photo":
        await bot.send_photo(chat_id=chat_id, photo=fid, caption=caption, parse_mode="HTML")
    elif ftype == "audio":
        await bot.send_audio(chat_id=chat_id, audio=fid, caption=caption, parse_mode="HTML")
    elif ftype == "voice":
        await bot.send_voice(chat_id=chat_id, voice=fid, caption=caption, parse_mode="HTML")
    elif ftype == "video":
        await bot.send_video(chat_id=chat_id, video=fid, caption=caption, parse_mode="HTML")


# ================= أوامر البوت =================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user = update.effective_user
    await register_user(user.id, context.bot)

    if not await is_subscribed(user.id, context.bot):
        await update.message.reply_text(
            f"⚠️ عذراً، يجب الاشتراك في قناة <b>{esc(BRAND_NAME)}</b> أولاً لاستخدام الأرشيف:",
            reply_markup=force_sub_keyboard(),
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        welcome_text(),
        reply_markup=main_menu_keyboard(user.id),
        parse_mode="HTML",
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    await update.message.reply_text(
        f"⚙️ <b>لوحة تحكم {esc(BRAND_NAME)}</b>\nاختر الإجراء المطلوب:",
        reply_markup=admin_menu_keyboard(),
        parse_mode="HTML",
    )


# ================= معالج الأزرار =================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "check_sub":
        if await is_subscribed(user_id, context.bot):
            await query.answer("✅ تم التحقق من الاشتراك!")
            context.user_data.clear()
            await safe_edit(query, welcome_text(), main_menu_keyboard(user_id))
        else:
            await query.answer("❌ لم يتم رصد اشتراكك بعد، اشترك ثم حاول مجدداً.", show_alert=True)
        return

    if not await is_subscribed(user_id, context.bot):
        await query.answer()
        await safe_edit(
            query,
            f"⚠️ يجب الاشتراك في قناة <b>{esc(BRAND_NAME)}</b> أولاً للاستمرار:",
            force_sub_keyboard(),
        )
        return

    # --- تنقل المستخدم العادي ---
    if data == "home":
        await query.answer()
        context.user_data.clear()
        await safe_edit(query, welcome_text(), main_menu_keyboard(user_id))
        return

    if data == "search":
        await query.answer()
        context.user_data["state"] = "SEARCH"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="home")]])
        await safe_edit(query, "🔍 أرسل الآن اسم الملف أو أي كلمة مفتاحية للبحث عنها:", kb)
        return

    if data.startswith("sub:"):
        await query.answer()
        sid = data.split(":")[1]
        sub = DB["subjects"].get(sid)
        if not sub:
            await safe_edit(query, "⚠️ هذه المادة غير موجودة.", main_menu_keyboard(user_id))
            return
        rows = []
        cats = list(sub["categories"].items())
        for i in range(0, len(cats), 2):
            row = [
                InlineKeyboardButton(
                    f"📂 {c['name']} ({len(c['files'])})",
                    callback_data=f"cat:{sid}:{cid}:0",
                )
                for cid, c in cats[i:i + 2]
            ]
            rows.append(row)
        rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="home")])
        await safe_edit(
            query, f"📚 <b>{esc(sub['name'])}</b>\nاختر القسم الفرعي:", InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("cat:"):
        await query.answer()
        _, sid, cid, page_str = data.split(":")
        page = int(page_str)
        sub = DB["subjects"].get(sid)
        cat = sub["categories"].get(cid) if sub else None
        if not cat:
            await safe_edit(query, "⚠️ القسم غير موجود.", main_menu_keyboard(user_id))
            return

        files_list = cat["files"]
        start_idx = page * PAGE_SIZE
        chunk = files_list[start_idx:start_idx + PAGE_SIZE]

        rows = [
            [InlineKeyboardButton(f"📄 {f['title']}", callback_data=f"file:{sid}:{cid}:{f['id']}")]
            for f in chunk
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("➡️ السابق", callback_data=f"cat:{sid}:{cid}:{page - 1}"))
        if start_idx + PAGE_SIZE < len(files_list):
            nav.append(InlineKeyboardButton("التالي ⬅️", callback_data=f"cat:{sid}:{cid}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([
            InlineKeyboardButton("🔙 رجوع للمادة", callback_data=f"sub:{sid}"),
            InlineKeyboardButton("🏠 الرئيسية", callback_data="home"),
        ])

        msg_text = f"📚 <b>{esc(sub['name'])}</b> ← 📂 <b>{esc(cat['name'])}</b>\n"
        msg_text += "اضغط على أي ملف لتحميله فوراً:" if files_list else "لا توجد ملفات في هذا القسم حالياً."
        await safe_edit(query, msg_text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("file:"):
        _, sid, cid, fid = data.split(":")
        sub = DB["subjects"].get(sid)
        cat = sub["categories"].get(cid) if sub else None
        target = next((f for f in (cat["files"] if cat else []) if f["id"] == fid), None)
        if not target:
            await query.answer("⚠️ الملف غير متاح.", show_alert=True)
            return
        await query.answer("⏳ جاري إرسال الملف...")
        await send_stored_file(query.message.chat_id, target, context.bot)
        return

    # --- لوحة تحكم الأدمن ---
    if not data.startswith("adm:"):
        await query.answer()
        return
    if not is_admin(user_id):
        await query.answer("⛔ هذا القسم خاص بالمشرفين فقط.", show_alert=True)
        return

    if data == "adm:home":
        await query.answer()
        context.user_data.clear()
        await safe_edit(query, f"⚙️ <b>لوحة تحكم {esc(BRAND_NAME)}</b>", admin_menu_keyboard())
        return

    if data == "adm:stats":
        await query.answer()
        sub_count = len(DB.get("subjects", {}))
        cat_count = sum(len(s["categories"]) for s in DB.get("subjects", {}).values())
        file_count = count_total_files()
        user_count = len(DB.get("users", []))
        cloud_status = "✅ مفعّلة" if STORAGE_CHANNEL_ID else "⚠️ غير مضبوطة"
        text = (
            f"📊 <b>إحصائيات {esc(BRAND_NAME)}</b>\n\n"
            f"👥 المستخدمون: <b>{user_count}</b>\n"
            f"📚 المواد: <b>{sub_count}</b>\n"
            f"📂 الأقسام الفرعية: <b>{cat_count}</b>\n"
            f"📁 إجمالي الملفات: <b>{file_count}</b>\n"
            f"☁️ المزامنة السحابية: <b>{cloud_status}</b>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لوحة الأدمن", callback_data="adm:home")]])
        await safe_edit(query, text, kb)
        return

    if data == "adm:backup":
        await query.answer("💾 جاري تجهيز النسخة...")
        raw_bytes = json.dumps(DB, ensure_ascii=False, indent=2).encode("utf-8")
        bio = io.BytesIO(raw_bytes)
        bio.name = "notebook95_backup.json"
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=bio,
            caption=f"💾 نسخة احتياطية فورية | {BRAND_NAME}",
        )
        return

    if data == "adm:bc":
        await query.answer()
        context.user_data["state"] = "ADMIN_BROADCAST"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="adm:home")]])
        await safe_edit(query, "📣 أرسل الآن الرسالة المطلوب إذاعتها (نص، صورة، ملف، أو توجيه):", kb)
        return

    if data == "adm:add_sub":
        await query.answer()
        context.user_data["state"] = "ADMIN_ADD_SUB"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="adm:home")]])
        await safe_edit(query, "➕ أرسل اسم المادة الجديدة:", kb)
        return

    if data == "adm:del_sub":
        await query.answer()
        rows = [
            [InlineKeyboardButton(f"🗑 {s['name']}", callback_data=f"adm:cf_sub:{sid}")]
            for sid, s in DB["subjects"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:home")])
        await safe_edit(query, "⚠️ اختر المادة لحذفها:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm:cf_sub:"):
        await query.answer()
        sid = data.split(":")[2]
        sub = DB["subjects"].get(sid)
        if not sub:
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، احذف المادة نهائياً", callback_data=f"adm:rm_sub:{sid}")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="adm:del_sub")],
        ])
        await safe_edit(
            query, f"⚠️ هل أنت متأكد من حذف مادة <b>{esc(sub['name'])}</b> وجميع ملفاتها؟", kb
        )
        return

    if data.startswith("adm:rm_sub:"):
        await query.answer()
        sid = data.split(":")[2]
        removed = DB["subjects"].pop(sid, None)
        if removed:
            await sync_db_to_cloud(context.bot)
            await safe_edit(
                query, f"✅ تم حذف مادة <b>{esc(removed['name'])}</b>.", admin_menu_keyboard()
            )
        return

    if data == "adm:add_cat":
        await query.answer()
        rows = [
            [InlineKeyboardButton(f"📚 {s['name']}", callback_data=f"adm:ac_s:{sid}")]
            for sid, s in DB["subjects"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:home")])
        await safe_edit(query, "اختر المادة لإضافة قسم فرعي داخلها:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm:ac_s:"):
        await query.answer()
        sid = data.split(":")[2]
        if sid not in DB["subjects"]:
            return
        context.user_data["state"] = "ADMIN_ADD_CAT"
        context.user_data["target_sub"] = sid
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="adm:home")]])
        await safe_edit(
            query,
            f"➕ أرسل اسم القسم الفرعي الجديد داخل <b>{esc(DB['subjects'][sid]['name'])}</b>:",
            kb,
        )
        return

    if data == "adm:del_cat":
        await query.answer()
        rows = [
            [InlineKeyboardButton(f"📚 {s['name']}", callback_data=f"adm:dc_s:{sid}")]
            for sid, s in DB["subjects"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:home")])
        await safe_edit(query, "اختر المادة لحذف قسم فرعي منها:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm:dc_s:"):
        await query.answer()
        sid = data.split(":")[2]
        sub = DB["subjects"].get(sid)
        if not sub:
            return
        rows = [
            [InlineKeyboardButton(f"🗑 {c['name']}", callback_data=f"adm:rm_cat:{sid}:{cid}")]
            for cid, c in sub["categories"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:del_cat")])
        await safe_edit(
            query, f"اختر القسم الفرعي لحذفه من <b>{esc(sub['name'])}</b>:", InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("adm:rm_cat:"):
        await query.answer()
        _, _, sid, cid = data.split(":")
        sub = DB["subjects"].get(sid)
        removed = sub["categories"].pop(cid, None) if sub else None
        if removed:
            await sync_db_to_cloud(context.bot)
            await safe_edit(
                query, f"✅ تم حذف القسم <b>{esc(removed['name'])}</b>.", admin_menu_keyboard()
            )
        return

    # --- رفع الملفات ---
    if data == "adm:up":
        await query.answer()
        rows = [
            [InlineKeyboardButton(f"📚 {s['name']}", callback_data=f"adm:up_s:{sid}")]
            for sid, s in DB["subjects"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:home")])
        await safe_edit(query, "📤 اختر المادة لرفع الملفات إليها:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm:up_s:"):
        await query.answer()
        sid = data.split(":")[2]
        sub = DB["subjects"].get(sid)
        if not sub:
            return
        rows = [
            [InlineKeyboardButton(f"📂 {c['name']}", callback_data=f"adm:up_c:{sid}:{cid}")]
            for cid, c in sub["categories"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:up")])
        await safe_edit(
            query, f"📤 اختر القسم الفرعي داخل <b>{esc(sub['name'])}</b>:", InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("adm:up_c:"):
        await query.answer()
        _, _, sid, cid = data.split(":")
        sub = DB["subjects"].get(sid)
        if not sub or cid not in sub["categories"]:
            return
        context.user_data["state"] = "ADMIN_UPLOAD"
        context.user_data["target_sub"] = sid
        context.user_data["target_cat"] = cid
        sub_name = esc(sub["name"])
        cat_name = esc(sub["categories"][cid]["name"])
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ إنهاء الرفع", callback_data="adm:home")]])
        await safe_edit(
            query,
            f"📤 جاهز للاستقبال في:\n<b>{sub_name} ← {cat_name}</b>\n\n"
            "أرسل الآن ملفاً أو عدة ملفات متتالية (PDF، صور، صوتيات، فيديو).\n"
            "💡 <i>إذا كتبت وصفاً (Caption) مع الملف سيُعتمد كاسم للملف، وإلا سيؤخذ اسم الملف الأصلي.</i>",
            kb,
        )
        return

    # --- حذف الملفات ---
    if data == "adm:del":
        await query.answer()
        rows = [
            [InlineKeyboardButton(f"📚 {s['name']}", callback_data=f"adm:del_s:{sid}")]
            for sid, s in DB["subjects"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:home")])
        await safe_edit(query, "🗑 اختر المادة لحذف ملف منها:", InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm:del_s:"):
        await query.answer()
        sid = data.split(":")[2]
        sub = DB["subjects"].get(sid)
        if not sub:
            return
        rows = [
            [
                InlineKeyboardButton(
                    f"📂 {c['name']} ({len(c['files'])})",
                    callback_data=f"adm:del_c:{sid}:{cid}:0",
                )
            ]
            for cid, c in sub["categories"].items()
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm:del")])
        await safe_edit(
            query, f"🗑 اختر القسم داخل <b>{esc(sub['name'])}</b>:", InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("adm:del_c:"):
        await query.answer()
        _, _, sid, cid, page_str = data.split(":")
        page = int(page_str)
        sub = DB["subjects"].get(sid)
        cat = sub["categories"].get(cid) if sub else None
        if not cat:
            return
        files_list = cat["files"]
        start_idx = page * PAGE_SIZE
        chunk = files_list[start_idx:start_idx + PAGE_SIZE]
        rows = [
            [InlineKeyboardButton(f"❌ {f['title']}", callback_data=f"adm:del_f:{sid}:{cid}:{f['id']}")]
            for f in chunk
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("➡️ السابق", callback_data=f"adm:del_c:{sid}:{cid}:{page - 1}"))
        if start_idx + PAGE_SIZE < len(files_list):
            nav.append(InlineKeyboardButton("التالي ⬅️", callback_data=f"adm:del_c:{sid}:{cid}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"adm:del_s:{sid}")])
        text = "اضغط على الملف لحذفه فوراً:" if files_list else "القسم فارغ."
        await safe_edit(query, text, InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm:del_f:"):
        _, _, sid, cid, fid = data.split(":")
        sub = DB["subjects"].get(sid)
        cat = sub["categories"].get(cid) if sub else None
        if not cat:
            await query.answer("⚠️ القسم غير موجود.", show_alert=True)
            return
        cat["files"] = [f for f in cat["files"] if f["id"] != fid]
        await sync_db_to_cloud(context.bot)
        await query.answer("✅ تم حذف الملف.", show_alert=True)
        rows = [
            [InlineKeyboardButton(f"❌ {f['title']}", callback_data=f"adm:del_f:{sid}:{cid}:{f['id']}")]
            for f in cat["files"][:PAGE_SIZE]
        ]
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"adm:del_s:{sid}")])
        await safe_edit(query, "تم التحديث. اختر ملفاً آخر للحذف أو ارجع:", InlineKeyboardMarkup(rows))
        return

    await query.answer()


# ================= معالج الرسائل (بحث + عمليات الأدمن) =================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if not state:
        return

    user_id = update.effective_user.id
    msg = update.message

    # --- البحث السريع ---
    if state == "SEARCH":
        if not msg.text:
            return
        query_norm = normalize_text(msg.text)
        matches = []
        for sid, sub in DB.get("subjects", {}).items():
            for cid, cat in sub.get("categories", {}).items():
                for f in cat.get("files", []):
                    haystack = normalize_text(f"{f['title']} {sub['name']} {cat['name']}")
                    if query_norm in haystack:
                        matches.append((sid, cid, f, sub["name"]))

        if not matches:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 بحث جديد", callback_data="search")],
                [InlineKeyboardButton("🏠 الرئيسية", callback_data="home")],
            ])
            await msg.reply_text("❌ لم يتم العثور على ملفات مطابقة، جرب كلمة أخرى.", reply_markup=kb)
            return

        rows = [
            [InlineKeyboardButton(f"📄 {f['title']} ({sub_name})", callback_data=f"file:{sid}:{cid}:{f['id']}")]
            for sid, cid, f, sub_name in matches[:25]
        ]
        rows.append([InlineKeyboardButton("🏠 الرئيسية", callback_data="home")])
        context.user_data.clear()
        await msg.reply_text(
            f"🔍 تم العثور على <b>{len(matches)}</b> نتيجة:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="HTML",
        )
        return

    # --- عمليات الأدمن ---
    if not is_admin(user_id):
        return

    if state == "ADMIN_ADD_SUB" and msg.text:
        name = msg.text.strip()
        sid = next_uid("s")
        cats = {next_uid("c"): {"name": cat_name, "files": []} for cat_name in DEFAULT_CATEGORIES}
        DB["subjects"][sid] = {"name": name, "categories": cats}
        context.user_data.clear()
        await sync_db_to_cloud(context.bot)
        await msg.reply_text(
            f"✅ تمت إضافة مادة <b>{esc(name)}</b> مع الأقسام الافتراضية.",
            reply_markup=admin_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    if state == "ADMIN_ADD_CAT" and msg.text:
        sid = context.user_data.get("target_sub")
        name = msg.text.strip()
        if sid in DB["subjects"]:
            cid = next_uid("c")
            DB["subjects"][sid]["categories"][cid] = {"name": name, "files": []}
            await sync_db_to_cloud(context.bot)
            await msg.reply_text(
                f"✅ تمت إضافة القسم <b>{esc(name)}</b> بنجاح.",
                reply_markup=admin_menu_keyboard(),
                parse_mode="HTML",
            )
        context.user_data.clear()
        return

    if state == "ADMIN_UPLOAD":
        sid = context.user_data.get("target_sub")
        cid = context.user_data.get("target_cat")
        ftype, fid, default_title = None, None, None

        if msg.document:
            ftype = "document"
            fid = msg.document.file_id
            default_title = msg.document.file_name or "ملف مستند"
        elif msg.photo:
            ftype = "photo"
            fid = msg.photo[-1].file_id
            default_title = "صورة"
        elif msg.audio:
            ftype = "audio"
            fid = msg.audio.file_id
            default_title = msg.audio.title or msg.audio.file_name or "تسجيل صوتي"
        elif msg.voice:
            ftype = "voice"
            fid = msg.voice.file_id
            default_title = "رسالة صوتية"
        elif msg.video:
            ftype = "video"
            fid = msg.video.file_id
            default_title = msg.video.file_name or "فيديو شرح"

        if not ftype:
            await msg.reply_text("⚠️ أرسل ملفاً (PDF، صورة، صوت، فيديو) أو اضغط إنهاء الرفع.")
            return

        title = (msg.caption.strip() if msg.caption else default_title)[:80]
        file_rec = {
            "id": next_uid("f"),
            "title": title,
            "file_id": fid,
            "file_type": ftype,
        }
        DB["subjects"][sid]["categories"][cid]["files"].append(file_rec)
        await sync_db_to_cloud(context.bot)

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ إنهاء الرفع", callback_data="adm:home")]])
        await msg.reply_text(
            f"✅ تم حفظ: <b>{esc(title)}</b>\nأرسل الملف التالي أو اضغط إنهاء.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    if state == "ADMIN_BROADCAST":
        context.user_data.clear()
        users = list(DB.get("users", []))
        status_msg = await msg.reply_text(f"⏳ جاري الإرسال إلى {len(users)} مستخدم...")
        sent, failed = 0, 0
        for uid in users:
            try:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                )
                sent += 1
            except TelegramError:
                failed += 1
            await asyncio.sleep(0.05)

        await status_msg.edit_text(
            f"✅ <b>اكتملت الإذاعة</b>\n\n📬 تم الإرسال: <b>{sent}</b>\n❌ فشل/محظور: <b>{failed}</b>",
            reply_markup=admin_menu_keyboard(),
            parse_mode="HTML",
        )


def main():
    if not BOT_TOKEN:
        raise ValueError("متغير BOT_TOKEN غير مضبوط!")

    threading.Thread(target=start_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(load_db_on_startup).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    logger.info("Bot started successfully.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
