import asyncio
import logging
import os
import sqlite3
import time
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler,
    ContextTypes, MessageHandler, filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.getenv("CHANNEL_ID", "@denov_avto_savdo").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/denov_avto_savdo").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1003945847242"))
INVITE_TARGET = 3
NOTICE_COOLDOWN = 30
BOT_NOTICE_TTL = 60
DB_PATH = os.getenv("DB_PATH", "denov_avto_nazorat.sqlite3")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("denov-avto-nazorat")

ACTIVE_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER if hasattr(ChatMemberStatus, "OWNER")
    else ChatMemberStatus.CREATOR,
}


# ---------------- SQLITE ----------------

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS invite_links (
                user_id INTEGER PRIMARY KEY,
                invite_link TEXT NOT NULL UNIQUE
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS invite_joins (
                invited_user_id INTEGER PRIMARY KEY,
                inviter_user_id INTEGER NOT NULL,
                joined_at INTEGER NOT NULL
            )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_inviter ON invite_joins(inviter_user_id)"
        )


def invite_count(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM invite_joins WHERE inviter_user_id=?",
            (user_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def record_invite_join(inviter_id: int, invited_id: int):
    if inviter_id == invited_id:
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT OR REPLACE INTO invite_joins
               (invited_user_id, inviter_user_id, joined_at)
               VALUES (?, ?, ?)""",
            (invited_id, inviter_id, int(time.time())),
        )


def remove_invited_member(invited_id: int):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "DELETE FROM invite_joins WHERE invited_user_id=?",
            (invited_id,),
        )


def get_invite_link(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT invite_link FROM invite_links WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def save_invite_link(user_id: int, link: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO invite_links(user_id, invite_link) VALUES (?, ?)",
            (user_id, link),
        )


async def ensure_personal_invite_link(context, user_id: int) -> str:
    link = get_invite_link(user_id)
    if link:
        return link
    invite = await context.bot.create_chat_invite_link(
        chat_id=GROUP_ID, name=f"3-taklif-{user_id}", creates_join_request=False
    )
    save_invite_link(user_id, invite.invite_link)
    return invite.invite_link


# ---------------- YORDAMCHILAR ----------------

def user_display_name(user) -> str:
    return escape(user.full_name or "Foydalanuvchi")


async def is_subscribed(context, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ACTIVE_STATUSES
    except TelegramError:
        log.exception("Obunani tekshirishda xato: user_id=%s", user_id)
        return False


async def send_temporary_group_message(context, **kwargs):
    """Bot yuborgan guruh xabari va inline tugmalari 60 soniyadan so'ng o'chadi."""
    sent = await context.bot.send_message(chat_id=GROUP_ID, **kwargs)

    async def delete_later():
        await asyncio.sleep(BOT_NOTICE_TTL)
        try:
            await context.bot.delete_message(
                chat_id=sent.chat_id, message_id=sent.message_id
            )
            log.info("BOT_NOTICE_DELETED message_id=%s", sent.message_id)
        except TelegramError:
            log.exception(
                "BOT_NOTICE_DELETE_FAILED chat_id=%s message_id=%s",
                sent.chat_id, sent.message_id,
            )

    # create_task faqat coroutine oladi; Task'ni ikkinchi marta o'ramaymiz.
    context.application.create_task(
        delete_later(), name=f"delete-group-notice-{sent.message_id}"
    )
    return sent


# ---------------- OBUNA ESLATMASI ----------------

async def send_subscription_prompt(context, user):
    now = time.monotonic()
    last_notice = context.application.bot_data.setdefault("last_notice", {})
    if now - last_notice.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    mention = f'<a href="tg://user?id={user.id}">{user_display_name(user)}</a>'
    count = invite_count(user.id)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 DENOV AVTO SAVDO KANALIGA OBUNA BO‘LISH", url=CHANNEL_URL)],
        [InlineKeyboardButton(
            "✅ OBUNA VA 3 TA ODAMNI TASDIQLASH",
            callback_data=f"check_sub:{user.id}",
        )],
    ])
    try:
        await send_temporary_group_message(
            context,
            text=(
                f"📣 <b>Hurmatli {mention}!</b>\n\n"
                "Guruhda yozish uchun kanalga obuna bo‘ling va "
                "kontaktlaringizdan 3 ta yangi odamni guruhga qo‘shing.\n\n"
                f"👥 Hisobingiz: <b>{count}/{INVITE_TARGET}</b>\n"
                "So‘ng tasdiqlash tugmasini bosing.\n"
                "<i>Ikkala shart bajarilgandan keyin guruhda yozishingiz mumkin.</i>"
            ),
            parse_mode="HTML", reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        last_notice[user.id] = now
    except TelegramError:
        log.exception("Obuna eslatmasini yuborib bo'lmadi: user_id=%s", user.id)


# ---------------- TASDIQLASH ----------------

async def check_subscription_button(update: Update, context):
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        target_user_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Tugma xato. Yangi xabarni kuting.", show_alert=True)
        return

    if query.from_user.id != target_user_id:
        await query.answer("Bu tugma boshqa foydalanuvchi uchun.", show_alert=True)
        return

    subscribed = await is_subscribed(context, target_user_id)
    count = invite_count(target_user_id)

    if subscribed and count >= INVITE_TARGET:
        await query.answer(
            "🎉 Barcha shartlar bajarildi! Endi guruhda yozishingiz mumkin.",
            show_alert=True,
        )
        log.info("REQUIREMENTS_MET user_id=%s", target_user_id)
        return

    if not subscribed:
        await query.answer("❌ Avval kanalga obuna bo‘ling.", show_alert=True)
        return

    await query.answer(
        f"✅ Obuna tasdiqlandi. Yana {INVITE_TARGET-count} ta yangi odam qo‘shing.",
        show_alert=True,
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ QAYTA TASDIQLASH",
            callback_data=f"check_sub:{target_user_id}",
        )
    ]])
    try:
        await send_temporary_group_message(
            context,
            text=(
                f"👥 Hisobingiz: <b>{count}/{INVITE_TARGET}</b>\n"
                f"Yana <b>{INVITE_TARGET-count} ta</b> yangi odamni "
                "guruhga qo‘shing, so‘ng qayta tasdiqlang."
            ),
            parse_mode="HTML", reply_markup=keyboard,
        )
    except TelegramError:
        log.exception("Tasdiqlash eslatmasini yuborishda xato.")


# ---------------- A'ZOLAR HISOBI ----------------

async def track_group_member(update: Update, context):
    change = update.chat_member
    if not change or change.chat.id != GROUP_ID:
        return

    member = change.new_chat_member.user
    if member.is_bot:
        return

    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status
    joined = old_status not in ACTIVE_STATUSES and new_status in ACTIVE_STATUSES
    left = old_status in ACTIVE_STATUSES and new_status not in ACTIVE_STATUSES

    if joined:
        inviter_id = None
        if change.invite_link:
            with sqlite3.connect(DB_PATH) as con:
                row = con.execute(
                    "SELECT user_id FROM invite_links WHERE invite_link=?",
                    (change.invite_link.invite_link,),
                ).fetchone()
            if row:
                inviter_id = int(row[0])

        # Bevosita kontaktlardan qo'shilgan bo'lsa, from_user qo'shuvchini bildiradi.
        if inviter_id is None:
            actor = change.from_user
            if actor and actor.id != member.id and not actor.is_bot:
                inviter_id = actor.id

        if inviter_id is not None:
            record_invite_join(inviter_id, member.id)
            log.info(
                "NEW_MEMBER_COUNTED inviter=%s invited=%s count=%s",
                inviter_id, member.id, invite_count(inviter_id),
            )
    elif left:
        remove_invited_member(member.id)
        log.info("MEMBER_LEFT_INVITE_REMOVED user_id=%s", member.id)


# ---------------- GURUH MODERATSIYASI ----------------

async def moderate_group_message(update: Update, context):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.id != GROUP_ID:
        return

    # Muhokama guruhidagi avtomatik kanal posti hech qachon o'chirilmaydi.
    if getattr(message, "is_automatic_forward", False):
        return

    # Anonim admin/kanal nomidan yuborilgan xabarlarga tegmaymiz.
    if message.sender_chat:
        return

    user = update.effective_user
    if not user or user.is_bot:
        return

    subscribed = await is_subscribed(context, user.id)
    invited = invite_count(user.id)
    if subscribed and invited >= INVITE_TARGET:
        return

    try:
        await context.bot.delete_message(
            chat_id=chat.id, message_id=message.message_id
        )
        log.info(
            "USER_MESSAGE_DELETED user_id=%s message_id=%s",
            user.id, message.message_id,
        )
    except TelegramError:
        log.exception(
            "USER_MESSAGE_DELETE_FAILED user_id=%s message_id=%s",
            user.id, message.message_id,
        )

    if not subscribed:
        await send_subscription_prompt(context, user)
        return

    now = time.monotonic()
    last_notice = context.application.bot_data.setdefault("last_notice", {})
    if now - last_notice.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ 3 TA ODAMNI TASDIQLASH",
            callback_data=f"check_sub:{user.id}",
        )
    ]])
    try:
        await send_temporary_group_message(
            context,
            text=(
                f"👥 Guruhda yozish uchun yana "
                f"<b>{INVITE_TARGET-invited} ta</b> yangi odamni "
                "kontaktlaringizdan guruhga qo‘shing.\n"
                f"Hisobingiz: <b>{invited}/{INVITE_TARGET}</b>\n"
                "Qo‘shilgan odam guruhdan chiqsa, hisobdan ayriladi.\n"
                "3 ta odam qo‘shilgach, tasdiqlash tugmasini bosing."
            ),
            parse_mode="HTML", reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        last_notice[user.id] = now
    except TelegramError:
        log.exception("Taklif eslatmasida xato: user_id=%s", user.id)


async def clean_service_messages(update: Update, context):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.id != GROUP_ID:
        return
    try:
        await context.bot.delete_message(
            chat_id=chat.id, message_id=message.message_id
        )
    except TelegramError:
        log.exception("Xizmat xabarini o'chirib bo'lmadi: message_id=%s", message.message_id)


# ---------------- ISHGA TUSHIRISH ----------------

async def post_init(application: Application):
    try:
        channel = await application.bot.get_chat(CHANNEL_ID)
        application.bot_data["channel_id"] = channel.id
        log.info("CHANNEL_CONNECTED title=%s id=%s", channel.title, channel.id)
    except TelegramError:
        log.exception("Kanalga ulanib bo'lmadi. CHANNEL_ID va bot adminligini tekshiring.")

    try:
        member = await application.bot.get_chat_member(GROUP_ID, application.bot.id)
        log.info("GROUP_BOT_STATUS status=%s", member.status)
    except TelegramError:
        log.exception("Botning guruhdagi holatini tekshirib bo'lmadi.")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(
        ChatMemberHandler(track_group_member, ChatMemberHandler.CHAT_MEMBER),
        group=-1,
    )
    app.add_handler(
        CallbackQueryHandler(check_subscription_button, pattern=r"^check_sub:"),
        group=0,
    )
    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & (filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER),
            clean_service_messages,
        ),
        group=1,
    )
    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID) & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            moderate_group_message,
        ),
        group=1,
    )

    log.info("Denov Avto Nazorat Bot ishga tushmoqda...")
    app.run_polling(
        allowed_updates=["message", "chat_member", "callback_query", "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
