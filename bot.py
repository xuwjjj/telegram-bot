#!/usr/bin/env python3
"""
بوت تيليجرام لتحميل الفيديوهات والصوتيات من منصات متعددة
YouTube • Instagram • TikTok • Facebook • Twitter/X
─────────────────────────────────────────────────────
✅ بدون subprocess — يستخدم yt_dlp كمكتبة بايثون مباشرة
✅ يتضمن Health Server للاستضافة المجانية على Render
⚡ Dev: @xuwjj — Marco
"""

import os
import re
import json
import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction

# ──────────────────── الإعدادات ────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "7379445553:AAEOQo6_umHhSAd8c2ykKdV0ir3UYCAsiYc")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "1647643509").split(",")))
MAX_FILE_MB = 50
STATS_FILE = "stats.json"

executor = ThreadPoolExecutor(max_workers=4)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────── Health Server (لـ Render Free) ────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ──────────────────── أنماط الروابط (Regex) ────────────────────
PATTERNS = {
    "youtube": re.compile(
        r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]+"
    ),
    "instagram": re.compile(
        r"(https?://)?(www\.)?instagram\.com/(reel|p|tv)/[\w\-]+"
    ),
    "tiktok": re.compile(
        r"(https?://)?(www\.|vm\.)?tiktok\.com/[@\w\-./]+"
    ),
    "facebook": re.compile(
        r"(https?://)?(www\.|m\.)?(facebook\.com|fb\.watch)/[\w\-./]+"
    ),
    "twitter": re.compile(
        r"(https?://)?(www\.)?(twitter\.com|x\.com)/\w+/status/\d+"
    ),
}

# ──────────────────── الإحصائيات ────────────────────

def load_stats() -> dict:
    if Path(STATS_FILE).exists():
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "total_downloads": 0, "daily": {}}


def save_stats(data: dict):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def track(user_id: int, username: str, platform: str):
    stats = load_stats()
    uid = str(user_id)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if uid not in stats["users"]:
        stats["users"][uid] = {
            "username": username,
            "first_seen": today,
            "downloads": 0,
        }

    stats["users"][uid]["downloads"] += 1
    stats["users"][uid]["last_active"] = today
    stats["users"][uid]["username"] = username
    stats["total_downloads"] += 1
    stats["daily"][today] = stats["daily"].get(today, 0) + 1

    save_stats(stats)


# ──────────────────── أدوات المنصات ────────────────────

def detect_platform(url: str) -> str | None:
    for platform, pattern in PATTERNS.items():
        if pattern.search(url):
            return platform
    return None


def extract_url(text: str) -> str | None:
    for pattern in PATTERNS.values():
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


PLATFORM_NAMES = {
    "youtube": "🎬 يوتيوب",
    "instagram": "📸 انستغرام",
    "tiktok": "🎵 تيك توك",
    "facebook": "📘 فيسبوك",
    "twitter": "🐦 تويتر / X",
}

# ──────────────────── yt-dlp كمكتبة: جلب المعلومات ────────────────────

def _extract_info_sync(url: str) -> dict | None:
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None

            duration_sec = info.get("duration", 0) or 0
            mins, secs = divmod(int(duration_sec), 60)
            views = info.get("view_count", 0) or 0

            return {
                "title": info.get("title", "بدون عنوان"),
                "duration": f"{mins}:{secs:02d}",
                "uploader": info.get("uploader", "غير معروف"),
                "views": f"{views:,}" if views else "—",
            }
    except Exception as e:
        logger.error(f"extract_info error: {e}")
        return None


async def get_info(url: str) -> dict | None:
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, _extract_info_sync, url),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return None


# ──────────────────── yt-dlp كمكتبة: التحميل ────────────────────

def _download_sync(
    url: str,
    output_dir: str,
    mode: str = "video",
    quality: str = "best",
    audio_quality: str = "192",
) -> dict:
    output_template = os.path.join(output_dir, "%(title).60s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    if mode == "mp3":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_quality,
            }],
        })
    else:
        if quality == "best":
            ydl_opts["format"] = (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4]/best"
            )
        else:
            ydl_opts["format"] = (
                f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={quality}][ext=mp4]/best"
            )
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        files = list(Path(output_dir).glob("*"))
        if not files:
            return {"ok": False, "error": "لم يتم العثور على الملف المُحمّل"}

        file_path = max(files, key=lambda f: f.stat().st_size)
        size_mb = file_path.stat().st_size / (1024 * 1024)

        if size_mb > MAX_FILE_MB:
            return {
                "ok": False,
                "error": f"⚠️ حجم الملف ({size_mb:.1f}MB) أكبر من الحد المسموح ({MAX_FILE_MB}MB)",
            }

        return {
            "ok": True,
            "path": str(file_path),
            "size_mb": round(size_mb, 2),
            "filename": file_path.name,
        }

    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)
        logger.error(f"download error: {err_msg}")
        return {"ok": False, "error": _parse_error(err_msg)}
    except Exception as e:
        logger.error(f"download exception: {e}")
        return {"ok": False, "error": f"⚠️ خطأ: {str(e)[:200]}"}


async def download_media(
    url: str,
    output_dir: str,
    mode: str = "video",
    quality: str = "best",
    audio_quality: str = "192",
) -> dict:
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                executor,
                _download_sync,
                url, output_dir, mode, quality, audio_quality,
            ),
            timeout=300,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "⏰ انتهت المهلة — حاول مرة أخرى"}


def _parse_error(error_msg: str) -> str:
    lower = error_msg.lower()
    if "private" in lower:
        return "🔒 هذا المحتوى خاص ولا يمكن تحميله"
    if "not available" in lower or "unavailable" in lower:
        return "❌ المحتوى غير متاح أو محذوف"
    if "age" in lower:
        return "🔞 المحتوى مقيد بالعمر"
    if "copyright" in lower:
        return "©️ المحتوى محمي بحقوق النشر"
    if "geo" in lower:
        return "🌍 المحتوى محظور في منطقتك"
    if "login" in lower or "sign in" in lower:
        return "🔑 المحتوى يتطلب تسجيل دخول"
    if "ffmpeg" in lower:
        return "⚠️ يجب تثبيت ffmpeg — حمّله من ffmpeg.org وأضفه لـ PATH"
    return f"⚠️ خطأ في التحميل:\n<code>{error_msg[:200]}</code>"


# ──────────────────── لوحات المفاتيح ────────────────────

def youtube_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 MP3 (192k)", callback_data="yt_mp3_192"),
            InlineKeyboardButton("🎵 MP3 (320k)", callback_data="yt_mp3_320"),
        ],
        [
            InlineKeyboardButton("📹 فيديو 360p", callback_data="yt_vid_360"),
            InlineKeyboardButton("📹 فيديو 480p", callback_data="yt_vid_480"),
        ],
        [
            InlineKeyboardButton("📹 فيديو 720p", callback_data="yt_vid_720"),
            InlineKeyboardButton("📹 أفضل جودة", callback_data="yt_vid_best"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])


def social_keyboard(platform: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📹 فيديو (بدون علامة مائية)", callback_data=f"{platform}_vid"),
        ],
        [
            InlineKeyboardButton("🎵 صوت MP3", callback_data=f"{platform}_mp3"),
        ],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel")],
    ])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 الإحصائيات", callback_data="admin_stats"),
            InlineKeyboardButton("👥 المستخدمون", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("📢 إذاعة", callback_data="admin_broadcast"),
        ],
    ])


# ──────────────────── شريط التقدم ────────────────────

async def animate_progress(message, stop_event: asyncio.Event):
    frames = [
        "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%",
        "🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜ 10%",
        "🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 20%",
        "🟩🟩🟩⬜⬜⬜⬜⬜⬜⬜ 30%",
        "🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 40%",
        "🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50%",
        "🟩🟩🟩🟩🟩🟩⬜⬜⬜⬜ 60%",
        "🟩🟩🟩🟩🟩🟩🟩⬜⬜⬜ 70%",
        "🟩🟩🟩🟩🟩🟩🟩🟩⬜⬜ 80%",
        "🟩🟩🟩🟩🟩🟩🟩🟩🟩⬜ 90%",
        "🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩 ✅",
    ]
    idx = 0
    while not stop_event.is_set():
        frame = frames[min(idx, len(frames) - 2)]
        try:
            await message.edit_text(f"⏳ جارِ التحميل...\n\n{frame}")
        except Exception:
            pass
        idx = min(idx + 1, len(frames) - 2)
        await asyncio.sleep(1.5)


# ──────────────────── الأوامر ────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 أهلاً <b>{user.first_name}</b>!\n\n"
        "🤖 أنا بوت التحميل الشامل — أرسل لي أي رابط من:\n\n"
        "  🎬 <b>يوتيوب</b> — فيديو أو شورت (مع خيار MP3)\n"
        "  📸 <b>انستغرام</b> — ريلز أو بوست\n"
        "  🎵 <b>تيك توك</b> — بدون علامة مائية\n"
        "  📘 <b>فيسبوك</b> — فيديوهات\n"
        "  🐦 <b>تويتر / X</b> — فيديوهات\n\n"
        "📌 فقط أرسل الرابط وأنا أتكفل بالباقي!\n\n"
        "⚡ <b>Dev:</b> @xuwjj — Marco"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>طريقة الاستخدام:</b>\n\n"
        "1️⃣ أرسل رابط فيديو من أي منصة مدعومة\n"
        "2️⃣ اختر نوع التحميل (فيديو أو صوت)\n"
        "3️⃣ انتظر لحظات واستلم الملف\n\n"
        "⚙️ <b>الأوامر:</b>\n"
        "/start — رسالة الترحيب\n"
        "/help — المساعدة\n"
        "/admin — لوحة الإدارة (للمسؤول فقط)\n\n"
        "⚡ <b>Dev:</b> @xuwjj — Marco"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ هذا الأمر للمسؤولين فقط.")
        return
    await update.message.reply_text(
        "🛠 <b>لوحة الإدارة</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


# ──────────────────── استقبال الروابط ────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text(
            "🔗 أرسل لي رابط فيديو من يوتيوب، انستغرام، تيك توك، فيسبوك، أو تويتر."
        )
        return

    if not url.startswith("http"):
        url = "https://" + url

    platform = detect_platform(url)
    if not platform:
        await update.message.reply_text("❌ هذا الرابط غير مدعوم.")
        return

    ctx.user_data["url"] = url
    ctx.user_data["platform"] = platform

    platform_name = PLATFORM_NAMES.get(platform, platform)

    if platform == "youtube":
        await update.message.reply_chat_action(ChatAction.TYPING)
        info = await get_info(url)

        if info:
            caption = (
                f"{platform_name}\n\n"
                f"📌 <b>{info['title']}</b>\n"
                f"👤 {info['uploader']}\n"
                f"⏱ {info['duration']}  •  👁 {info['views']}\n\n"
                "🎛 اختر صيغة التحميل:"
            )
        else:
            caption = f"{platform_name}\n\n🎛 اختر صيغة التحميل:"

        await update.message.reply_text(
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=youtube_keyboard(),
        )
    else:
        await update.message.reply_text(
            f"{platform_name}\n\n🎛 اختر نوع التحميل:",
            parse_mode=ParseMode.HTML,
            reply_markup=social_keyboard(platform),
        )


# ──────────────────── معالجة الأزرار ────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        ctx.user_data.pop("url", None)
        ctx.user_data.pop("platform", None)
        await query.edit_message_text("❌ تم الإلغاء.")
        return

    if data.startswith("admin_"):
        await _handle_admin_callback(query, ctx, data)
        return

    url = ctx.user_data.get("url")
    if not url:
        await query.edit_message_text("⚠️ انتهت الجلسة — أرسل الرابط مرة أخرى.")
        return

    mode, quality, audio_q = _parse_callback_data(data)

    if not mode:
        await query.edit_message_text("❌ خيار غير معروف.")
        return

    stop_event = asyncio.Event()
    progress_msg = await query.edit_message_text("⏳ جارِ التحميل...\n\n⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%")
    anim_task = asyncio.create_task(animate_progress(progress_msg, stop_event))

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = await download_media(
                url=url,
                output_dir=tmp_dir,
                mode=mode,
                quality=quality,
                audio_quality=audio_q,
            )

            stop_event.set()
            await anim_task

            if not result["ok"]:
                await progress_msg.edit_text(
                    f"❌ {result['error']}", parse_mode=ParseMode.HTML
                )
                return

            await progress_msg.edit_text(
                "🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩 ✅\n\n📤 جارِ الرفع إلى تيليجرام..."
            )

            file_path = result["path"]
            caption = (
                f"📥 {result['filename']}\n"
                f"💾 {result['size_mb']} MB\n\n"
                f"⚡ @xuwjj — Marco"
            )
            user = update.effective_user

            with open(file_path, "rb") as f:
                if mode == "mp3":
                    await query.message.reply_audio(
                        audio=InputFile(f, filename=result["filename"]),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await query.message.reply_video(
                        video=InputFile(f, filename=result["filename"]),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                    )

            track(
                user.id,
                user.username or user.first_name,
                ctx.user_data.get("platform", "unknown"),
            )

            await progress_msg.delete()

    except Exception as e:
        stop_event.set()
        logger.error(f"handle_callback error: {e}")
        await progress_msg.edit_text(
            f"❌ حدث خطأ غير متوقع:\n<code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )

    finally:
        ctx.user_data.pop("url", None)
        ctx.user_data.pop("platform", None)


def _parse_callback_data(data: str) -> tuple:
    parts = data.split("_")

    if len(parts) >= 2 and parts[-1] == "mp3":
        return ("mp3", "best", "192")
    if len(parts) == 3 and parts[1] == "mp3":
        return ("mp3", "best", parts[2])
    if len(parts) >= 2 and parts[-1] == "vid":
        return ("video", "best", "192")
    if len(parts) == 3 and parts[1] == "vid":
        return ("video", parts[2], "192")

    return (None, None, None)


# ──────────────────── إدارة الأدمن ────────────────────

async def _handle_admin_callback(query, ctx, data: str):
    if query.from_user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔ غير مصرح.")
        return

    stats = load_stats()

    if data == "admin_stats":
        total = stats.get("total_downloads", 0)
        users_count = len(stats.get("users", {}))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dl = stats.get("daily", {}).get(today, 0)

        text = (
            "📊 <b>إحصائيات البوت</b>\n\n"
            f"👥 عدد المستخدمين: <b>{users_count}</b>\n"
            f"📥 إجمالي التحميلات: <b>{total}</b>\n"
            f"📅 تحميلات اليوم: <b>{today_dl}</b>\n"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)

    elif data == "admin_users":
        users = stats.get("users", {})
        if not users:
            await query.edit_message_text("📭 لا يوجد مستخدمون بعد.")
            return

        lines = ["👥 <b>آخر المستخدمين:</b>\n"]
        sorted_users = sorted(
            users.items(),
            key=lambda x: x[1].get("last_active", ""),
            reverse=True,
        )[:20]

        for uid, info in sorted_users:
            lines.append(
                f"• <code>{uid}</code> — @{info.get('username', '—')} "
                f"({info.get('downloads', 0)} تحميل)"
            )

        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML)

    elif data == "admin_broadcast":
        ctx.user_data["awaiting_broadcast"] = True
        await query.edit_message_text(
            "📢 أرسل الرسالة التي تريد إذاعتها لجميع المستخدمين.\n"
            "أرسل /cancel للإلغاء."
        )


async def handle_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_broadcast"):
        return False

    if update.message.text == "/cancel":
        ctx.user_data.pop("awaiting_broadcast", None)
        await update.message.reply_text("❌ تم إلغاء الإذاعة.")
        return True

    if update.effective_user.id not in ADMIN_IDS:
        return False

    ctx.user_data.pop("awaiting_broadcast", None)
    broadcast_text = update.message.text
    stats = load_stats()
    users = stats.get("users", {})

    sent, failed = 0, 0
    status_msg = await update.message.reply_text(
        f"📢 جارِ الإرسال إلى {len(users)} مستخدم..."
    )

    for uid in users:
        try:
            await ctx.bot.send_message(
                chat_id=int(uid),
                text=broadcast_text,
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"✅ تم الإرسال!\n\n📨 نجح: {sent}\n❌ فشل: {failed}"
    )
    return True


# ──────────────────── التوجيه الرئيسي ────────────────────

async def message_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("awaiting_broadcast"):
        handled = await handle_broadcast(update, ctx)
        if handled:
            return
    await handle_message(update, ctx)


# ──────────────────── نقطة التشغيل ────────────────────

def main():
    print("🤖 جارِ تشغيل البوت...")
    print(f"✅ yt-dlp version: {yt_dlp.version.__version__}")
    print("✅ الوضع: مكتبة بايثون مباشرة (بدون subprocess)")
    print("⚡ Dev: @xuwjj — Marco")

    # ✅ تشغيل Health Server للاستضافة المجانية
    Thread(target=start_health_server, daemon=True).start()
    print(f"✅ Health server started on port {os.environ.get('PORT', 10000)}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    print("✅ البوت يعمل الآن!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
