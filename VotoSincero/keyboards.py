from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder


# Reply кнопки (как на твоём скрине)
BTN_MY_PROFILE = "👤 Il mio profilo"
BTN_RATE = "🔥 Valuta"
BTN_WHO_RATED = "📩 Chi mi ha valutato"
BTN_LEADERBOARD = "🏆 Classifica"

BTN_BACK = "⬅️ Indietro"

BTN_EDIT_PHOTO = "🖼️ Cambia foto"
BTN_EDIT_GENDER = "🚻 Cambia genere"
BTN_EDIT_AGE = "🎂 Cambia età"
BTN_EDIT_CITY = "📍 Cambia città"
BTN_EDIT_BIO = "📝 Cambia bio"
BTN_EDIT_BE_RATED_BY = "✅ Chi mi valuta"
BTN_EDIT_RATE_PREF = "🔥 Chi valuto"

BTN_SKIP_BIO = "⏭️ Salta la bio"

BTN_GENDER_MALE = "👨 Uomo"
BTN_GENDER_FEMALE = "👩 Donna"

BTN_PREF_MALE = "👨 Uomini"
BTN_PREF_FEMALE = "👩 Donne"
BTN_PREF_BOTH = "👥 Entrambi"

BTN_RATE_MSG = "💬 Messaggio"
BTN_RATE_REPORT = "🚨 Segnala"


def main_menu_kb(unread: int = 0) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()

    who_btn = BTN_WHO_RATED
    if unread > 0:
        who_btn += f" ({unread})"

    kb.row(
        KeyboardButton(text=BTN_MY_PROFILE),
        KeyboardButton(text=BTN_RATE),
        KeyboardButton(text=who_btn),
    )
    kb.row(KeyboardButton(text=BTN_LEADERBOARD))
    return kb.as_markup(resize_keyboard=True)


def profile_menu_kb(unread: int = 0) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(
        KeyboardButton(text=BTN_EDIT_PHOTO),
        KeyboardButton(text=BTN_EDIT_GENDER),
        KeyboardButton(text=BTN_EDIT_AGE),
    )
    kb.row(
        KeyboardButton(text=BTN_EDIT_CITY),
        KeyboardButton(text=BTN_EDIT_BIO),
        KeyboardButton(text=BTN_EDIT_BE_RATED_BY),
    )
    kb.row(
        KeyboardButton(text=BTN_EDIT_RATE_PREF),
        KeyboardButton(text=BTN_BACK),
    )
    return kb.as_markup(resize_keyboard=True)


def skip_bio_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text=BTN_SKIP_BIO))
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def gender_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(
        KeyboardButton(text=BTN_GENDER_MALE),
        KeyboardButton(text=BTN_GENDER_FEMALE),
    )
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def pref_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(
        KeyboardButton(text=BTN_PREF_MALE),
        KeyboardButton(text=BTN_PREF_FEMALE),
        KeyboardButton(text=BTN_PREF_BOTH),
    )
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def rating_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.row(*[KeyboardButton(text=str(i)) for i in range(1, 6)])
    kb.row(*[KeyboardButton(text=str(i)) for i in range(6, 11)])
    kb.row(
        KeyboardButton(text=BTN_BACK),
        KeyboardButton(text=BTN_RATE_MSG),
        KeyboardButton(text=BTN_RATE_REPORT),
    )
    return kb.as_markup(resize_keyboard=True)


# Inline клавиатура только для Classifica (чтобы было ОДНО сообщение)
def leaderboard_inline_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="1️⃣", callback_data="lb:1")
    kb.button(text="2️⃣", callback_data="lb:2")
    kb.button(text="3️⃣", callback_data="lb:3")
    kb.adjust(3)
    return kb.as_markup()


# Inline для админ-жалоб
def admin_report_kb(report_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Blocca", callback_data=f"rep:block:{report_id}")
    kb.button(text="✅ Chiudi", callback_data=f"rep:close:{report_id}")
    kb.adjust(2)
    return kb.as_markup()
