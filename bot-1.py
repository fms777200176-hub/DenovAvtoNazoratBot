
import logging
import os
import time
from html import escape

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("DenovAvtoNazorat")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.getenv("CHANNEL_ID", "@denov_avto_savdo")
CHANNEL_URL = os.getenv(
    "CHANNEL_URL",
    "https://t.me/denov_avto_savdo"
)
GROUP_ID = int(os.getenv("GROUP_ID", "-1003945847242"))

ACTIVE_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    "creator",
}

NOTICE_COOLDOWN = 60

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

DENY_PERMISSIONS = ChatPermissions(
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


async def is_subscribed(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            CHANNEL_ID,
            user_id,
        )
        return member.status in ACTIVE_STATUSES

    except TelegramError:
        log.exception("Kanal obunasini tekshirishda xato")
        return False


async def allow_user(context, user_id):
    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=ALLOW_PERMISSIONS,
    )


async def restrict_user(context, user_id):
    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=DENY_PERMISSIONS,
    )


def get_user_name(user):
    if user.username:
        return "@" + escape(user.username)
    return escape(user.first_name)


async def send_subscription_notice(context, user):
    now = time.monotonic()

    notices = context.application.bot_data.setdefault(
        "last_notice", {}
    )

    if now - notices.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    name = get_user_name(user)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 DENOV AVTO SAVDO KANALIGA OBUNA BO‘LISH",
                url=CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ TASDIQLASH",
                callback_data=f"verify:{user.id}",
            )
        ],
    ])

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"📢 Hurmatli {name}!\n\n"
                "Guruhda yozish uchun avval "
                "asosiy kanalimizga obuna bo‘ling:\n\n"
                "🔗 <b>Denov Avto Savdo</b>\n\n"
                "Obuna bo‘lganingizdan keyin "
                "«TASDIQLASH» tugmasini bosing."
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        notices[user.id] = now

    except TelegramError:
        log.exception("Obuna eslatmasini yuborishda xato")


# TASDIQLASH tugmasi
async def verify_subscription(update, context):
    query = update.callback_query

    if not query:
        return

    try:
        target_id = int(query.data.split(":")[1])
    except (ValueError, IndexError, AttributeError):
        await query.answer("Tugma xato.", show_alert=True)
        return

    # Boshqa odam uning tugmasidan foydalana olmaydi.
    if query.from_user.id != target_id:
        await query.answer(
            "Bu tugma boshqa foydalanuvchi uchun.",
            show_alert=True,
        )
        return

    subscribed = await is_subscribed(
        context,
        query.from_user.id,
    )

    if not subscribed:
        await query.answer(
            "❌ Siz hali kanalga obuna bo‘lmagansiz. "
            "Avval kanalga obuna bo‘ling!",
            show_alert=True,
        )
        return

    try:
        await allow_user(context, query.from_user.id)

        # Eslatma xabarini tugma bilan birga olib tashlash
        if query.message:
            try:
                await query.message.delete()
            except TelegramError:
                log.warning("Eslatma xabarini o‘chirib bo‘lmadi")

        # Minnatdorchilik faqat tugmani bosgan odamga ko‘rinadi.
        await query.answer(
            "🎉 Hurmatli "
            + (
                "@" + query.from_user.username
                if query.from_user.username
                else query.from_user.first_name
            )
            + "!\n\n"
            "Denov Avto Savdo asosiy kanalimizga "
            "obuna bo‘lganingiz uchun minnatdormiz!\n\n"
            "✅ Endi guruhda bemalol yozishingiz mumkin.",
            show_alert=True,
        )

        log.info(
            "Obuna tasdiqlandi: %s",
            query.from_user.id,
        )

    except TelegramError:
        log.exception("Tasdiqlashda xato")
        await query.answer(
            "Xatolik yuz berdi. Iltimos, qayta urinib ko‘ring.",
            show_alert=True,
        )


# Guruhdagi xabarlarni tekshirish
async def moderate_group_message(update, context):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.id != GROUP_ID or user.is_bot:
        return

    if message.sender_chat:
        return

    # Admin va guruh egalariga tegmaymiz.
    try:
        member = await context.bot.get_chat_member(
            GROUP_ID,
            user.id,
        )

        if member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            "creator",
        ):
            return

    except TelegramError:
        log.exception("Guruh a'zosini tekshirishda xato")
        return

    if await is_subscribed(context, user.id):
        return

    try:
        await message.delete()
    except TelegramError:
        log.exception("Obunasiz foydalanuvchi xabarini o‘chirishda xato")

    await send_subscription_notice(context, user)


# Yangi a'zo va xizmat xabarlarini tozalash
async def clean_service_messages(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat or chat.id != GROUP_ID:
        return

    try:
        await message.delete()
    except TelegramError:
        log.exception("Xizmat xabarini o‘chirishda xato")


# Kanalga obuna bo‘lish va obunadan chiqishni kuzatish
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

    was_subscribed = (
        change.old_chat_member.status in ACTIVE_STATUSES
    )
    now_subscribed = (
        change.new_chat_member.status in ACTIVE_STATUSES
    )

    if was_subscribed == now_subscribed:
        return

    if was_subscribed and not now_subscribed:
        try:
            await restrict_user(context, user.id)
            log.info("Obunadan chiqdi, yozish cheklangan: %s", user.id)

        except TelegramError:
            log.exception("Obunadan chiqqan foydalanuvchini cheklashda xato")

    # Yangi obuna bo‘lganda guruhga xabar yubormaymiz.
    # Foydalanuvchi TASDIQLASH tugmasini bosishi kerak.


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

    app.add_handler(
        ChatMemberHandler(
            channel_membership_changed,
            ChatMemberHandler.CHAT_MEMBER,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            verify_subscription,
            pattern=r"^verify:\d+$",
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
            & ~filters.StatusUpdate.ALL,
            moderate_group_message,
        ),
        group=1,
    )

    log.info("Denov Avto Nazorat Bot ishga tushdi!")

    app.run_polling(
        allowed_updates=[
            "message",
            "chat_member",
            "callback_query",
            "my_chat_member",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
