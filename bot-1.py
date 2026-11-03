
import logging
import os
import time
from html import escape

from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.getenv("CHANNEL_ID", "@denov_avto_savdo")
CHANNEL_URL = os.getenv(
    "CHANNEL_URL",
    "https://t.me/denov_avto_savdo"
)
GROUP_ID = int(os.getenv("GROUP_ID", "-1003945847242"))

ACTIVE_STATUSES = {
    "member",
    "administrator",
    "creator",
}

NOTICE_COOLDOWN = 60

# Obuna bo'lgan foydalanuvchiga yozish huquqini qaytarish
ALLOW_PERMISSIONS = ChatPermissions(
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


async def is_subscribed(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            CHANNEL_ID,
            user_id
        )
        return member.status in ACTIVE_STATUSES

    except TelegramError:
        log.exception("Obunani tekshirishda xato")
        return False


async def allow_user(context, user_id):
    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=ALLOW_PERMISSIONS,
    )


async def send_subscription_notice(context, user):
    now = time.monotonic()

    notices = context.application.bot_data.setdefault(
        "last_notice", {}
    )

    if now - notices.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    if user.username:
        name = "@" + escape(user.username)
    else:
        name = escape(user.first_name)

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"📢 Hurmatli {name}!\n\n"
                "Guruhda yozish uchun avval "
                '<a href="' + escape(CHANNEL_URL, quote=True) +
                '">Denov Avto Savdo</a> kanaliga obuna bo‘ling.\n\n'
                "✅ Obuna bo‘lgach, guruhda bemalol "
                "yozishingiz mumkin."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        notices[user.id] = now

    except TelegramError:
        log.exception("Obuna eslatmasini yuborishda xato")


# Yangi a'zolar va xizmat xabarlarini boshqarish
async def clean_service_messages(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or chat.id != GROUP_ID:
        return

    try:
        await message.delete()
    except TelegramError:
        log.exception("Xizmat xabarini o'chirishda xato")


# Guruhdagi oddiy xabarlarni tekshirish
async def moderate_group_message(update, context):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.id != GROUP_ID or user.is_bot:
        return

    # Admin va guruh egalarining xabarlariga tegmaymiz
    if message.sender_chat:
        return

    try:
        member = await context.bot.get_chat_member(
            GROUP_ID,
            user.id
        )

        if member.status in ("administrator", "creator"):
            return

    except TelegramError:
        log.exception("Guruh a'zosini tekshirishda xato")

    if await is_subscribed(context, user.id):
        return

    # Obuna bo'lmagan odamning xabarini o'chiramiz
    try:
        await message.delete()
    except TelegramError:
        log.exception("Foydalanuvchi xabarini o'chirishda xato")

    await send_subscription_notice(context, user)


# Kanalga obuna bo'lish yoki obunadan chiqishni kuzatish
async def channel_membership_changed(update, context):
    change = update.chat_member

    if not change:
        return

    channel_id = context.application.bot_data.get("channel_id")

    if channel_id is None or change.chat.id != channel_id:
        return

    user = change.new_chat_member.user

    if user.is_bot:
        return

    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status

    was_subscribed = old_status in ACTIVE_STATUSES
    now_subscribed = new_status in ACTIVE_STATUSES

    # Faqat yangi obuna bo'lganida yozish huquqini ochamiz.
    # Guruhga minnatdorchilik xabari yuborilmaydi.
    if now_subscribed and not was_subscribed:
        try:
            await allow_user(context, user.id)
            log.info("Obuna tasdiqlandi: %s", user.id)

        except TelegramError:
            log.exception("Yozish huquqini ochishda xato")


async def post_init(application):
    try:
        channel = await application.bot.get_chat(CHANNEL_ID)
        application.bot_data["channel_id"] = channel.id

        log.info("Kanal ulandi: %s", channel.title)

    except TelegramError:
        log.exception("Kanalga ulanishda xato")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Kanal a'zolarining obunasini kuzatish
    app.add_handler(
        ChatMemberHandler(
            channel_membership_changed,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    # Yangi a'zo va chiqish xizmat xabarlarini o'chirish
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (
                filters.StatusUpdate.NEW_CHAT_MEMBERS
                | filters.StatusUpdate.LEFT_CHAT_MEMBER
            ),
            clean_service_messages,
        ),
        group=0,
    )

    # Guruhdagi obuna tekshiruvi
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & ~filters.StatusUpdate.ALL
            & ~filters.COMMAND,
            moderate_group_message,
        ),
        group=1,
    )

    log.info("Denov Avto Nazorat Bot ishga tushdi!")

    app.run_polling(
        allowed_updates=[
            "message",
            "chat_member",
            "my_chat_member",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
