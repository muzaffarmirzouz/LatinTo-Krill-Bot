import asyncio
import html as html_lib
import logging
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import db
from config import BOT_TOKEN, LEGACY_CHANNEL_ID, OPERATOR_IDS, REQUIRED_CHANNEL
from transliterate import TRANSLIT_MODES, transliterate

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

MAX_CAPTION_LEN = 1024  # Telegramning media caption limiti (teglarsiz belgilar)
MAX_MESSAGE_LEN = 4096  # oddiy xabar limiti
ALBUM_DEBOUNCE = 1.5  # soniya — albomning barcha elementlari kelishini kutamiz
NOTIFY_COOLDOWN = 3600  # bir kanal bo'yicha xato xabarini soatiga bir martadan ko'p yubormaymiz
SKIP_TAG_RE = re.compile(r"#izohsiz\b", re.I)

# Izoh qo'shiladigan media turlari
MEDIA_FILTER = F.video | F.photo | F.document | F.animation | F.audio

album_messages: dict[str, list[Message]] = {}
album_tasks: dict[str, asyncio.Task] = {}
album_done: dict[str, float] = {}
_last_notified: dict[int, float] = {}

# Shaxsiy chatdagi pastki tugmalar (yozuv turini tanlash)
BTN_L2C = "🔤 Lotin → Kirill"
BTN_C2L = "🔤 Kirill → Lotin"
BTN_NEW = "✨ Yangi alifbo"
BTN_AUTO = "🔄 Avto"
USER_MODE_BUTTONS = {BTN_L2C: "l2c", BTN_C2L: "c2l", BTN_NEW: "new", BTN_AUTO: "auto"}
MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_L2C), KeyboardButton(text=BTN_C2L)],
        [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_AUTO)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Matn yuboring…",
)
USER_MODE_INFO = {
    "l2c": "Lotin → Kirill",
    "c2l": "Kirill → Lotin",
    "new": "Yangi alifbo (sh→ş, ch→ç, o'→ö, g'→ğ)",
    "auto": "Avto (lotin yozsangiz kirillga, kirill yozsangiz lotinga)",
}

WELCOME_TEXT = (
    "Salom! 👋\n\n"
    "Men kanalga tashlangan video/rasm/fayllarga <b>siz belgilagan izohni avtomatik "
    "qo'shib boruvchi</b> botman. Lotin va kirill orasida avtomatik o'girish "
    "imkoniyati ham bor.\n\n"
    "<b>Qanday ishlatiladi:</b>\n"
    "1. Meni o'z kanalingizga <b>admin</b> qilib qo'shing.\n"
    "2. Berilgan huquqlar orasida <b>\"Xabarlarni tahrirlash\" (Edit Messages)</b> "
    "ni yoqing.\n"
    "3. Shu yerga (shaxsiy chatga) qaytib, /setcaption buyrug'i bilan izoh matnini "
    "kiriting.\n\n"
    "Shundan keyin o'sha kanalga tashlangan har bir yangi video, rasm, GIF, fayl yoki "
    "albomga izohingiz avtomatik qo'shiladi. Har kim faqat o'zi qo'shgan "
    "kanal(lar)ni boshqaradi — boshqalarnikiga kirolmaydi.\n\n"
    "<b>Buyruqlar:</b>\n"
    "/setcaption — kanal tanlab, izoh belgilash\n"
    "/caption — bir kanalning joriy izohini ko'rish\n"
    "/clearcaption — bir kanal uchun avtomatik izohni to'xtatish\n"
    "/pause — izohni vaqtincha o'chirish/yoqish (izoh saqlanadi)\n"
    "/position — izoh post matnidan oldin yoki keyin turishi\n"
    "/translit — kanal uchun lotin↔kirill o'girish yo'nalishini tanlash\n"
    "/channels — sizga tegishli kanallar ro'yxati\n\n"
    "💡 Post matniga <code>#izohsiz</code> yozsangiz, o'sha postga izoh qo'shilmaydi "
    "(belgi o'zi o'chiriladi).\n\n"
    "<b>Matn o'girish (kanalsiz ham ishlaydi):</b>\n"
    "Pastdagi tugmalardan birini tanlang, so'ng botga matn yuboring:\n"
    "🔤 Lotin → Kirill\n"
    "🔤 Kirill → Lotin\n"
    "✨ Yangi alifbo — lotin yoki kirilldagi matnni yangi harflarga o'giradi: "
    "<b>sh→ş, ch→ç, o'→ö, g'→ğ</b>\n"
    "🔄 Avto — yozuvni o'zi aniqlaydi\n"
    "Buyruqlar: /lotin_kirill, /kirill_lotin, /yangi_alifbo, /avto\n\n"
    "Kanalda yangi alifbo uchun /translit orqali <b>Yangi alifbo</b> rejimini tanlang — "
    "postlardagi eski harflar avtomatik yangisiga o'zgaradi."
)


def esc(s) -> str:
    return html_lib.escape(str(s), quote=False)


def plain_len(html_text: str) -> int:
    """Telegram limiti teglarsiz (entity'lar hisobga olinmagan) belgilar
    bo'yicha hisoblanadi."""
    return len(html_lib.unescape(re.sub(r"<[^>]+>", "", html_text)))


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


async def _owned_channel(callback: CallbackQuery, part: int = 1):
    """callback_data'dan kanal ID'ni oladi va egalikni tekshiradi.
    Muvaffaqiyatda (channel_id, title) qaytaradi, aks holda None."""
    try:
        channel_id = int(callback.data.split(":")[part])
    except (ValueError, IndexError):
        await callback.answer("Noto'g'ri tugma.", show_alert=True)
        return None
    if db.get_channel_owner(channel_id) != callback.from_user.id:
        await callback.answer("Bu kanal sizga tegishli emas.", show_alert=True)
        return None
    title = db.get_channel_title(channel_id) or str(channel_id)
    return channel_id, title


async def notify_owner(channel_id: int, text: str) -> None:
    """Kanal egasiga shaxsiy xabar (soatiga ko'pi bilan bir marta)."""
    now = time.monotonic()
    if now - _last_notified.get(channel_id, -NOTIFY_COOLDOWN) < NOTIFY_COOLDOWN:
        return
    owner_id = db.get_channel_owner(channel_id)
    if not owner_id:
        return
    _last_notified[channel_id] = now
    try:
        await bot.send_message(owner_id, text)
    except TelegramAPIError as e:
        logging.warning("Egaga (%s) xabar yuborib bo'lmadi: %s", owner_id, e)


# ---------- Majburiy obuna ----------

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
    """True/False — aniq natija. None — tekshirib bo'lmadi (fail-open)."""
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member") or getattr(
            member, "is_member", False
        )
    except TelegramAPIError as e:
        logging.warning("Obuna tekshiruvida xatolik (user_id=%s): %s", user_id, e)
        return None


async def _check_subscription(user_id: int) -> bool:
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
    await callback.message.answer(
        "⌨️ Pastdagi tugmalardan yozuv turini tanlang 👇", reply_markup=MENU_KB
    )


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

        title = update.chat.title or str(update.chat.id)
        # Mavjud ega o'zgarmaydi (boshqa admin huquqni o'zgartirsa ham)
        db.register_channel(update.chat.id, title, update.from_user.id)
        owner_id = db.get_channel_owner(update.chat.id) or update.from_user.id
        if owner_id != update.from_user.id:
            logging.info(
                "chat_id=%s: egasi %s, o'zgartirgan admin %s — ega o'zgarmadi.",
                update.chat.id, owner_id, update.from_user.id,
            )

        can_edit = getattr(update.new_chat_member, "can_edit_messages", False)
        if can_edit:
            text = (
                f"✅ Kanal ro'yxatga olindi: <b>{esc(title)}</b>\n"
                "Endi /setcaption orqali shu kanal uchun izoh belgilashingiz mumkin."
            )
        else:
            text = (
                f"⚠️ <b>{esc(title)}</b> qo'shildi, lekin ishlashi uchun kanal admin "
                "sozlamalarida <b>\"Xabarlarni tahrirlash\" (Edit Messages)</b> "
                "huquqini ham yoqishingiz kerak."
            )

        needs_subscription = await _is_subscribed(owner_id) is False
        if needs_subscription:
            text += (
                f"\n\n⚠️ Botdan foydalanish uchun avval {esc(REQUIRED_CHANNEL)} "
                "kanaliga obuna bo'lishingiz kerak."
            )

        try:
            await bot.send_message(
                owner_id, text,
                reply_markup=SUBSCRIBE_KEYBOARD if needs_subscription else None,
            )
        except TelegramAPIError:
            logging.warning("owner_id=%s ga xabar yuborib bo'lmadi.", owner_id)
    elif status in ("left", "kicked", "member"):
        # Sozlamalar (izoh, rejim) o'chirilmaydi — faqat nofaol qilinadi
        owner_id = db.get_channel_owner(update.chat.id)
        title = db.get_channel_title(update.chat.id) or str(update.chat.id)
        db.deactivate_channel(update.chat.id)
        if owner_id:
            try:
                await bot.send_message(
                    owner_id,
                    f"ℹ️ Bot <b>{esc(title)}</b> kanalida admin emas — izoh qo'shish "
                    "to'xtadi. Sozlamalaringiz saqlanib qoldi: botni qayta admin "
                    "qilsangiz, hammasi tiklanadi.",
                )
            except TelegramAPIError:
                pass


# ---------- Umumiy buyruqlar ----------

@dp.message(Command("start", "help"), F.chat.type == "private")
async def cmd_start(message: Message):
    if not await require_subscription_message(message):
        return
    await message.answer(WELCOME_TEXT, reply_markup=MENU_KB)


async def _set_user_mode(message: Message, mode: str) -> None:
    if not await require_subscription_message(message):
        return
    db.set_user_mode(message.from_user.id, mode)
    await message.answer(
        f"✅ Rejim: <b>{USER_MODE_INFO[mode]}</b>\nEndi matn yuboring — o'girib beraman.",
        reply_markup=MENU_KB,
    )


# Tugmalar va buyruqlar /setcaption holatidan OLDIN ro'yxatga olinadi,
# shunda ular izoh matni sifatida qabul qilinib qolmaydi.
@dp.message(F.chat.type == "private", F.text.in_(USER_MODE_BUTTONS))
async def btn_user_mode(message: Message):
    await _set_user_mode(message, USER_MODE_BUTTONS[message.text])


@dp.message(Command("lotin_kirill"), F.chat.type == "private")
async def cmd_mode_l2c(message: Message):
    await _set_user_mode(message, "l2c")


@dp.message(Command("kirill_lotin"), F.chat.type == "private")
async def cmd_mode_c2l(message: Message):
    await _set_user_mode(message, "c2l")


@dp.message(Command("yangi_alifbo"), F.chat.type == "private")
async def cmd_mode_new(message: Message):
    await _set_user_mode(message, "new")


@dp.message(Command("avto"), F.chat.type == "private")
async def cmd_mode_auto(message: Message):
    await _set_user_mode(message, "auto")


@dp.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: Message):
    if message.from_user.id not in OPERATOR_IDS:
        return

    modes = db.count_channels_by_mode()
    modes_text = ", ".join(
        f"{TRANSLIT_MODES.get(k, (k,))[0]}: {v}" for k, v in sorted(modes.items())
    ) or "—"
    text = (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Foydalanuvchilar: {db.count_owners()}\n"
        f"📢 Ulangan kanallar: {db.count_channels()}\n"
        f"📝 Izohi borlar: {db.count_channels_with_caption()}\n"
        f"🔤 Rejimlar: {modes_text}\n"
        f"✅ Qo'shilgan izohlar: jami {db.count_captions_applied()}, "
        f"24 soatda {db.count_captions_applied(24)}"
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
        if db.get_paused(channel_id):
            has_caption = "⏸ to'xtatilgan"
        else:
            has_caption = "✅ izoh bor" if db.get_caption(channel_id) else "— izoh yo'q"
        mode_name = TRANSLIT_MODES.get(db.get_translit_mode(channel_id), ("?",))[0]
        pos = "oldin" if db.get_caption_pos(channel_id) == "before" else "keyin"
        count = db.count_captions_applied(channel_id=channel_id)
        lines.append(f"• {esc(title)} ({has_caption}, {mode_name}, izoh {pos}, {count} ta post)")
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
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    await state.set_state(SetCaption.waiting_for_text)
    await state.update_data(channel_id=channel_id)
    await callback.message.edit_text(
        f"«{esc(title)}» uchun izoh matnini yuboring — qalin/kursiv shrift, havolalar "
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
    raw_html = message.html_text or plain_text

    if not plain_text.strip():
        await message.answer("Matn topilmadi. Qayta yuboring yoki /cancel bilan bekor qiling.")
        return

    mode = db.get_translit_mode(channel_id)
    preview = transliterate(raw_html, mode)

    # Uzunlik o'girishdan KEYIN ham tekshiriladi (kirill→lotinda matn uzayadi)
    longest = max(plain_len(raw_html), plain_len(preview))
    if longest > MAX_CAPTION_LEN:
        await message.answer(
            f"Matn juda uzun ({longest} belgi, o'girilgan holda). "
            f"Maksimum {MAX_CAPTION_LEN} belgi bo'lishi kerak, post matni uchun joy ham qoldiring."
        )
        return

    # ASL matn saqlanadi; o'girish postga qo'shilayotganda joriy rejim bilan
    db.set_caption(channel_id, raw_html)
    await state.clear()
    title = db.get_channel_title(channel_id) or str(channel_id)
    await message.answer(
        f"✅ «{esc(title)}» uchun izoh saqlandi. Namuna qanday chiqishini pastda ko'rasiz 👇"
    )
    await message.answer(preview)


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
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    caption = db.get_caption(channel_id)
    await callback.answer()
    if caption:
        await callback.message.answer(f"«{esc(title)}» uchun joriy izoh:")
        await callback.message.answer(transliterate(caption, db.get_translit_mode(channel_id)))
    else:
        await callback.message.answer(f"«{esc(title)}» uchun izoh hali belgilanmagan.")


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
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    db.clear_caption(channel_id)
    await callback.answer("O'chirildi")
    await callback.message.edit_text(f"✅ «{esc(title)}» uchun avtomatik izoh o'chirildi.")


# ---------- /pause ----------

@dp.message(Command("pause"), F.chat.type == "private")
async def cmd_pause(message: Message):
    if not await require_subscription_message(message):
        return
    if not db.list_channels_for_owner(message.from_user.id):
        await _no_channels_message(message)
        return
    await message.answer(
        "Qaysi kanal uchun izohni to'xtatamiz/yoqamiz?",
        reply_markup=channels_keyboard("pause", message.from_user.id),
    )


@dp.callback_query(F.data.startswith("pause:"))
async def cb_pause(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    now_paused = not db.get_paused(channel_id)
    db.set_paused(channel_id, now_paused)
    await callback.answer("Saqlandi")
    if now_paused:
        text = f"⏸ «{esc(title)}» uchun izoh qo'shish vaqtincha to'xtatildi (izoh saqlanib turibdi). Qayta yoqish uchun yana /pause."
    else:
        text = f"▶️ «{esc(title)}» uchun izoh qo'shish qayta yoqildi."
    await callback.message.edit_text(text)


# ---------- /position ----------

@dp.message(Command("position"), F.chat.type == "private")
async def cmd_position(message: Message):
    if not await require_subscription_message(message):
        return
    if not db.list_channels_for_owner(message.from_user.id):
        await _no_channels_message(message)
        return
    await message.answer(
        "Qaysi kanal uchun izoh joyini tanlaymiz?",
        reply_markup=channels_keyboard("poschan", message.from_user.id),
    )


@dp.callback_query(F.data.startswith("poschan:"))
async def cb_choose_position_channel(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    current = db.get_caption_pos(channel_id)
    options = [("after", "Post matnidan keyin"), ("before", "Post matnidan oldin")]
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if key == current else "") + name,
            callback_data=f"posset:{channel_id}:{key}",
        )]
        for key, name in options
    ]
    await callback.message.edit_text(
        f"«{esc(title)}» uchun izoh joyini tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("posset:"))
async def cb_set_position(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    pos = callback.data.split(":")[2] if callback.data.count(":") >= 2 else ""
    if pos not in ("before", "after"):
        await callback.answer("Noto'g'ri tugma.", show_alert=True)
        return
    db.set_caption_pos(channel_id, pos)
    label = "post matnidan oldin" if pos == "before" else "post matnidan keyin"
    await callback.answer("Saqlandi")
    await callback.message.edit_text(f"✅ «{esc(title)}» uchun izoh joyi: {label}")


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
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    current = db.get_translit_mode(channel_id)
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if key == current else "") + name,
            callback_data=f"trlmode:{channel_id}:{key}",
        )]
        for key, (name, _fn) in TRANSLIT_MODES.items()
    ]
    await callback.message.edit_text(
        f"«{esc(title)}» uchun o'girish rejimini tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("trlmode:"))
async def cb_set_translit_mode(callback: CallbackQuery):
    if not await require_subscription_callback(callback):
        return
    owned = await _owned_channel(callback)
    if not owned:
        return
    channel_id, title = owned
    mode = callback.data.split(":", 2)[2] if callback.data.count(":") >= 2 else ""
    if mode not in TRANSLIT_MODES:
        await callback.answer("Bu tugma eskirgan, /translit ni qayta bosing.", show_alert=True)
        return
    db.set_translit_mode(channel_id, mode)
    await callback.answer("Saqlandi")
    await callback.message.edit_text(
        f"✅ «{esc(title)}» uchun o'girish rejimi: {TRANSLIT_MODES[mode][0]}\n"
        "Saqlangan izoh ham yangi rejim bo'yicha qo'shiladi."
    )


# ---------- Botga to'g'ridan-to'g'ri yozilgan matnni o'girish ----------

_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_LETTER_RE = re.compile(r"[Ѐ-ӿ]")


def _split_text(text: str, size: int = 3800) -> list[str]:
    """Uzun matnni qator chegarasidan bo'laklarga ajratadi."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:size])
            line = line[size:]
        if len(current) + len(line) + 1 > size and current:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [""]


@dp.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def echo_translit(message: Message):
    """Botga yozilgan istalgan matnni yo'nalishini avtomatik aniqlab o'giradi."""
    if not await require_subscription_message(message):
        return

    raw_text = message.text or ""
    latin_count = len(_LATIN_LETTER_RE.findall(raw_text))
    cyrillic_count = len(_CYRILLIC_LETTER_RE.findall(raw_text))
    if latin_count == 0 and cyrillic_count == 0:
        return

    mode = db.get_user_mode(message.from_user.id)
    if mode == "auto":
        mode = "l2c" if latin_count >= cyrillic_count else "c2l"

    source = message.html_text or raw_text
    result = transliterate(source, mode)
    if result == source:
        await message.answer(
            "Bu matnda o'zgartiriladigan harf topilmadi (tanlangan rejim: "
            f"<b>{USER_MODE_INFO.get(mode, mode)}</b>). Boshqa rejim uchun pastdagi tugmalardan foydalaning.",
            reply_markup=MENU_KB,
        )
        return
    for chunk in _split_text(result):
        try:
            await message.answer(chunk)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            await message.answer(chunk)
        except TelegramBadRequest:
            # Bo'lak teg o'rtasidan bo'linib qolgan bo'lishi mumkin — oddiy matn
            await message.answer(chunk, parse_mode=None)


# ---------- Kanal postlariga avtomatik izoh qo'shish ----------

async def _edit_caption(chat_id: int, message_id: int, caption: str) -> "str | None":
    """None — muvaffaqiyat, aks holda xato matni. Flood limitda kutib qayta urinadi.
    Hech qachon 'zaxira variant' sifatida postning asl matnini o'chirmaydi."""
    last_error = None
    for _ in range(3):
        try:
            await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption)
            return None
        except TelegramRetryAfter as e:
            logging.warning("Flood limit: %s soniya kutamiz (chat_id=%s)", e.retry_after, chat_id)
            await asyncio.sleep(e.retry_after + 1)
            last_error = str(e)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return None
            return str(e)
        except TelegramAPIError as e:
            return str(e)
    return last_error


async def _edit_text(chat_id: int, message_id: int, text: str, link_preview_options=None) -> "str | None":
    """Faqat matnli kanal postini tahrirlaydi (None — muvaffaqiyat)."""
    last_error = None
    for _ in range(3):
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id,
                link_preview_options=link_preview_options,
            )
            return None
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            last_error = str(e)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return None
            return str(e)
        except TelegramAPIError as e:
            return str(e)
    return last_error


async def apply_text_post(message: Message) -> None:
    """Faqat 'Yangi alifbo' rejimida: matnli postdagi eski harflar (lotin/kirill)
    yangi alifboga o'zgartiriladi. Boshqa rejimlarda matnli postlarga tegilmaydi."""
    chat_id = message.chat.id
    if db.get_paused(chat_id) or not db.is_active(chat_id):
        return
    if db.get_translit_mode(chat_id) != "new":
        return

    base = message.html_text or ""
    source = SKIP_TAG_RE.sub("", base).strip() if SKIP_TAG_RE.search(base) else base
    new_text = transliterate(source, "new")
    if new_text == base or not new_text.strip():
        return
    if plain_len(new_text) > MAX_MESSAGE_LEN:
        return

    err = await _edit_text(
        chat_id, message.message_id, new_text,
        link_preview_options=getattr(message, "link_preview_options", None),
    )
    if err is None:
        db.log_caption_applied(chat_id)
        return
    logging.error("Matnli postni o'zgartirib bo'lmadi: chat_id=%s, xato=%s", chat_id, err)
    await notify_owner(
        chat_id,
        f"⚠️ «{esc(db.get_channel_title(chat_id) or chat_id)}» kanalidagi postni yangi alifboga "
        f"o'zgartirib bo'lmadi.\nSabab: <code>{esc(err)}</code>\n"
        "Botda <b>\"Xabarlarni tahrirlash\" (Edit Messages)</b> huquqi yoqilganini tekshiring.",
    )


@dp.channel_post(F.text)
async def handle_channel_text(message: Message):
    """Matnli kanal postlari (faqat Yangi alifbo rejimida o'zgartiriladi)."""
    await apply_text_post(message)


async def apply_caption(chat_id: int, message_id: int, base_html: str, caption_raw: str) -> None:
    if db.get_paused(chat_id) or not db.is_active(chat_id):
        return

    mode = db.get_translit_mode(chat_id)

    # #izohsiz — izoh qo'shilmaydi, faqat belgining o'zi o'chiriladi
    if SKIP_TAG_RE.search(base_html):
        stripped = SKIP_TAG_RE.sub("", base_html).strip()
        new_caption = transliterate(stripped, mode).strip()
        err = await _edit_caption(chat_id, message_id, new_caption)
        if err:
            logging.error("#izohsiz belgisini o'chirib bo'lmadi: chat_id=%s, xato=%s", chat_id, err)
        return

    old = transliterate(base_html, mode).strip()
    cap = transliterate(caption_raw, mode).strip()
    if not cap:
        return
    if cap in old:
        return  # izoh allaqachon bor — ikki marta qo'shmaymiz

    if db.get_caption_pos(chat_id) == "before":
        new_caption = f"{cap}\n\n{old}" if old else cap
    else:
        new_caption = f"{old}\n\n{cap}" if old else cap

    if plain_len(new_caption) > MAX_CAPTION_LEN:
        logging.error(
            "Caption uzun (%s belgi): chat_id=%s, message_id=%s — post o'zgarishsiz qoldi.",
            plain_len(new_caption), chat_id, message_id,
        )
        await notify_owner(
            chat_id,
            f"⚠️ «{esc(db.get_channel_title(chat_id) or chat_id)}» kanalidagi postga izoh "
            f"qo'shilmadi: post matni + izoh {plain_len(new_caption)} belgi bo'ldi "
            f"(limit {MAX_CAPTION_LEN}). Izohni qisqartiring: /setcaption.",
        )
        return

    err = await _edit_caption(chat_id, message_id, new_caption)
    if err is None:
        logging.info("Caption yangilandi: chat_id=%s, message_id=%s", chat_id, message_id)
        db.log_caption_applied(chat_id)
        return

    logging.error(
        "Caption tahrirlashda xatolik: chat_id=%s, message_id=%s, xato=%s",
        chat_id, message_id, err,
    )
    await notify_owner(
        chat_id,
        f"⚠️ «{esc(db.get_channel_title(chat_id) or chat_id)}» kanalidagi postga izoh "
        f"qo'shib bo'lmadi.\nSabab: <code>{esc(err)}</code>\n"
        "Botda <b>\"Xabarlarni tahrirlash\" (Edit Messages)</b> huquqi yoqilganini tekshiring.",
    )


@dp.channel_post(~F.media_group_id, MEDIA_FILTER)
async def handle_channel_post(message: Message):
    """Yakka (albom bo'lmagan) media postlari."""
    caption_text = db.get_caption(message.chat.id)
    if not caption_text:
        return
    await apply_caption(message.chat.id, message.message_id, message.html_text or "", caption_text)


@dp.channel_post(F.media_group_id, MEDIA_FILTER)
async def handle_album_item(message: Message):
    """Albom (bir vaqtda yuborilgan bir nechta media) postlari."""
    group_id = message.media_group_id

    # Eski yozuvlarni tozalaymiz
    now = time.monotonic()
    for gid in [g for g, t in album_done.items() if now - t > 120]:
        album_done.pop(gid, None)

    # Albom allaqachon qayta ishlangan (yoki ishlanmoqda) — kech kelgan
    # element ishlayotgan tahrirni bekor qilmasin
    if group_id in album_done:
        return

    album_messages.setdefault(group_id, []).append(message)
    if group_id in album_tasks:
        album_tasks[group_id].cancel()
    album_tasks[group_id] = asyncio.create_task(process_album(group_id))


async def process_album(group_id: str) -> None:
    try:
        await asyncio.sleep(ALBUM_DEBOUNCE)
    except asyncio.CancelledError:
        return
    album_done[group_id] = time.monotonic()
    messages = album_messages.pop(group_id, [])
    album_tasks.pop(group_id, None)
    if not messages:
        return

    chat_id = messages[0].chat.id
    caption_text = db.get_caption(chat_id)
    if not caption_text:
        return

    # Telegram butun albom uchun faqat BITTA izohni ko'rsatadi — asl izohi
    # bor elementni tanlaymiz, bo'lmasa birinchisini
    messages.sort(key=lambda m: m.message_id)
    target = next((m for m in messages if (m.html_text or "").strip()), messages[0])
    await apply_caption(chat_id, target.message_id, target.html_text or "", caption_text)


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

    if OPERATOR_IDS:
        db.assign_default_owner(OPERATOR_IDS[0])

    # drop_pending_updates=False: bot o'chiq turganda tashlangan postlar
    # qayta ishga tushganda izohsiz qolib ketmasin
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
