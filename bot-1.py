import logging
import os

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
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/denov_avto_savdo").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1003945847242"))

# Permissions restored after a user subscribes.
NORMAL_PERMISSIONS = ChatPermissions(
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

NO_SEND_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)


async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError:
        log.exception("Kanal obunasini tekshirishda xato (user_id=%s)", user_id)
        # Fail closed: if Telegram cannot confirm membership, do not allow posting.
        return False


async def restrict_user(context, user_id: int) -> None:
    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=NO_SEND_PERMISSIONS,
    )


async def allow_user(context, user_id: int) -> None:
    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=NORMAL_PERMISSIONS,
    )


async def notify_to_subscribe(context, user_id: int) -> None:
    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                "📢 Guruhda yozish uchun avval "
                f'<a href="{CHANNEL_URL}">Denov Avto Savdo kanaliga obuna bo‘ling</a>.\n'
                "Obuna bo‘lgach, guruhga qaytib yozishingiz mumkin."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except TelegramError:
        log.exception("Obuna eslatmasini yuborib bo‘lmadi")


async def clean_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or chat.id != GROUP_ID:
        return

    # Remove Telegram's automatic “user joined/left” service messages.
    try:
        await message.delete()
    except TelegramError:
        log.exception("Kirish/chiqish xizmat xabarini o‘chirib bo‘lmadi")

    # If a new member joined, immediately check and restrict them if needed.
    for member in (message.new_chat_members or []):
        if member.is_bot:
            continue
        if await is_subscribed(context, member.id):
            continue
        try:
            await restrict_user(context, member.id)
            await notify_to_subscribe(context, member.id)
        except TelegramError:
            log.exception("Yangi a'zoni cheklashda xato")


async def moderate_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat or chat.id != GROUP_ID:
        return
    if user.is_bot:
        return

    if await is_subscribed(context, user.id):
        return

    # Delete the non-subscriber's message and prevent further posting.
    try:
        await message.delete()
    except TelegramError:
        log.exception("Obunasiz foydalanuvchi xabarini o‘chirib bo‘lmadi")

    try:
        await restrict_user(context, user.id)
        await notify_to_subscribe(context, user.id)
    except TelegramError:
        log.exception("Obunasiz foydalanuvchini cheklashda xato")


async def post_init(application: Application) -> None:
    # Resolve the channel ID once at startup and store it for update filtering.
    try:
        channel = await application.bot.get_chat(CHANNEL_ID)
        application.bot_data["channel_id"] = channel.id
        log.info("Kanal ulandi: %s (%s)", channel.title, channel.id)
    except TelegramError:
        log.exception("Kanalga ulanib bo‘lmadi. Bot kanal administratori ekanini tekshiring.")


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

    active = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    }
    was_subscribed = change.old_chat_member.status in active
    now_subscribed = change.new_chat_member.status in active

    if now_subscribed and not was_subscribed:
        try:
            await allow_user(context, user.id)
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text="✅ Kanalga obunangiz tasdiqlandi. Endi guruhda yozishingiz mumkin.",
            )
        except TelegramError:
            log.exception("Obuna bo‘lgach cheklovni olib tashlashda xato")
    elif was_subscribed and not now_subscribed:
        try:
            await restrict_user(context, user.id)
            await notify_to_subscribe(context, user.id)
        except TelegramError:
            log.exception("Kanal obunasidan chiqqan a'zoni cheklashda xato")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Channel membership changes: subscribe -> allow, unsubscribe -> restrict.
    app.add_handler(
        ChatMemberHandler(channel_membership_changed, ChatMemberHandler.CHAT_MEMBER)
    )

    # Delete automatic join/leave service messages, and check new members.
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER),
            clean_service_messages,
        ),
        group=0,
    )

    # Check every ordinary group message for channel subscription.
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
        allowed_updates=["message", "chat_member", "my_chat_member"]
    )


if __name__ == "__main__":
    main()
