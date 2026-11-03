
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
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =====================================
# SOZLAMALAR
# =====================================

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

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("DenovAvtoNazorat")


# =====================================
# OBUNANI TEKSHIRISH
# =====================================

async def is_subscribed(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id,
        )

        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            "creator",
        )

    except TelegramError:
        log.exception("Kanal obunasini tekshirishda xato")
        return None


# =====================================
# ESKI CHEKLOVNI OCHISH
# FAQAT OBUNA TASDIQLANGANDA ISHLAYDI
# =====================================

async def allow_user(context, user_id):
    permissions = ChatPermissions(
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

    await context.bot.restrict_chat_member(
        chat_id=GROUP_ID,
        user_id=user_id,
        permissions=permissions,
    )


# =====================================
# FOYDALANUVCHI NIKNAME
# =====================================

def user_name(user):
    if user.username:
        return "@" + escape(user.username)

    return escape(user.first_name)


# =====================================
# OBUNA XABARINI YUBORISH
# =====================================

async def send_subscription_notice(context, user):

    notices = context.application.bot_data.setdefault(
        "last_notice", {}
    )

    now = time.monotonic()

    if now - notices.get(user.id, 0) < NOTICE_COOLDOWN:
        return

    name = user_name(user)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 DENOV AVTO SAVDO",
                url=CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ OBUNANI TASDIQLASH",
                callback_data=f"verify:{user.id}",
            )
        ],
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
            disable_web_page_preview=True,
        )

        notices[user.id] = now

    except TelegramError:
        log.exception("Obuna xabarini yuborishda xato")


# =====================================
# OBUNANI TASDIQLASH
# =====================================

async def verify_subscription(update, context):

    query = update.callback_query

    if not query:
        return

    try:
        target_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        await query.answer(
            "Tugma xato.",
            show_alert=True,
        )
        return

    # Tugmani faqat o'z egasi bosishi mumkin
    if query.from_user.id != target_id:
        await query.answer(
            "Bu tugma siz uchun emas.",
            show_alert=True,
        )
        return

    subscribed = await is_subscribed(
        context,
        query.from_user.id,
    )

    if subscribed is None:
        await query.answer(
            "Obunani tekshirishda xatolik. "
            "Keyinroq qayta urinib ko‘ring.",
            show_alert=True,
        )
        return

    if not subscribed:
        await query.answer(
            "❌ Siz hali kanalga obuna bo‘lmagansiz. "
            "Avval kanalga obuna bo‘ling!",
            show_alert=True,
        )
        return

    # Obuna bo'lgan foydalanuvchining eski cheklovini ochish
    try:
        await allow_user(
            context,
            query.from_user.id,
        )

    except TelegramError:
        log.exception("Yozish huquqini ochishda xato")

        await query.answer(
            "Obuna tasdiqlandi, lekin guruhdagi "
            "cheklovni ochib bo‘lmadi. "
            "Admin bilan bog‘laning.",
            show_alert=True,
        )
        return

    # Minnatdorchilik faqat foydalanuvchining o'ziga ko'rinadi
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
        show_alert=True,
    )

    # Tasdiqlangan eski tugmali xabarni o'chirish
    if query.message:
        try:
            await query.message.delete()
        except TelegramError:
            log.warning("Tugmali xabarni o'chirib bo'lmadi")

    log.info(
        "Obuna tasdiqlandi: %s",
        query.from_user.id,
    )


# =====================================
# GURUH XABARLARINI TEKSHIRISH
# =====================================

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

    # Xizmat xabarlarini bu yerda tekshirmaymiz
    if message.new_chat_members or message.left_chat_member:
        return

    # Anonim admin xabarlariga tegmaymiz
    if message.sender_chat:
        return

    # Guruh adminlariga tegmaymiz
    try:
        member = await context.bot.get_chat_member(
            chat_id=GROUP_ID,
            user_id=user.id,
        )

        if member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            "creator",
        ):
            return

    except TelegramError:
        log.exception("Guruh a'zosini tekshirishda xato")
        return

    subscribed = await is_subscribed(
        context,
        user.id,
    )

    # Texnik xatoda foydalanuvchining xabarini o'chirmaymiz
    if subscribed is None:
        return

    # Obuna bo'lgan odam bemalol yozadi
    if subscribed:
        return

    # Obuna bo'lmagan odamning xabarini o'chiramiz
    try:
        await message.delete()
    except TelegramError:
        log.exception("Foydalanuvchi xabarini o'chirishda xato")

    # Obuna tugmalarini yuboramiz
    await send_subscription_notice(
        context,
        user,
    )


# =====================================
# YANGI A'ZO XIZMAT XABARLARINI O'CHIRISH
# =====================================

async def clean_service_messages(update, context):

    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.id != GROUP_ID:
        return

    if not (
        message.new_chat_members
        or message.left_chat_member
    ):
        return

    try:
        await message.delete()
    except TelegramError:
        log.exception("Xizmat xabarini o'chirishda xato")


# =====================================
# BOTNI ISHGA TUSHIRISH
# =====================================

async def post_init(application):

    try:
        channel = await application.bot.get_chat(
            CHANNEL_ID
        )

        application.bot_data["channel_id"] = channel.id

        log.info(
            "Kanal ulandi: %s",
            channel.title,
        )

    except TelegramError:
        log.exception(
            "Kanalga ulanishda xato. CHANNEL_ID ni tekshiring."
        )


def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Tasdiqlash tugmasi
    app.add_handler(
        CallbackQueryHandler(
            verify_subscription,
            pattern=r"^verify:\d+$",
        )
    )

    # Yangi a'zo va chiqish xizmat xabarlari
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            clean_service_messages,
        ),
        group=0,
    )

    # Guruhdagi oddiy xabarlarni tekshirish
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS,
            moderate_group_message,
        ),
        group=1,
    )

    log.info("Denov Avto Nazorat Bot ishga tushdi!")

    app.run_polling(
        allowed_updates=[
            "message",
            "callback_query",
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
