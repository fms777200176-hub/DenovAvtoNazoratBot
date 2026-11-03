
import logging
import os
import time
from html import escape

from telegram import (
    Update,
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

# ==============================
# SOZLAMALAR
# ==============================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("DenovAvtoNazorat")

BOT_TOKEN = os.environ["BOT_TOKEN"]

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    "@denov_avto_savdo"
)

CHANNEL_URL = os.getenv(
    "CHANNEL_URL",
    "https://t.me/denov_avto_savdo"
)

GROUP_ID = int(
    os.getenv("GROUP_ID", "-1003945847242")
)

NOTICE_COOLDOWN = 60

ACTIVE_STATUSES = {
    "member",
    "administrator",
    "creator",
}


# ==============================
# FOYDALANUVCHI ISMI
# ==============================

def get_user_name(user):
    if user.username:
        return "@" + escape(user.username)

    return escape(user.first_name)


# ==============================
# KANAL OBUNASINI TEKSHIRISH
# ==============================

async def is_subscribed(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            CHANNEL_ID,
            user_id
        )

        return member.status in ACTIVE_STATUSES

    except TelegramError:
        log.exception("Kanal obunasini tekshirishda xato")
        return None


# ==============================
# OBUNA BO'LISH XABARI
# ==============================

async def send_subscription_notice(context, user):
    now = time.monotonic()

    notices = context.application.bot_data.setdefault(
        "last_notice", {}
    )

    # 60 soniyada bir martadan ortiq yubormaslik
    if now - notices.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    name = get_user_name(user)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 DENOV AVTO SAVDO KANALI",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ OBUNANI TASDIQLASH",
                callback_data=f"verify:{user.id}"
            )
        ]
    ])

    text = (
        f"📢 <b>Hurmatli {name}!</b>\n\n"
        "Guruhda yozish uchun avval asosiy "
        "kanalimizga obuna bo‘ling.\n\n"
        f"🔗 <b><a href=\"{escape(CHANNEL_URL, quote=True)}\">"
        "Denov Avto Savdo</a></b>\n\n"
        "Obuna bo‘lganingizdan so‘ng "
        "«OBUNANI TASDIQLASH» tugmasini bosing.\n\n"
        "<i>Tugmani bosib obunangizni tekshiring.</i>"
    )

    try:
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

        notices[user.id] = now

    except TelegramError:
        log.exception("Obuna xabarini yuborishda xato")


# ==============================
# OBUNANI TASDIQLASH TUGMASI
# ==============================

async def verify_subscription(update, context):
    query = update.callback_query

    if not query:
        return

    try:
        target_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        await query.answer(
            "Tugma xato.",
            show_alert=True
        )
        return

    # Tugmani faqat o'z egasi bosishi mumkin
    if query.from_user.id != target_id:
        await query.answer(
            "Bu tugma boshqa foydalanuvchi uchun.",
            show_alert=True
        )
        return

    subscribed = await is_subscribed(
        context,
        query.from_user.id
    )

    if subscribed is None:
        await query.answer(
            "Obunani tekshirishda xatolik. "
            "Birozdan so‘ng qayta urinib ko‘ring.",
            show_alert=True
        )
        return

    if not subscribed:
        await query.answer(
            "❌ Siz hali kanalga obuna bo‘lmagansiz!\n\n"
            "Avval Denov Avto Savdo kanaliga obuna bo‘ling.",
            show_alert=True
        )
        return

    # Faqat tugmani bosgan odamga ko'rinadigan xabar
    if query.from_user.username:
        display_name = "@" + query.from_user.username
    else:
        display_name = query.from_user.first_name

    thank_you = (
        f"🎉 Hurmatli {display_name}!\n\n"
        "Denov Avto Savdo asosiy kanalimizga "
        "obuna bo‘lganingiz uchun minnatdormiz!\n\n"
        "✅ Endi guruhda bemalol yozishingiz mumkin."
    )

    await query.answer(
        thank_you,
        show_alert=True
    )

    # Tasdiqlangan tugmali xabarni guruhdan olib tashlash
    if query.message:
        try:
            await query.message.delete()
        except TelegramError:
            log.warning(
                "Tasdiqlash xabarini o'chirib bo'lmadi"
            )

    log.info(
        "Obuna tasdiqlandi: %s",
        query.from_user.id
    )


# ==============================
# GURUH XABARLARINI NAZORAT QILISH
# ==============================

async def moderate_group_message(update, context):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.id != GROUP_ID:
        return

    if user.is_bot:
        return

    # Anonim admin xabarlariga tegmaymiz
    if message.sender_chat:
        return

    # Adminlar va guruh egalariga tegmaymiz
    try:
        member = await context.bot.get_chat_member(
            GROUP_ID,
            user.id
        )

        if member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ):
            return

    except TelegramError:
        log.exception("Guruh a'zosini tekshirishda xato")
        return

    subscribed = await is_subscribed(
        context,
        user.id
    )

    # Tekshirishda texnik xato bo'lsa, xabarni o'chirmaymiz
    if subscribed is None:
        return

    # Obuna bo'lgan odam bemalol yozadi
    if subscribed:
        return

    # Obuna bo'lmagan odamning xabarini o'chiramiz
    try:
        await message.delete()
    except TelegramError:
        log.exception("Xabarni o'chirishda xato")

    # Kanalga obuna bo'lish tugmalarini yuboramiz
    await send_subscription_notice(context, user)


# ==============================
# YANGI A'ZO XIZMAT XABARLARI
# ==============================

async def clean_service_messages(update, context):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.id != GROUP_ID:
        return

    try:
        await message.delete()
    except TelegramError:
        log.exception("Xizmat xabarini o'chirishda xato")


# ==============================
# KANAL OBUNASI O'ZGARISHI
# ==============================

async def channel_membership_changed(update, context):
    change = update.chat_member

    if not change:
        return

    channel_id = context.application.bot_data.get(
        "channel_id"
    )

    if channel_id is None:
        return

    if change.chat.id != channel_id:
        return

    user = change.new_chat_member.user

    if user.is_bot:
        return

    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status

    was_subscribed = old_status in ACTIVE_STATUSES
    now_subscribed = new_status in ACTIVE_STATUSES

    if was_subscribed == now_subscribed:
        return

    if now_subscribed:
        log.info(
            "Kanalga obuna bo'ldi: %s",
            user.id
        )

    else:
        log.info(
            "Kanal obunasidan chiqdi: %s",
            user.id
        )

    # DIQQAT:
    # Hech kimning guruhda yozish huquqini
    # cheklamaymiz yoki o'zgartirmaymiz.
    # Keyingi yuborgan xabarida obunasi tekshiriladi.


# ==============================
# BOT ISHGA TUSHISHI
# ==============================

async def post_init(application):
    try:
        channel = await application.bot.get_chat(
            CHANNEL_ID
        )

        application.bot_data["channel_id"] = channel.id

        log.info(
            "Kanal ulandi: %s",
            channel.title
        )

    except TelegramError:
        log.exception("Kanalga ulanishda xato")


# ==============================
# ASOSIY FUNKSIYA
# ==============================

def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Kanal obunasi o'zgarishini kuzatish
    app.add_handler(
        ChatMemberHandler(
            channel_membership_changed,
            ChatMemberHandler.CHAT_MEMBER
        )
    )

    # Obunani tasdiqlash tugmasi
    app.add_handler(
        CallbackQueryHandler(
            verify_subscription,
            pattern=r"^verify:\d+$"
        )
    )

    # Yangi a'zo va chiqish xabarlarini tozalash
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (
                filters.StatusUpdate.NEW_CHAT_MEMBERS
                | filters.StatusUpdate.LEFT_CHAT_MEMBER
            ),
            clean_service_messages
        ),
        group=0
    )

    # Guruhdagi xabarlarni tekshirish
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & ~filters.StatusUpdate.ALL,
            moderate_group_message
        ),
        group=1
    )

    log.info("Denov Avto Nazorat Bot ishga tushdi!")

    app.run_polling(
        allowed_updates=[
            "message",
            "chat_member",
            "callback_query",
            "my_chat_member"
        ],
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
