import logging
import os
import time

from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("denov-avto-nazorat")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.getenv("CHANNEL_ID", "@denov_avto_savdo").strip()
CHANNEL_URL = os.getenv(
    "CHANNEL_URL", "https://t.me/denov_avto_savdo"
).strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1003945847242"))

ACTIVE_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}

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


# Bir foydalanuvchiga obuna eslatmasini ko‘pi bilan 60 soniyada bir yuboradi.
NOTICE_COOLDOWN_SECONDS = 60


async def is_subscribed(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> bool:
    """Kanal obunasini tekshiradi. Tekshirish xato bo‘lsa, ruxsat bermaydi."""
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ACTIVE_STATUSES
    except TelegramError:
        log.exception("Kanal obunasini tekshirishda xato (user_id=%s)", user_id)
        return False


async def allow_user(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=ALLOW_PERMISSIONS,
    )


async def notify_to_subscribe(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """Guruhdagi obuna eslatmalarini takror-takror yubormaslik uchun cheklaydi."""
    now = time.monotonic()
    last_sent = context.application.bot_data.setdefault("last_notice", {})
    if now - last_sent.get(user_id, 0) < NOTICE_COOLDOWN_SECONDS:
        return

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f'<a href="tg://user?id={user_id}">Siz</a>, guruhda yozish uchun avval '
                f'<a href="{CHANNEL_URL}">Denov Avto Savdo kanaliga obuna bo‘ling</a>.\n'
                "Obuna bo‘lgach, bemalol yozishingiz mumkin."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        last_sent[user_id] = now
    except TelegramError:
        log.exception("Obuna eslatmasini yuborib bo‘lmadi (user_id=%s)", user_id)


async def clean_service_messages(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.id != GROUP_ID:
        return

    # Telegramning avtomatik qo‘shildi/chiqdi xizmat xabarini o‘chiradi.
    try:
        await message.delete()
    except TelegramError:
        log.exception("Xizmat xabarini o‘chirib bo‘lmadi")

    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        # Yangi a'zoning yozish klaviaturasini ochiq qoldiramiz.
        # Obunasiz a'zo yozishga uringanda xabari o'chirilib, eslatma yuboriladi.
        if await is_subscribed(context, member.id):
            try:
                await allow_user(context, member.id)
            except TelegramError:
                log.exception("Obunali yangi a'zoga ruxsat berilmadi")


async def moderate_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat or chat.id != GROUP_ID:
        return
    if user.is_bot:
        return

    if await is_subscribed(context, user.id):
        return

    # Klaviatura ochiq qoladi: xabar o'chiriladi, lekin yozish huquqi olinmaydi.
    try:
        await message.delete()
    except TelegramError:
        log.exception("Obunasiz foydalanuvchi xabarini o'chirib bo'lmadi")

    await notify_to_subscribe(context, user.id)


async def post_init(application: Application) -> None:
    """Kanal ID sini aniqlaydi va logga yozadi."""
    try:
        channel = await application.bot.get_chat(CHANNEL_ID)
        application.bot_data["channel_id"] = channel.id
        log.info("Kanal ulandi: %s (%s)", channel.title, channel.id)
    except TelegramError:
        log.exception(
            "Kanalga ulanib bo‘lmadi. Kanal manzili va bot adminligini tekshiring."
        )


async def channel_membership_changed(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    change = update.chat_member
    if not change:
        return

    expected_channel_id = context.application.bot_data.get("channel_id")
    if expected_channel_id is None or change.chat.id != expected_channel_id:
        return

    user = change.new_chat_member.user
    if user.is_bot:
        return

    was_subscribed = change.old_chat_member.status in ACTIVE_STATUSES
    now_subscribed = change.new_chat_member.status in ACTIVE_STATUSES
    if now_subscribed == was_subscribed:
        return

    try:
        if now_subscribed:
            # Oldingi versiya cheklagan bo'lsa, obuna bo'lganda ruxsatni tiklaymiz.
            await allow_user(context, user.id)
            log.info("Obuna tasdiqlandi; guruhda yozish ruxsati tiklandi.")
        else:
            # Obunadan chiqsa ham yozish klaviaturasi yopilmaydi.
            log.info("Foydalanuvchi obunadan chiqdi; xabarlar moderatsiya qilinadi.")
    except TelegramError:
        log.exception("Kanal obunasi o'zgarganda guruh ruxsatini yangilashda xato")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(
        ChatMemberHandler(
            channel_membership_changed,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

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

    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & ~filters.StatusUpdate.ALL
            & ~filters.COMMAND,
            moderate_group_message,
        ),
        group=1,
    )

    log.info("Denov Avto Nazorat Bot ishga tushmoqda...")
    app.run_polling(
        allowed_updates=["message", "chat_member", "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
