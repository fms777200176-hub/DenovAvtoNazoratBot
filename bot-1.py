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
        con.execute("""
            CREATE TABLE IF NOT EXISTS success_notices (
                user_id INTEGER PRIMARY KEY,
                sent_at INTEGER NOT NULL
            )
        """)
        # 3 ta odam qo'shish shartini bir marta bajargan foydalanuvchi
        # keyinchalik taklif qilingan odamlar chiqib ketsa ham ruxsatini saqlaydi.
        con.execute("""
            CREATE TABLE IF NOT EXISTS permanent_unlocks (
                user_id INTEGER PRIMARY KEY,
                unlocked_at INTEGER NOT NULL
            )
        """)
        # Migratsiya: eski bazada 3 ta amaldagi taklifi yoki oldingi
        # muvaffaqiyat xabari qayd etilgan foydalanuvchilarni saqlab qolamiz.
        con.execute("""
            INSERT OR IGNORE INTO permanent_unlocks(user_id, unlocked_at)
            SELECT inviter_user_id, ? FROM invite_joins
            GROUP BY inviter_user_id
            HAVING COUNT(*) >= ?
        """, (int(time.time()), INVITE_TARGET))
        con.execute("""
            INSERT OR IGNORE INTO permanent_unlocks(user_id, unlocked_at)
            SELECT user_id, sent_at FROM success_notices
        """)


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
    # Taklif qilingan a'zo chiqsa, uning hissasi hisobdan ayriladi.
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT inviter_user_id FROM invite_joins WHERE invited_user_id=?",
            (invited_id,),
        ).fetchone()
        con.execute("DELETE FROM invite_joins WHERE invited_user_id=?", (invited_id,))
        inviter_id = int(row[0]) if row else None

    # Taklif qilingan odam chiqib ketsa ham permanent_unlocks o'chirilmaydi.
    # Bir marta 3 ta odam qo'shib ruxsat olgan foydalanuvchining huquqi saqlanadi.


def has_permanent_unlock(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT 1 FROM permanent_unlocks WHERE user_id=?", (user_id,)
        ).fetchone()
        return row is not None


def grant_permanent_unlock(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO permanent_unlocks(user_id, unlocked_at) VALUES(?, ?)",
            (user_id, int(time.time())),
        )


def has_success_notice(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT 1 FROM success_notices WHERE user_id=?", (user_id,)
        ).fetchone()
        return row is not None


def mark_success_notice(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO success_notices(user_id, sent_at) VALUES(?, ?)",
            (user_id, int(time.time())),
        )


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
            await unlock_if_eligible(context, inviter_id)

    elif left:
        remove_invited_member(member.id)
        log.info("A'zo chiqdi, taklif hisobi ayrildi. user_id=%s", member.id)


# Bot yuborgan guruh xabarlari 1 daqiqadan keyin o'chiriladi.
BOT_NOTICE_TTL = 60


async def send_temporary_group_message(
    context: ContextTypes.DEFAULT_TYPE,
    ttl: int = BOT_NOTICE_TTL,
    **kwargs,
):
    """Bot xabarini belgilangan vaqtdan keyin o'chiradi (odatda 60 soniya)."""
    sent = await context.bot.send_message(chat_id=GROUP_ID, **kwargs)

    async def delete_later():
        await asyncio.sleep(ttl)
        try:
            await sent.delete()
        except TelegramError:
            log.info("Vaqtinchalik bot xabari o'chirilmadi: message_id=%s", sent.message_id)

    asyncio.create_task(delete_later())
    return sent


SUCCESS_NOTICE_TTL = 5 * 60


async def unlock_if_eligible(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    """3 ta odam qo'shgan va kanalga obuna bo'lgan foydalanuvchini darhol ochadi."""
    # 3 ta taklif bir marta bajarilishi bilan doimiy ruxsat qayd etiladi.
    # Keyinchalik taklif qilinganlar chiqib ketsa ham bu qayd o'chmaydi.
    if invite_count(user_id) >= INVITE_TARGET:
        grant_permanent_unlock(user_id)

    # Avval doimiy ruxsat bor-yo'qligini tekshiramiz: bunday foydalanuvchidan
    # qayta 3 ta odam qo'shish talab qilinmaydi. Kanal obunasi sharti saqlanadi.
    if not has_permanent_unlock(user_id):
        return False
    if not await is_subscribed(context, user_id):
        return False

    try:
        # Telegramdagi haqiqiy guruh cheklovini ham darhol olib tashlaymiz.
        await restore_group_permissions(context, user_id)
    except TelegramError:
        log.exception("Yozish huquqini ochib bo'lmadi. user_id=%s", user_id)
        return False

    # Takroriy chat_member/message hodisalari tabrikni qayta-qayta yubormasin.
    if not has_success_notice(user_id):
        try:
            member = await context.bot.get_chat_member(GROUP_ID, user_id)
            name = user_display_name(member.user)
        except TelegramError:
            name = "Foydalanuvchi"
            log.exception("Foydalanuvchi ismini olishda xato. user_id=%s", user_id)

        mention = f'<a href="tg://user?id={user_id}">{name}</a>'
        try:
            await send_temporary_group_message(
                context,
                ttl=SUCCESS_NOTICE_TTL,
                text=(
                    f"🎉 <b>Tabriklaymiz!</b>\n\n"
                    f"Hurmatli {mention}, siz guruhimizga 3 ta odam qo‘shdingiz!\n\n"
                    "Endi bemalol yozishingiz va e’lon joylashingiz mumkin. 😊\n\n"
                    "<i>Denov Avto Savdo jamoasi</i>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            mark_success_notice(user_id)
        except TelegramError:
            log.exception("Tabrik xabarini yuborib bo'lmadi. user_id=%s", user_id)

    log.info("Yozish huquqi ochildi: user_id=%s", user_id)
    return True


# ---------------- OBUNA TUGMALARI ----------------

async def send_subscription_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    user,
) -> None:
    """Kanalga obuna bo'lmagan foydalanuvchiga obuna havolasini ko'rsatadi."""
    now = time.monotonic()
    last_notice = context.application.bot_data.setdefault("last_notice", {})
    if now - last_notice.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    name = user_display_name(user)
    mention = f'<a href="tg://user?id={user.id}">{name}</a>'
    count = invite_count(user.id)
    already_unlocked = has_permanent_unlock(user.id)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 DENOV AVTO SAVDO KANALIGA OBUNA BO‘LISH", url=CHANNEL_URL)]
    ])
    if already_unlocked:
        prompt_text = (
            f"📣 <b>Hurmatli {mention}!</b>\n\n"
            "Siz 3 ta odam qo‘shish shartini avval bajargansiz. Qayta odam qo‘shishingiz shart emas. "
            "Guruhda yozish uchun kanalimizga obuna bo‘ling."
        )
    else:
        prompt_text = (
            f"📣 <b>Hurmatli {mention}!</b>\n\n"
            "Guruhda yozish uchun avval kanalimizga obuna bo‘ling va kontaktlaringizdan "
            f"{INVITE_TARGET} ta yangi odamni guruhga qo‘shing.\n\n"
            f"👥 Qo‘shgan odamlaringiz: <b>{count}/{INVITE_TARGET}</b>"
        )
    try:
        await send_temporary_group_message(
            context,
            text=prompt_text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        last_notice[user.id] = now
    except TelegramError:
        log.exception("Obuna xabarini yuborib bo'lmadi. user_id=%s", user.id)


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
    if not user or user.is_bot:
        return

    # Buyruqlarni o'chirmaymiz (masalan /help).
    if message.text and message.text.startswith("/"):
        return

    invited = invite_count(user.id)
    permanently_unlocked = has_permanent_unlock(user.id)

    # Hozirgi hisob 3 taga yetgan bo'lsa, avval doimiy qayd qilamiz.
    if invited >= INVITE_TARGET and not permanently_unlocked:
        grant_permanent_unlock(user.id)
        permanently_unlocked = True

    subscribed = await is_subscribed(context, user.id)

    # Doimiy ruxsatli foydalanuvchiga qayta odam qo'shish sharti qo'yilmaydi.
    # Kanal obunasi alohida shart sifatida qoladi.
    if permanently_unlocked and subscribed:
        await unlock_if_eligible(context, user.id)
        return

    # Shartlardan biri bajarilmagan bo'lsa, foydalanuvchi xabarini o'chiramiz.
    try:
        await message.delete()
    except TelegramError:
        log.exception(
            "Foydalanuvchi xabarini o'chirib bo'lmadi. Bot adminmi va delete_messages huquqi bormi? user_id=%s",
            user.id,
        )

    if not subscribed:
        await send_subscription_prompt(context, user)
        return

    now = time.monotonic()
    last_notice = context.application.bot_data.setdefault("last_notice", {})
    if now - last_notice.get(user.id, 0) >= NOTICE_COOLDOWN:
        try:
            await send_temporary_group_message(
                context,
                text=(
                    f"👥 Guruhda yozish uchun yana <b>{max(0, INVITE_TARGET - invited)} ta</b> "
                    "yangi odamni kontaktlaringizdan guruhga qo‘shing.\n"
                    f"Hisobingiz: <b>{invited}/{INVITE_TARGET}</b>\n"
                    "Siz qo‘shgan odam guruhdan chiqsa, hisobdan ayriladi."
                ),
                parse_mode="HTML",
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
        log.info("Kanalga obuna bo'ldi. user_id=%s", user.id)
        # Agar 3 ta odamni oldin qo'shgan bo'lsa, hoziroq yozish huquqini ochamiz.
        await unlock_if_eligible(context, user.id)
    else:
        # Obunadan chiqqan odam yozsa, uning xabari moderatsiya tomonidan o'chiriladi.
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

    log.info("Denov Avto Nazorat Bot ishga tushmoqda... (3 taklif bir marta = doimiy ruxsat; tabrik 5 daqiqa; boshqa xabarlar 1 daqiqa)")
    app.run_polling(
        allowed_updates=["message", "chat_member", "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
