import logging
import os
import time

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

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 DENOV AVTO SAVDO",
                    url=CHANNEL_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ OBUNANI TASDIQLASH",
                    callback_data=f"check_sub:{user.id}",
                )
            ],
        ]
    )

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=(
                f"📣 <b>Hurmatli {mention}!</b>\n\n"
                "Guruhda yozish uchun avval asosiy kanalimizga obuna bo‘ling.\n\n"
                "Obuna bo‘lganingizdan so‘ng "
                "«OBUNANI TASDIQLASH» tugmasini bosing.\n\n"
                "<i>Tugmani bosib obunangizni tekshiring.</i>"
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        last_notice[user.id] = now
    except TelegramError:
        log.exception("Obuna xabarini yuborib bo'lmadi. user_id=%s", user.id)


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
        try:
            await restore_group_permissions(context, target_user_id)
        except TelegramError:
            # Ruxsatni tiklashda xato bo'lsa ham, obuna tekshiruvi muvaffaqiyatli bo'lganini yashirmaymiz.
            log.exception("Guruh ruxsatini tiklashda xato. user_id=%s", target_user_id)

        name = user_display_name(query.from_user)
        await query.answer(
            f"🎉 Hurmatli {query.from_user.full_name}, "
            "asosiy Denov Avto Savdo kanalimizga obuna bo‘lganingiz uchun minnatdormiz! "
            "Endi guruhda bemalol yozishingiz mumkin.",
            show_alert=True,
        )
        log.info("Obuna tasdiqlandi. user_id=%s name=%s", target_user_id, name)
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
    user = update.effective_user

    if not message or not chat or chat.id != GROUP_ID or not user:
        return

    if user.is_bot:
        return

    # Buyruqlarni o'chirmaymiz (masalan /help).
    if message.text and message.text.startswith("/"):
        return

    if await is_subscribed(context, user.id):
        return

    # Obuna bo'lmagan odamning xabarini o'chiramiz.
    try:
        await message.delete()
    except TelegramError:
        log.exception(
            "Foydalanuvchi xabarini o'chirib bo'lmadi. "
            "Bot adminmi va delete_messages huquqi bormi? user_id=%s",
            user.id,
        )

    # Yozish klaviaturasini yopmaymiz; har bir xabar yuborilganda obuna eslatiladi.
    await send_subscription_prompt(context, user)


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
