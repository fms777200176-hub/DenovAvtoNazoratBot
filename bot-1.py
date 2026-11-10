import asyncio
import logging
import os
import time
import sqlite3

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- SOZLAMALAR ----------------

BOT_TOKEN = os.environ["BOT_TOKEN"]

CHANNEL_ID = os.getenv("CHANNEL_ID", "@denov_avto_savdo").strip()
CHANNEL_URL = os.getenv(
    "CHANNEL_URL", "https://t.me/denov_avto_savdo"
).strip()

GROUP_ID = int(os.getenv("GROUP_ID", "-1003945847242"))

# Bir foydalanuvchiga takroriy obuna xabari yuborish oralig'i
NOTICE_COOLDOWN = 30
INVITE_TARGET = 3
DB_PATH = os.getenv("DB_PATH", "denov_avto_nazorat.sqlite3")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("denov-avto-nazorat")

# Kanalda obunachi hisoblanadigan holatlar
ACTIVE_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER if hasattr(ChatMemberStatus, "OWNER")
    else ChatMemberStatus.CREATOR,
}

# Oldin bot cheklagan obunachining guruhda yozish huquqini tiklash
FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


# ---------------- YORDAMCHI FUNKSIYALAR ----------------

async def is_subscribed(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """Foydalanuvchining asosiy kanalga obunasini tekshiradi."""
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ACTIVE_STATUSES
    except TelegramError:
        log.exception("Obunani tekshirishda xato. user_id=%s", user_id)
        return False


async def restore_group_permissions(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """Avval cheklangan foydalanuvchining yozish huquqini tiklaydi."""
    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=FULL_PERMISSIONS,
    )


def user_display_name(user) -> str:
    """Ismni xavfsiz HTML ko'rinishida tayyorlaydi."""
    from html import escape

    name = user.full_name or "Foydalanuvchi"
    return escape(name)


# ---------------- TAKLIFLAR HISOBI (DOIMIY SQLITE) ----------------

def init_db() -> None:
    """Taklif havolalari va amaldagi takliflarni restartdan keyin ham saqlaydi."""
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
        con.execute("CREATE INDEX IF NOT EXISTS idx_inviter ON invite_joins(inviter_user_id)")


def get_invite_link(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT invite_link FROM invite_links WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else None


def save_invite_link(user_id: int, link: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO invite_links(user_id, invite_link) VALUES(?, ?)",
            (user_id, link),
        )


def invite_count(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM invite_joins WHERE inviter_user_id=?", (user_id,)
        ).fetchone()
        return int(row[0])


def record_invite_join(inviter_id: int, invited_id: int) -> None:
    if inviter_id == invited_id:
        return
    with sqlite3.connect(DB_PATH) as con:
        # Bitta odam bir vaqtda faqat bitta taklif qiluvchiga hisoblanadi.
        con.execute(
            "INSERT OR REPLACE INTO invite_joins(invited_user_id, inviter_user_id, joined_at) "
            "VALUES(?, ?, ?)",
            (invited_id, inviter_id, int(time.time())),
        )


def remove_invited_member(invited_id: int) -> None:
    # Tanlangan qoida: taklif qilingan a'zo chiqsa, uning hissasi hisobdan ayriladi.
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM invite_joins WHERE invited_user_id=?", (invited_id,))


async def ensure_personal_invite_link(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    link = get_invite_link(user_id)
    if link:
        return link
    invite = await context.bot.create_chat_invite_link(
        chat_id=GROUP_ID,
        name=f"3-taklif-{user_id}",
        creates_join_request=False,
    )
    save_invite_link(user_id, invite.invite_link)
    return invite.invite_link


async def track_group_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Guruhga yangi odamni kim qo'shganini hisoblaydi; chiqsa hisobdan ayiradi."""
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

        # Agar shaxsiy taklif havolasi ishlatilgan bo'lsa, uning egasini aniqlaymiz.
        if change.invite_link:
            link = change.invite_link.invite_link
            with sqlite3.connect(DB_PATH) as con:
                row = con.execute(
                    "SELECT user_id FROM invite_links WHERE invite_link=?", (link,)
                ).fetchone()
            if row:
                inviter_id = int(row[0])

        # Kontaktlardan bevosita guruhga qo'shilgan odam:
        # Telegram update'dagi from_user — qo'shishni bajargan foydalanuvchi.
        if inviter_id is None:
            actor = change.from_user
            if actor and actor.id != member.id and not actor.is_bot:
                inviter_id = actor.id

        if inviter_id is not None and inviter_id != member.id:
            record_invite_join(inviter_id, member.id)
            log.info("Yangi a'zo hisoblandi. inviter=%s invited=%s", inviter_id, member.id)

    elif left:
        remove_invited_member(member.id)
        log.info("A'zo chiqdi, taklif hisobi ayrildi. user_id=%s", member.id)


# GURUHDA BOT YUBORGAN BARCHA XABARLAR 60 SONIYADAN KEYIN O'CHIRILADI.
# Bu muddat barcha foydalanuvchilar uchun bir xil; xabar bilan uning tugmalari ham yo'qoladi.
BOT_NOTICE_TTL = 60


async def send_temporary_group_message(
    context: ContextTypes.DEFAULT_TYPE,
    **kwargs,
):
    """Guruhga bot xabarini yuboradi va 60 soniyadan keyin hamma uchun o'chiradi."""
    sent = await context.bot.send_message(chat_id=GROUP_ID, **kwargs)

    async def delete_later():
        await asyncio.sleep(BOT_NOTICE_TTL)
        try:
            await context.bot.delete_message(
                chat_id=sent.chat_id,
                message_id=sent.message_id,
            )
            log.info(
                "Bot xabari 60 soniyadan keyin o'chirildi. message_id=%s",
                sent.message_id,
            )
        except TelegramError:
            log.exception(
                "Bot xabarini o'chirish amalga oshmadi. "
                "Bot adminligi va delete_messages huquqini tekshiring. message_id=%s",
                sent.message_id,
            )

    # Application.create_task coroutine'ni o'zi boshqaradi. Avval asyncio.create_task
    # bilan Task yaratib, keyin uni yana create_task'ga berish noto'g'ri edi:
    # PTB coroutine kutadi, Task emas. Shu xato taymerni ishga tushirmasligi mumkin.
    context.application.create_task(
        delete_later(),
        name=f"delete-group-notice-{sent.message_id}",
    )
    return sent


# ---------------- OBUNA TUGMALARI ----------------

async def send_subscription_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    user,
) -> None:
    """
    Guruhda obuna bo'lish xabarini yuboradi.
    Telegram guruh xabarini faqat bitta odamga ko'rsatish imkonini bermaydi:
    bu xabar guruh a'zolariga ko'rinadi, ammo tugma natijasi faqat bosgan odamga chiqadi.
    """
    now = time.monotonic()
    last_notice = context.application.bot_data.setdefault("last_notice", {})
    if now - last_notice.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    name = user_display_name(user)
    mention = f'<a href="tg://user?id={user.id}">{name}</a>'

    try:
        count = invite_count(user.id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 DENOV AVTO SAVDO KANALIGA OBUNA BO‘LISH", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ OBUNA VA 3 TA ODAMNI TASDIQLASH", callback_data=f"check_sub:{user.id}")],
        ])
        await send_temporary_group_message(
            context,
            text=(
                f"📣 <b>Hurmatli {mention}!</b>\n\n"
                "Guruhda yozish uchun kanalga obuna bo‘ling va kontaktlaringizdan 3 ta yangi odamni guruhga qo‘shing.\n\n"
                f"👥 Qo‘shgan odamlaringiz hisobi: <b>{count}/{INVITE_TARGET}</b>\n"
                ""
                "So‘ng tasdiqlash tugmasini bosing.\n\n"
                "<i>Ikkala shart bajarilgandan keyin guruhda yozishingiz mumkin.</i>"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        last_notice[user.id] = now
    except TelegramError:
        log.exception("Obuna/taklif xabarini yuborib bo'lmadi. user_id=%s", user.id)


# ---------------- TASDIQLASH TUGMASI ----------------

async def check_subscription_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    try:
        target_user_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("Tugma xato. Iltimos, yangi xabarni kuting.", show_alert=True)
        return

    # Boshqa odam birovning tugmasini bossa, obunani uning nomidan tekshirmaymiz.
    if query.from_user.id != target_user_id:
        await query.answer(
            "Bu tugma boshqa foydalanuvchi uchun. O'zingiz yozganingizda chiqqan tugmani bosing.",
            show_alert=True,
        )
        return

    if await is_subscribed(context, target_user_id):
        count = invite_count(target_user_id)
        if count >= INVITE_TARGET:
            name = user_display_name(query.from_user)
            await query.answer(
                f"🎉 Hurmatli {query.from_user.full_name}, barcha shartlar bajarildi. Endi guruhda yozishingiz mumkin!",
                show_alert=True,
            )
            log.info("Kirish talablari bajarildi. user_id=%s name=%s", target_user_id, name)
        else:
            await query.answer(
                f"✅ Kanal obunasi tasdiqlandi. Yana {INVITE_TARGET-count} ta yangi odamni kontaktlaringizdan guruhga qo‘shing.",
                show_alert=True,
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ QAYTA TASDIQLASH", callback_data=f"check_sub:{target_user_id}")]
            ])
            await send_temporary_group_message(
                context,
                text=f"👥 Qo‘shgan odamlaringiz hisobi: {count}/{INVITE_TARGET}. Yana {INVITE_TARGET-count} ta yangi odamni kontaktlaringizdan guruhga qo‘shing, so‘ng qayta tekshiring.",
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    else:
        await query.answer(
            "❌ Siz hali kanalga obuna bo‘lmagansiz. "
            "Avval kanalga obuna bo‘ling, keyin yana tasdiqlang.",
            show_alert=True,
        )


# ---------------- GURUH XABARLARINI TEKSHIRISH ----------------

async def moderate_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or chat.id != GROUP_ID:
        return

    # Kanal postining muhokama guruhiga avtomatik uzatilgan nusxasiga tegmaymiz.
    if getattr(message, "is_automatic_forward", False):
        return

    user = update.effective_user
    if not user:
        return

    if user.is_bot:
        return

    # Buyruqlarni o'chirmaymiz (masalan /help).
    if message.text and message.text.startswith("/"):
        return

    subscribed = await is_subscribed(context, user.id)
    invited = invite_count(user.id)
    if subscribed and invited >= INVITE_TARGET:
        return

    # Talablardan biri bajarilmagan bo'lsa, oddiy foydalanuvchi xabarini o'chiramiz.
    try:
        await message.delete()
    except TelegramError:
        log.exception(
            "Foydalanuvchi xabarini o'chirib bo'lmadi. "
            "Bot adminmi va delete_messages huquqi bormi? user_id=%s",
            user.id,
        )

    # Qaysi shart bajarilmaganini aniq ko'rsatamiz.
    if not subscribed:
        await send_subscription_prompt(context, user)
    else:
        now = time.monotonic()
        last_notice = context.application.bot_data.setdefault("last_notice", {})
        if now - last_notice.get(user.id, 0) >= NOTICE_COOLDOWN:
            try:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ 3 TA ODAMNI TASDIQLASH", callback_data=f"check_sub:{user.id}")]
                ])
                await send_temporary_group_message(
                    context,
                    text=(
                        f"👥 Guruhda yozish uchun yana <b>{max(0, INVITE_TARGET - invited)} ta</b> yangi odamni kontaktlaringizdan guruhga qo‘shing.\n"
                        f"Hisobingiz: <b>{invited}/{INVITE_TARGET}</b>\n"
                        "Siz qo‘shgan odam guruhdan chiqsa, hisobdan ayriladi.\n"
                        "3 ta odam qo‘shilgach, tasdiqlash tugmasini bosing."
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                last_notice[user.id] = now
            except TelegramError:
                log.exception("Taklif eslatmasini yuborishda xato. user_id=%s", user.id)


async def clean_service_messages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Guruhga qo'shilish/chiqish haqidagi avtomatik xizmat xabarlarini o'chiradi."""
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or chat.id != GROUP_ID:
        return

    try:
        await message.delete()
    except TelegramError:
        log.exception("Guruh xizmat xabarini o'chirib bo'lmadi")


# ---------------- KANAL OBUNASI O'ZGARISHI ----------------

async def channel_membership_changed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    change = update.chat_member
    if not change:
        return

    channel_id = context.application.bot_data.get("channel_id")
    if channel_id is None or change.chat.id != channel_id:
        return

    user = change.new_chat_member.user
    if user.is_bot:
        return

    was_subscribed = change.old_chat_member.status in ACTIVE_STATUSES
    is_now_subscribed = change.new_chat_member.status in ACTIVE_STATUSES

    if was_subscribed == is_now_subscribed:
        return

    if is_now_subscribed:
        try:
            await restore_group_permissions(context, user.id)
            log.info("Kanalga obuna bo'ldi. user_id=%s", user.id)
        except TelegramError:
            log.exception("Obuna bo'lganda ruxsatni tiklashda xato. user_id=%s", user.id)
    else:
        # Guruh yozish joyini yopmaymiz. Obunadan chiqqan odam yozsa,
        # uning xabari o'chiriladi va obuna tugmalari yuboriladi.
        log.info("Kanal obunasidan chiqdi. user_id=%s", user.id)


# ---------------- ISHGA TUSHIRISH ----------------

async def post_init(application: Application) -> None:
    try:
        channel = await application.bot.get_chat(CHANNEL_ID)
        application.bot_data["channel_id"] = channel.id
        log.info("Kanal topildi: %s (%s)", channel.title, channel.id)
    except TelegramError:
        log.exception(
            "Kanalga ulanib bo'lmadi. CHANNEL_ID/CHANNEL_URL va "
            "botning kanaldagi adminligini tekshiring."
        )


def main() -> None:
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Kanal obunasi o'zgarganini kuzatadi.
    app.add_handler(
        ChatMemberHandler(
            channel_membership_changed,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # Tugmani bosgan odamning obunasini tekshiradi.
    app.add_handler(CallbackQueryHandler(check_subscription_button, pattern=r"^check_sub:"))

    # Guruhga kim taklif havolasi bilan kirganini va kim chiqib ketganini kuzatadi.
    app.add_handler(
        ChatMemberHandler(track_group_member, ChatMemberHandler.CHAT_MEMBER),
        group=-1,
    )

    # Yangi a'zo / chiqib ketgan a'zo xizmat xabarlarini tozalaydi.
    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & (filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER),
            clean_service_messages,
        )
    )

    # Guruhdagi oddiy xabarlarni tekshiradi.
    app.add_handler(
        MessageHandler(
            filters.Chat(GROUP_ID)
            & ~filters.COMMAND
            & ~filters.StatusUpdate.ALL,
            moderate_group_message,
        )
    )

    log.info("Denov Avto Nazorat Bot ishga tushmoqda...")
    app.run_polling(
        allowed_updates=["message", "chat_member", "callback_query", "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
