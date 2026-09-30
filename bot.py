import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
from config import BOT_TOKEN, LEGACY_CHANNEL_ID, OPERATOR_IDS, REQUIRED_CHANNEL
from transliterate import TRANSLIT_MODES, transliterate

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

MAX_CAPTION_LEN = 1024  # Telegramning video/rasm caption limiti
ALBUM_DEBOUNCE = 1.5  # soniya — albomning barcha elementlari kelishini kutamiz

album_messages: dict[str, list[Message]] = {}
album_tasks: dict[str, asyncio.Task] = {}

WELCOME_TEXT = (
    "Salom! 👋\n\n"
    "Men kanalga tashlangan video/rasmlarga <b>siz belgilagan izohni avtomatik "
    "qo'shib boruvchi</b> botman. Lotin va kirill orasida avtomatik o'girish "
    "imkoniyati ham bor.\n\n"
    "<b>Qanday ishlatiladi:</b>\n"
    "1. Meni o'z kanalingizga <b>admin</b> qilib qo'shing.\n"
    "2. Berilgan huquqlar orasida <b>\"Xabarlarni tahrirlash\" (Edit Messages)</b> "
    "ni yoqing.\n"
    "3. Shu yerga (shaxsiy chatga) qaytib, /setcaption buyrug'i bilan izoh matnini "
    "kiriting.\n\n"
    "Shundan keyin o'sha kanalga tashlangan har bir yangi video, rasm yoki "
    "albomga izohingiz avtomatik qo'shiladi. Har kim faqat o'zi qo'shgan "
    "kanal(lar)ni boshqaradi — boshqalarnikiga kirolmaydi.\n\n"
    "<b>Buyruqlar:</b>\n"
    "/setcaption — kanal tanlab, izoh belgilash\n"
    "/caption — bir kanalning joriy izohini ko'rish\n"
    "/clearcaption — bir kanal uchun avtomatik izohni to'xtatish\n"
    "/translit — kanal uchun lotin↔kirill o'girish yo'nalishini tanlash\n"
    "/channels — sizga tegishli kanallar ro'yxati"
)


def channels_keyboard(prefix: str, owner_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"{prefix}:{channel_id}")]
        for channel_id, title in db.list_channels_for_owner(owner_id)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _no_channels_message(message: Message) -> None:
    await message.answer(
        "Sizga tegishli hech qanday kanal topilmadi.\n\n"
        "Avval meni kanalingizga admin qilib qo'shing va "
        "<b>\"Xabarlarni tahrirlash\" (Edit Messages)</b> huquqini yoqing — "
        "shundan keyin bu yerga avtomatik qo'shiladi."
    )


# ---------- Majburiy obuna (@namanganliklar_uz) ----------

SUBSCRIBE_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Kanalga o'tish",
            url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}",
        )],
        [InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub")],
    ]
)


async def _is_subscribed(user_id: int) -> "bool | None":
    """True/False — aniq natija. None — tekshirib bo'lmadi (masalan bot
    REQUIRED_CHANNEL'ga qo'shilmagan) — bunda foydalanishga ruxsat beramiz
    (fail-open), aks holda texnik xatolik hammani bloklab qo'yishi mumkin."""
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member") or getattr(
            member, "is_member", False
        )
    except TelegramAPIError as e:
        logging.warning("Obuna tekshiruvida xatolik (user_id=%s): %s", user_id, e)
        return None


async def _check_subscription(user_id: int) -> bool:
    """Buyruqni davom ettirish mumkinmi (obuna bo'lgan yoki tekshirib
    bo'lmagan holat) — faqat aniq "obuna emas" holatida False qaytaradi."""
    return await _is_subscribed(user_id) is not False


async def require_subscription_message(message: Message) -> bool:
    if await _check_subscription(message.from_user.id):
        return True
    await message.answer(
        f"Botdan foydalanish uchun avval {REQUIRED_CHANNEL} kanaliga obuna "
        "bo'ling, so'ng \"✅ Obuna bo'ldim\" tugmasini bosing.",
        reply_markup=SUBSCRIBE_KEYBOARD,
    )
    return False


async def require_subscription_callback(callback: CallbackQuery) -> bool:
    if await _check_subscription(callback.from_user.id):
        return True
    await callback.answer(
        f"Avval {REQUIRED_CHANNEL} kanaliga obuna bo'ling.", show_alert=True
    )
    return False


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    if await _is_subscribed(callback.from_user.id) is False:
        await callback.answer("Hali obuna bo'lmagansiz.", show_alert=True)
        return
    await callback.answer("✅ Rahmat!")
    await callback.message.edit_text(WELCOME_TEXT)


# ---------- Kanal bot admin qilib qo'shilganda/olib tashlanganda ----------

@dp.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated):
    if update.chat.type != "channel":
        return

    status = update.new_chat_member.status
    logging.info(
        "my_chat_member: chat_id=%s, title=%s, status=%s, from_user=%s",
        update.chat.id, update.chat.title, status,
        update.from_user.id if update.from_user else None,
    )

    if status == "administrator":
        if not update.from_user:
            logging.warning("chat_id=%s: from_user yo'q, ro'yxatga olinmadi.", update.chat.id)
            return

        owner_id = update.from_user.id
        title = update.chat.title or str(update.chat.id)
        db.register_channel(update.chat.id, title, owner_id)

        can_edit = getattr(update.new_chat_member, "can_edit_messages", False)
        if can_edit:
            text = (
                f"✅ Kanal ro'yxatga olindi: <b>{title}</b>\n"
                "Endi /setcaption orqali shu kanal uchun izoh belgilashingiz mumkin."
            )
        else:
            text = (
                f"⚠️ <b>{title}</b> qo'shildi, lekin ishlashi uchun kanal admin "
                "sozlamalarida <b>\"Xabarlarni tahrirlash\" (Edit Messages)</b> "
                "huquqini ham yoqishingiz kerak."
            )

        needs_subscription = await _is_subscribed(owner_id) is False
        if needs_subscription:
            text += (
                f"\n\n⚠️ Botdan foydalanish uchun avval {REQUIRED_CHANNEL} "
                "kanaliga obuna bo'lishingiz kerak."
            )

        try:
            await bot.send_message(
                owner_id, text,
                reply_markup=SUBSCRIBE_KEYBOARD if needs_subscription else None,
            )
        except TelegramBadRequest:
            # Foydalanuvchi botga hali /start bosmagan bo'lishi mumkin —
            # bunday holda unga shaxsiy xabar yubora olmaymiz.
            logging.warning("owner_id=%s ga xabar yuborib bo'lmadi.", owner_id)
    elif status in ("left", "kicked", "member"):
        db.remove_channel(update.chat.id)


# ---------- Umumiy buyruqlar ----------

@dp.message(Command("start", "help"), F.chat.type == "private")
async def cmd_start(message: Message):
    if not await require_subscription_message(message):
        return
    await message.answer(WELCOME_TEXT)


@dp.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: Message):
    if message.from_user.id not in OPERATOR_IDS:
        return

    text = (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Foydalanuvchilar: {db.count_owners()}\n"
        f"📢 Ulangan kanallar: {db.count_channels()}"
    )
    await message.answer(text)


@dp.message(Command("channels"), F.chat.type == "private")
async def cmd_channels(message: Message):
    if not await require_subscription_message(message):
        return
    channels = db.list_channels_for_owner(message.from_user.id)
    if not channels:
        await _no_channels_message(message)
        return
    lines = []
    for channel_id, title in channels:
        has_caption = "✅ izoh bor" if db.get_caption(channel_id) else "— izoh yo'q"
        mode_name = TRANSLIT_MODES[db.get_translit_mode(channel_id)][0]
        lines.append(f"• {title} ({has_caption}, {mode_name})")
    await message.answer("Sizga tegishli kanallar:\n" + "\n".join(lines))


# ---------- /setcaption ----------

class SetCaption(StatesGroup):
    waiting_for_text = State()


@dp.message(Command("setcaption"), F.chat.type == "private")
async def cmd_set_caption(message: Message):
    if not await require_subscription_message(message):
        return
    if not db.list_channels_for_owner(message.from_user.id):
        await _no_channels_message(message)
        return
    await message.answer(
        "Qaysi kanal uchun izoh belgilaymiz?",
        reply_markup=channels_keyboard("setcap", message.from_user.id),
    )


@dp.callback_query(F.data.startswith("setcap:"))
async def cb_choose_setcaption(callback: CallbackQuery, state: FSMContext):
    if not await require_subscription_callback(callback):
        return
    channel_id = int(callback.data.split(":", 1)[1])
    if db.get_channel_owner(channel_id) != callback.from_user.id:
        await callback.answer("Bu kanal sizga tegishli emas.", show_alert=True)
        return
    title = db.get_channel_title(channel_id) or str(channel_id)
    await state.set_state(SetCaption.waiting_for_text)
    await state.update_data(channel_id=channel_id)
    await callback.message.edit_text(
        f"«{title}» uchun izoh matnini yuboring — qalin/kursiv shrift, havolalar "
        f"va premium emojilar saqlanadi.\nBekor qilish uchun /cancel."
    )
    await callback.answer()


@dp.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.")


@dp.message(SetCaption.waiting_for_text, F.chat.type == "private")
async def process_new_caption(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("channel_id")
    if not channel_id or db.get_channel_owner(channel_id) != message.from_user.id:
        await state.clear()
        await message.answer("Nimadir noto'g'ri ketdi, /setcaption bilan qayta boshlang.")
        return

    plain_text = message.text or message.caption or ""
    formatted_caption = message.html_text or plain_text

    if not plain_text.strip():
        await message.answer("Matn topilmadi. Qayta yuboring yoki /cancel bilan bekor qiling.")
        return

    if len(plain_text) > MAX_CAPTION_LEN:
        await message.answer(
            f"Matn juda uzun ({len(plain_text)} belgi). Maksimum {MAX_CAPTION_LEN} belgi bo'lishi kerak."
        )
        return

    # Shu kanal uchun tanlangan yo'nalishda (lotin->kirill / kirill->lotin
    # / o'chirilgan) o'girib, shu holatda saqlaymiz
    mode = db.get_translit_mode(channel_id)
    formatted_caption = transliterate(formatted_caption, mode)

    db.set_caption(channel_id, formatted_caption)
    await state.clear()
    title = db.get_channel_title(channel_id) or str(channel_id)
    await message.answer(f"✅ «{title}» uchun izoh saqlandi. Namuna qanday chiqishini pastda ko'rasiz 👇")
    await message.answer(formatted_caption)


# ---------- /caption ----------

@dp.message(Command("caption"), F.chat.type == "private")
async def cmd_get_caption(message: Message):
    if not await require_subscription_message(message):
        return
    if not db.list_channels_for_owner(message.from_user.id):
        await _no_channels_message(message)
        return
    await message.answer(
        "Qaysi kanalning izohini ko'rmoqchisiz?",
        reply_markup=channels_keyboard("getcap", message.from_user.id),
    )


@dp.callback_query(F.data.startswith("getcap:"))
async def cb_get_caption(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    channel_id = int(callback.data.split(":", 1)[1])
    if db.get_channel_owner(channel_id) != callback.from_user.id:
        await callback.answer("Bu kanal sizga tegishli emas.", show_alert=True)
        return
    title = db.get_channel_title(channel_id) or str(channel_id)
    caption = db.get_caption(channel_id)
    await callback.answer()
    if caption:
        await callback.message.answer(f"«{title}» uchun joriy izoh:")
        await callback.message.answer(caption)
    else:
        await callback.message.answer(f"«{title}» uchun izoh hali belgilanmagan.")


# ---------- /clearcaption ----------

@dp.message(Command("clearcaption"), F.chat.type == "private")
async def cmd_clear_caption(message: Message):
    if not await require_subscription_message(message):
        return
    if not db.list_channels_for_owner(message.from_user.id):
        await _no_channels_message(message)
        return
    await message.answer(
        "Qaysi kanalning avtomatik izohini o'chiramiz?",
        reply_markup=channels_keyboard("clrcap", message.from_user.id),
    )


@dp.callback_query(F.data.startswith("clrcap:"))
async def cb_clear_caption(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    channel_id = int(callback.data.split(":", 1)[1])
    if db.get_channel_owner(channel_id) != callback.from_user.id:
        await callback.answer("Bu kanal sizga tegishli emas.", show_alert=True)
        return
    title = db.get_channel_title(channel_id) or str(channel_id)
    db.clear_caption(channel_id)
    await callback.answer("O'chirildi")
    await callback.message.edit_text(f"✅ «{title}» uchun avtomatik izoh o'chirildi.")


# ---------- /translit ----------

@dp.message(Command("translit"), F.chat.type == "private")
async def cmd_translit(message: Message):
    if not await require_subscription_message(message):
        return
    if not db.list_channels_for_owner(message.from_user.id):
        await _no_channels_message(message)
        return
    await message.answer(
        "Qaysi kanal uchun o'girish yo'nalishini tanlaymiz?",
        reply_markup=channels_keyboard("trlch", message.from_user.id),
    )


@dp.callback_query(F.data.startswith("trlch:"))
async def cb_choose_translit_channel(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    channel_id = int(callback.data.split(":", 1)[1])
    if db.get_channel_owner(channel_id) != callback.from_user.id:
        await callback.answer("Bu kanal sizga tegishli emas.", show_alert=True)
        return
    title = db.get_channel_title(channel_id) or str(channel_id)
    current = db.get_translit_mode(channel_id)
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if key == current else "") + name,
            callback_data=f"trlmode:{channel_id}:{key}",
        )]
        for key, (name, _fn) in TRANSLIT_MODES.items()
    ]
    await callback.message.edit_text(
        f"«{title}» uchun o'girish rejimini tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("trlmode:"))
async def cb_set_translit_mode(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    _, channel_id_str, mode = callback.data.split(":", 2)
    channel_id = int(channel_id_str)
    if db.get_channel_owner(channel_id) != callback.from_user.id:
        await callback.answer("Bu kanal sizga tegishli emas.", show_alert=True)
        return
    db.set_translit_mode(channel_id, mode)
    title = db.get_channel_title(channel_id) or str(channel_id)
    mode_name = TRANSLIT_MODES[mode][0]
    await callback.answer("Saqlandi")
    await callback.message.edit_text(f"✅ «{title}» uchun o'girish rejimi: {mode_name}")


# ---------- Botga to'g'ridan-to'g'ri yozilgan matnni o'girish ----------
# (kanal/izoh bilan bog'liq emas — shunchaki tez tekshirish uchun)

_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_LETTER_RE = re.compile(r"[Ѐ-ӿ]")


@dp.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def echo_translit(message: Message):
    """/setcaption kabi holatlarda ishlamaydi (o'sha handlerlar bu yerdan
    oldin ro'yxatga olingan va ustuvor bo'ladi) — shunchaki botga yozilgan
    har qanday matnni yo'nalishini avtomatik aniqlab o'giradi."""
    if not await require_subscription_message(message):
        return

    raw_text = message.text or ""
    latin_count = len(_LATIN_LETTER_RE.findall(raw_text))
    cyrillic_count = len(_CYRILLIC_LETTER_RE.findall(raw_text))
    if latin_count == 0 and cyrillic_count == 0:
        return  # harflar yo'q (faqat raqam/emoji va h.k.) — tegmaymiz

    mode = "l2c" if latin_count >= cyrillic_count else "c2l"
    html_text = message.html_text or raw_text
    await message.answer(transliterate(html_text, mode))


# ---------- Kanal postlariga avtomatik izoh qo'shish ----------

async def apply_caption(chat_id: int, message_id: int, base_html: str, caption_text: str) -> None:
    # Postning o'z matnini ham shu kanal uchun tanlangan yo'nalishda o'giramiz
    # (caption_text — admin sozlagan izoh — allaqachon o'girilgan holda saqlanadi)
    mode = db.get_translit_mode(chat_id)
    old = transliterate(base_html, mode).strip()
    new_caption = f"{old}\n\n{caption_text}" if old else caption_text
    try:
        await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=new_caption)
        logging.info("Caption yangilandi: chat_id=%s, message_id=%s", chat_id, message_id)
        db.log_caption_applied(chat_id)
    except TelegramAPIError as e:
        logging.error(
            "Caption tahrirlashda xatolik (birinchi urinish): chat_id=%s, message_id=%s, xato=%s",
            chat_id, message_id, e,
        )
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption_text)
            logging.info("Caption (zaxira variant) yangilandi: chat_id=%s, message_id=%s", chat_id, message_id)
            db.log_caption_applied(chat_id)
        except TelegramAPIError as e2:
            logging.error(
                "Caption tahrirlashda xatolik (ikkinchi urinish): chat_id=%s, message_id=%s, xato=%s",
                chat_id, message_id, e2,
            )


@dp.channel_post(~F.media_group_id, F.video | F.photo)
async def handle_channel_post(message: Message):
    """Yakka (albom bo'lmagan) video/rasm postlari."""
    caption_text = db.get_caption(message.chat.id)
    if not caption_text:
        return
    await apply_caption(message.chat.id, message.message_id, message.html_text or "", caption_text)


@dp.channel_post(F.media_group_id, F.video | F.photo)
async def handle_album_item(message: Message):
    """Albom (bir vaqtda yuborilgan bir nechta video/rasm) postlari."""
    group_id = message.media_group_id
    album_messages.setdefault(group_id, []).append(message)

    if group_id in album_tasks:
        album_tasks[group_id].cancel()
    album_tasks[group_id] = asyncio.create_task(process_album(group_id))


async def process_album(group_id: str) -> None:
    await asyncio.sleep(ALBUM_DEBOUNCE)
    messages = album_messages.pop(group_id, [])
    album_tasks.pop(group_id, None)
    if not messages:
        return

    first = min(messages, key=lambda m: m.message_id)
    caption_text = db.get_caption(first.chat.id)
    if not caption_text:
        return

    # Telegram butun albom uchun faqat BITTA elementning (birinchisining)
    # izohini ko'rsatadi — shu sabab faqat o'shanga yozamiz
    await apply_caption(first.chat.id, first.message_id, first.html_text or "", caption_text)


async def main():
    db.init_db()
    if LEGACY_CHANNEL_ID:
        try:
            chat = await bot.get_chat(LEGACY_CHANNEL_ID)
            default_owner = OPERATOR_IDS[0] if OPERATOR_IDS else None
            if default_owner:
                db.register_channel(chat.id, chat.title or str(chat.id), default_owner)
        except Exception as e:
            logging.warning("Eski CHANNEL_ID uchun ma'lumot olinmadi: %s", e)
        db.migrate_legacy_caption(LEGACY_CHANNEL_ID)

    # Omma uchun ochilishidan oldin ro'yxatga olingan, egasi biriktirilmagan
    # kanallarni botning operatoriga (siz) bog'laymiz — bir martalik migratsiya.
    if OPERATOR_IDS:
        db.assign_default_owner(OPERATOR_IDS[0])

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
