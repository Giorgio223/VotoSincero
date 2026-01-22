import asyncio
import os
import re
import aiohttp

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv

from db import (
    create_engine, create_sessionmaker, init_db,
    get_user_by_tg_id, create_user, update_user_fields,
    bump_photo_version_and_update_photo,
    get_next_candidate, save_rating,
    get_ratings_for_me_and_mark_seen,
    get_my_rating_stats,
    get_unseen_count,
    get_top3, get_my_rank,
    list_required_channels, add_required_channel, remove_required_channel,
    create_report, get_report, list_open_reports, close_report, block_user, unblock_user,
)
from states import Reg, EditProfile, RateFlow
from keyboards import (
    main_menu_kb, profile_menu_kb, skip_bio_kb,
    gender_kb, pref_kb, rating_kb,
    leaderboard_inline_kb, admin_report_kb,
    BTN_MY_PROFILE, BTN_RATE, BTN_WHO_RATED, BTN_LEADERBOARD,
    BTN_BACK,
    BTN_EDIT_PHOTO, BTN_EDIT_GENDER, BTN_EDIT_AGE, BTN_EDIT_CITY, BTN_EDIT_BIO,
    BTN_EDIT_BE_RATED_BY, BTN_EDIT_RATE_PREF,
    BTN_SKIP_BIO,
    BTN_GENDER_MALE, BTN_GENDER_FEMALE,
    BTN_PREF_MALE, BTN_PREF_FEMALE, BTN_PREF_BOTH,
    BTN_RATE_MSG, BTN_RATE_REPORT,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Prod (Render/any VPS): set DB_URL (PostgreSQL, e.g. postgresql+asyncpg://...)
# Local (Windows/dev): set DB_URL_LOCAL (SQLite, e.g. sqlite+aiosqlite:///votosincero.db)
DB_URL = os.getenv("DB_URL") or os.getenv("DB_URL_LOCAL") or "sqlite+aiosqlite:///votosincero.db"

ADMIN_IDS = set()
raw_admins = os.getenv("ADMIN_IDS", "").strip()
if raw_admins:
    for x in raw_admins.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

engine = create_engine(DB_URL)
SessionLocal = create_sessionmaker(engine)
dp.update.middleware(BanGuardMiddleware(SessionLocal))

BANNED_TEXT = "Sei stato bannato, per essere sbannato scrivi a @gioorgioo"


class BlockMiddleware(BaseMiddleware):
    """Если юзер заблокирован — отвечает текстом и не пускает дальше."""

    async def __call__(self, handler, event, data):
        tg_id = None
        if hasattr(event, "from_user") and event.from_user:
            tg_id = event.from_user.id

        if not tg_id:
            return await handler(event, data)

        async with SessionLocal() as session:
            u = await get_user_by_tg_id(session, tg_id)
            if u and getattr(u, "blocked", False):
                if isinstance(event, CallbackQuery):
                    # уберём "часики"
                    try:
                        await event.answer()
                    except Exception:
                        pass
                    if event.message:
                        await event.message.answer(BANNED_TEXT)
                elif isinstance(event, Message):
                    await event.answer(BANNED_TEXT)
                return

        return await handler(event, data)


# Подключаем middleware на любые сообщения и callback-и


# ---------- Helpers ----------
async def city_exists(city: str) -> bool:
    city = city.strip()
    if len(city) < 2:
        return False
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": city, "format": "json", "limit": 1}
    headers = {"User-Agent": "votosincero-bot/1.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=8) as r:
                if r.status != 200:
                    return False
                data = await r.json()
                return bool(data)
    except Exception:
        return False

def gender_it(g: str) -> str:
    return "👨 Uomo" if g == "male" else "👩 Donna"

def pref_it(p: str) -> str:
    if p == "male":
        return "👨 Uomini"
    if p == "female":
        return "👩 Donne"
    return "👥 Entrambi"

def parse_gender_button(text: str) -> str | None:
    if text == BTN_GENDER_MALE:
        return "male"
    if text == BTN_GENDER_FEMALE:
        return "female"
    return None

def parse_pref_button(text: str) -> str | None:
    if text == BTN_PREF_MALE:
        return "male"
    if text == BTN_PREF_FEMALE:
        return "female"
    if text == BTN_PREF_BOTH:
        return "both"
    return None

def is_valid_name_or_username(s: str) -> bool:
    s = s.strip()
    if not (2 <= len(s) <= 32):
        return False
    if s.startswith("@"):
        return bool(re.fullmatch(r"@[A-Za-z0-9_]{5,32}", s))
    return True

async def show_profile(message: Message, user, unread: int, with_profile_kb: bool):
    async with SessionLocal() as session:
        avg, cnt = await get_my_rating_stats(session, user)

    rating_line = "⭐ Valutazione: —"
    if avg is not None:
        rating_line = f"⭐ Valutazione: {avg:.2f}/10  •  📊 Voti: {cnt}"

    caption = (
        f"👤 *Il tuo profilo*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🪪 Nome: {user.name}\n"
        f"🎂 Età: {user.age}\n"
        f"📍 Città: {user.city}\n"
        f"🚻 Genere: {gender_it(user.gender)}\n"
        f"🔥 *Valuto:* {pref_it(user.rate_pref)}\n"
        f"✅ *Mi valutano:* {pref_it(user.be_rated_by)}\n"
        f"{rating_line}\n"
        f"📝 Bio: {user.bio or '—'}\n"
    )

    kb = profile_menu_kb(unread) if with_profile_kb else main_menu_kb(unread)
    await message.answer_photo(
        photo=user.photo_file_id,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=kb
    )

async def send_candidate(message: Message, target_user, note: str | None = None):
    caption = (
        f"🔥 *Profilo da valutare*\n"
        f"━━━━━━━━━━━━━━\n"
        f"🪪 Nome: {target_user.name}\n"
        f"🎂 Età: {target_user.age}\n"
        f"📍 Città: {target_user.city}\n"
        f"🚻 Genere: {gender_it(target_user.gender)}\n"
        f"📝 Bio: {target_user.bio or '—'}\n"
    )
    if note:
        caption += f"\n✅ {note}"

    await message.answer_photo(
        photo=target_user.photo_file_id,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=rating_kb(),
    )

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

class BanGuardMiddleware(BaseMiddleware):
    """Блокирует забаненных пользователей на любом апдейте."""

    def __init__(self, sessionmaker):
        super().__init__()
        self._SessionLocal = sessionmaker
        self._cache = {}  # tg_id -> (is_blocked, expires_ts)

    async def __call__(self, handler, event, data):
        from aiogram.types import Message, CallbackQuery
        user = data.get("event_from_user")
        if user is None:
            user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        tg_id = int(user.id)

        # Админ всегда может /unban
        if isinstance(event, Message) and event.text and event.text.strip().lower().startswith("/unban") and is_admin(tg_id):
            return await handler(event, data)

        now = asyncio.get_event_loop().time()
        cached = self._cache.get(tg_id)
        if cached and cached[1] > now:
            if cached[0]:
                if isinstance(event, Message):
                    await event.answer(BANNED_TEXT)
                elif isinstance(event, CallbackQuery):
                    await event.answer(BANNED_TEXT, show_alert=True)
                return
            return await handler(event, data)

        async with self._SessionLocal() as session:
            u = await get_user_by_tg_id(session, tg_id)
            is_blocked = bool(u.blocked) if u else False

        # кеш на 30 секунд
        self._cache[tg_id] = (is_blocked, now + 30.0)

        if is_blocked:
            if isinstance(event, Message):
                await event.answer(BANNED_TEXT)
            elif isinstance(event, CallbackQuery):
                await event.answer(BANNED_TEXT, show_alert=True)
            return

        return await handler(event, data)

async def check_required_subscriptions(user_id: int) -> tuple[bool, list[tuple[str, str | None]]]:
    """
    returns (ok, missing_list)
    missing_list item: (title_or_username, link)
    """
    async with SessionLocal() as session:
        channels = await list_required_channels(session)

    missing = []
    for ch in channels:
        # username может быть "@channel" или "-100123..."
        chat_id_or_username = ch.username
        try:
            member = await bot.get_chat_member(chat_id_or_username, user_id)
            # statuses: "member", "administrator", "creator", "restricted", "left", "kicked"
            if member.status in ("left", "kicked"):
                missing.append((ch.title or ch.username, ch.link))
        except Exception:
            # если бот не имеет доступа к каналу или username неправильный
            missing.append((ch.title or ch.username, ch.link))

    return (len(missing) == 0), missing

async def send_subscribe_required(message: Message, missing: list[tuple[str, str | None]]):
    lines = ["🔒 Per usare il bot devi iscriverti ai canali obbligatori:\n"]
    for title, link in missing:
        if link:
            lines.append(f"• {title} → {link}")
        else:
            lines.append(f"• {title}")
    lines.append("\n✅ Dopo l’iscrizione riprova a premere *🔥 Valuta*.")
    await message.answer("\n".join(lines), parse_mode="Markdown")


# ---------- /start ----------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, message.from_user.id)
        if not user:
            await state.clear()
            await state.set_state(Reg.name)
            await message.answer("👋 Ciao! Scrivi il tuo nome oppure il tuo username seguito da @ (esempio @username)")
            return

        if user.blocked:
            await message.answer("🚫 Il tuo account è stato bloccato.")
            return

        unread = await get_unseen_count(session, user)

    await state.clear()
    await show_profile(message, user, unread, with_profile_kb=False)


# ---------- Registration ----------
@dp.message(Reg.name)
async def reg_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not is_valid_name_or_username(name):
        await message.answer("⚠️ Inserisci un nome valido oppure un username tipo *@username*.")
        return
    await state.update_data(name=name)
    await state.set_state(Reg.age)
    await message.answer("🎂 Quanti anni hai? *(9–100)*", parse_mode="Markdown")

@dp.message(Reg.age)
async def reg_age(message: Message, state: FSMContext):
    try:
        age = int((message.text or "").strip())
    except Exception:
        await message.answer("⚠️ L’età deve essere un numero. Esempio: *19*", parse_mode="Markdown")
        return
    if not (9 <= age <= 100):
        await message.answer("⚠️ L’età deve essere tra *9* e *100*. Riprova.", parse_mode="Markdown")
        return

    await state.update_data(age=age)
    await state.set_state(Reg.city)
    await message.answer("📍 Da dove vieni? Scrivi la *città* (controllerò se esiste).", parse_mode="Markdown")

@dp.message(Reg.city)
async def reg_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not await city_exists(city):
        await message.answer("❌ Non trovo questa città. Riprova senza errori di scrittura.")
        return

    await state.update_data(city=city)
    await state.set_state(Reg.bio)
    await message.answer(
        "📝 Scrivi una bio (esempio: Instagram: ...)\noppure premi *⏭️ Salta la bio*.",
        parse_mode="Markdown",
        reply_markup=skip_bio_kb()
    )

@dp.message(Reg.bio)
async def reg_bio(message: Message, state: FSMContext):
    if message.text == BTN_SKIP_BIO:
        await state.update_data(bio=None)
    else:
        bio = (message.text or "").strip()
        if len(bio) > 200:
            await message.answer("⚠️ Troppo lunga (max 200). Riprova o premi *⏭️ Salta la bio*.", parse_mode="Markdown")
            return
        await state.update_data(bio=bio)

    await state.set_state(Reg.photo)
    await message.answer("🖼️ Ora invia una tua *foto* (solo foto, niente testo).", parse_mode="Markdown", reply_markup=None)

@dp.message(Reg.photo)
async def reg_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("⚠️ Devi inviare *una foto* 🙂 Riprova.", parse_mode="Markdown")
        return

    photo_file_id = message.photo[-1].file_id
    await state.update_data(photo_file_id=photo_file_id)

    await state.set_state(Reg.gender)
    await message.answer("🚻 Seleziona il tuo genere:", reply_markup=gender_kb())

@dp.message(Reg.gender)
async def reg_gender(message: Message, state: FSMContext):
    gender = parse_gender_button(message.text or "")
    if not gender:
        await message.answer("⚠️ Scegli usando i pulsanti qui sotto 👇", reply_markup=gender_kb())
        return

    await state.update_data(gender=gender)
    await state.set_state(Reg.rate_pref)
    await message.answer("🔥 Chi vuoi valutare?", reply_markup=pref_kb())

@dp.message(Reg.rate_pref)
async def reg_rate_pref(message: Message, state: FSMContext):
    rate_pref = parse_pref_button(message.text or "")
    if not rate_pref:
        await message.answer("⚠️ Scegli usando i pulsanti 👇", reply_markup=pref_kb())
        return

    await state.update_data(rate_pref=rate_pref)
    await state.set_state(Reg.be_rated_by)
    await message.answer("✅ Chi vuoi che ti valuti?", reply_markup=pref_kb())

@dp.message(Reg.be_rated_by)
async def reg_be_rated_by(message: Message, state: FSMContext):
    be_rated_by = parse_pref_button(message.text or "")
    if not be_rated_by:
        await message.answer("⚠️ Scegli usando i pulsanti 👇", reply_markup=pref_kb())
        return

    data = await state.get_data()
    async with SessionLocal() as session:
        await create_user(
            session,
            tg_id=message.from_user.id,
            name=data["name"],
            age=data["age"],
            city=data["city"],
            bio=data.get("bio"),
            photo_file_id=data["photo_file_id"],
            gender=data["gender"],
            rate_pref=data["rate_pref"],
            be_rated_by=be_rated_by,
            photo_version=1,
        )

    await state.clear()
    await message.answer("✅ Registrazione completata!\n👉 Premi /start per vedere il menu.")


# ---------- Menu ----------
@dp.message(F.text == BTN_MY_PROFILE)
async def my_profile(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, message.from_user.id)
        if not user:
            await message.answer("⚠️ Non sei registrato. Premi /start.")
            return
        if user.blocked:
            await message.answer("🚫 Il tuo account è stato bloccato.")
            return
        unread = await get_unseen_count(session, user)

    await show_profile(message, user, unread, with_profile_kb=True)

@dp.message(F.text == BTN_BACK)
async def back_to_main(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as session:
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0
    await message.answer("🏠 Menu principale:", reply_markup=main_menu_kb(unread))


# ---------- Edit profile ----------
@dp.message(F.text == BTN_EDIT_PHOTO)
async def edit_photo_start(message: Message, state: FSMContext):
    await state.set_state(EditProfile.photo)
    await message.answer(
        "🖼️ Invia una nuova foto.\n\n"
        "⚠️ *Attenzione:* cambiando foto, la tua valutazione (⭐) verrà *azzerata*.",
        parse_mode="Markdown"
    )

@dp.message(EditProfile.photo)
async def edit_photo_save(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("⚠️ Solo foto 🙂 Riprova.")
        return

    new_photo_id = message.photo[-1].file_id
    async with SessionLocal() as session:
        await bump_photo_version_and_update_photo(session, message.from_user.id, new_photo_id)
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0

    await state.clear()
    await message.answer("✅ Foto aggiornata! ⭐ Valutazione azzerata.", reply_markup=profile_menu_kb(unread))

@dp.message(F.text == BTN_EDIT_GENDER)
async def edit_gender_start(message: Message, state: FSMContext):
    await state.set_state(EditProfile.gender)
    await message.answer("🚻 Seleziona il tuo genere:", reply_markup=gender_kb())

@dp.message(EditProfile.gender)
async def edit_gender_save(message: Message, state: FSMContext):
    gender = parse_gender_button(message.text or "")
    if not gender:
        await message.answer("⚠️ Scegli usando i pulsanti 👇", reply_markup=gender_kb())
        return

    async with SessionLocal() as session:
        await update_user_fields(session, message.from_user.id, gender=gender)
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0

    await state.clear()
    await message.answer("✅ Genere aggiornato!", reply_markup=profile_menu_kb(unread))

@dp.message(F.text == BTN_EDIT_AGE)
async def edit_age_start(message: Message, state: FSMContext):
    await state.set_state(EditProfile.age)
    await message.answer("🎂 Scrivi la tua nuova età *(9–100)*:", parse_mode="Markdown")

@dp.message(EditProfile.age)
async def edit_age_save(message: Message, state: FSMContext):
    try:
        age = int((message.text or "").strip())
    except Exception:
        await message.answer("⚠️ L’età deve essere un numero. Riprova.")
        return

    if not (9 <= age <= 100):
        await message.answer("⚠️ L’età deve essere tra 9 e 100. Riprova.")
        return

    async with SessionLocal() as session:
        await update_user_fields(session, message.from_user.id, age=age)
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0

    await state.clear()
    await message.answer("✅ Età aggiornata!", reply_markup=profile_menu_kb(unread))

@dp.message(F.text == BTN_EDIT_CITY)
async def edit_city_start(message: Message, state: FSMContext):
    await state.set_state(EditProfile.city)
    await message.answer("📍 Scrivi la nuova città (controllerò se esiste).")

@dp.message(EditProfile.city)
async def edit_city_save(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not await city_exists(city):
        await message.answer("❌ Non trovo questa città. Riprova senza errori di scrittura.")
        return

    async with SessionLocal() as session:
        await update_user_fields(session, message.from_user.id, city=city)
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0

    await state.clear()
    await message.answer("✅ Città aggiornata!", reply_markup=profile_menu_kb(unread))

@dp.message(F.text == BTN_EDIT_BIO)
async def edit_bio_start(message: Message, state: FSMContext):
    await state.set_state(EditProfile.bio)
    await message.answer(
        "📝 Scrivi la nuova bio.\nOppure premi ⏭️ per rimuoverla.",
        reply_markup=skip_bio_kb()
    )

@dp.message(EditProfile.bio)
async def edit_bio_save(message: Message, state: FSMContext):
    if message.text == BTN_SKIP_BIO:
        bio = None
    else:
        bio = (message.text or "").strip()
        if len(bio) > 200:
            await message.answer("⚠️ Troppo lunga (max 200). Riprova.")
            return

    async with SessionLocal() as session:
        await update_user_fields(session, message.from_user.id, bio=bio)
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0

    await state.clear()
    await message.answer("✅ Bio aggiornata!", reply_markup=profile_menu_kb(unread))

@dp.message(F.text == BTN_EDIT_BE_RATED_BY)
async def edit_berated_start(message: Message, state: FSMContext):
    await state.set_state(EditProfile.be_rated_by)
    await message.answer("✅ Chi vuoi che ti valuti?", reply_markup=pref_kb())

@dp.message(EditProfile.be_rated_by)
async def edit_berated_save(message: Message, state: FSMContext):
    be_rated_by = parse_pref_button(message.text or "")
    if not be_rated_by:
        await message.answer("⚠️ Scegli usando i pulsanti 👇", reply_markup=pref_kb())
        return

    async with SessionLocal() as session:
        await update_user_fields(session, message.from_user.id, be_rated_by=be_rated_by)
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0

    await state.clear()
    await message.answer("✅ Preferenza aggiornata!", reply_markup=profile_menu_kb(unread))

@dp.message(F.text == BTN_EDIT_RATE_PREF)
async def edit_ratepref_start(message: Message, state: FSMContext):
    await state.set_state(EditProfile.rate_pref)
    await message.answer("🔥 Chi vuoi valutare?", reply_markup=pref_kb())

@dp.message(EditProfile.rate_pref)
async def edit_ratepref_save(message: Message, state: FSMContext):
    rate_pref = parse_pref_button(message.text or "")
    if not rate_pref:
        await message.answer("⚠️ Scegli usando i pulsanti 👇", reply_markup=pref_kb())
        return

    async with SessionLocal() as session:
        await update_user_fields(session, message.from_user.id, rate_pref=rate_pref)
        user = await get_user_by_tg_id(session, message.from_user.id)
        unread = await get_unseen_count(session, user) if user else 0

    await state.clear()
    await message.answer("✅ Preferenza aggiornata!", reply_markup=profile_menu_kb(unread))


# ---------- Valuta + required channels ----------
@dp.message(F.text == BTN_RATE)
async def rate_start(message: Message, state: FSMContext):
    await state.clear()

    async with SessionLocal() as session:
        viewer = await get_user_by_tg_id(session, message.from_user.id)
        if not viewer:
            await message.answer("⚠️ Non sei registrato. Premi /start.")
            return
        if viewer.blocked:
            await message.answer("🚫 Il tuo account è stato bloccato.")
            return

    ok, missing = await check_required_subscriptions(message.from_user.id)
    if not ok:
        await send_subscribe_required(message, missing)
        return

    async with SessionLocal() as session:
        viewer = await get_user_by_tg_id(session, message.from_user.id)
        candidate = await get_next_candidate(session, viewer)
        unread = await get_unseen_count(session, viewer)

    if not candidate:
        await message.answer("✅ Nessun profilo compatibile da valutare per ora.", reply_markup=main_menu_kb(unread))
        return

    await state.set_state(RateFlow.rating)
    await state.update_data(target_id=candidate.tg_id, pending_message=None)
    await send_candidate(message, candidate)

@dp.message(RateFlow.rating)
async def rate_rating_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    target_id = data.get("target_id")

    if not target_id:
        await state.clear()
        await message.answer("⚠️ Errore: nessun profilo attivo. Premi 🔥 Valuta.")
        return

    if text == BTN_BACK:
        await state.clear()
        async with SessionLocal() as session:
            user = await get_user_by_tg_id(session, message.from_user.id)
            unread = await get_unseen_count(session, user) if user else 0
        await message.answer("🏠 Menu principale:", reply_markup=main_menu_kb(unread))
        return

    if text == BTN_RATE_MSG:
        await state.set_state(RateFlow.message)
        await message.answer("💬 Scrivi il messaggio (poi darai un voto 1–10).", reply_markup=None)
        return

    if text == BTN_RATE_REPORT:
        async with SessionLocal() as session:
            reporter = await get_user_by_tg_id(session, message.from_user.id)
            reported = await get_user_by_tg_id(session, int(target_id))
            if not reporter or not reported:
                await message.answer("❌ Errore: profilo non trovato.")
                return
            rep = await create_report(session, reporter.tg_id, reported)

        await message.answer("🚨 Segnalazione inviata ✅ Grazie!", reply_markup=rating_kb())

        # уведомим админов
        for admin_id in ADMIN_IDS:
            try:
                cap = (
                    f"🚨 *NUOVA SEGNALAZIONE* (ID: {rep.id})\n"
                    f"👤 Segnalato: {reported.name} ({reported.tg_id})\n"
                    f"📍 {reported.city} • 🎂 {reported.age} • {gender_it(reported.gender)}\n"
                    f"📝 Bio: {reported.bio or '—'}\n"
                    f"📸 PhotoVersion: {reported.photo_version}\n"
                    f"🙋 Reporter: {reporter.tg_id}"
                )
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=reported.photo_file_id,
                    caption=cap,
                    parse_mode="Markdown",
                    reply_markup=admin_report_kb(rep.id),
                )
            except Exception:
                pass

        return

    if text.isdigit():
        score = int(text)
        if not (1 <= score <= 10):
            await message.answer("⚠️ Scegli un numero da 1 a 10.", reply_markup=rating_kb())
            return

        pending_message = data.get("pending_message")

        async with SessionLocal() as session:
            viewer = await get_user_by_tg_id(session, message.from_user.id)
            target = await get_user_by_tg_id(session, int(target_id))
            if not viewer or not target:
                await state.clear()
                await message.answer("❌ Errore: profilo non trovato.")
                return
            if viewer.blocked:
                await state.clear()
                await message.answer("🚫 Il tuo account è stato bloccato.")
                return

            saved = await save_rating(session, viewer.tg_id, target, score, pending_message)
            if saved is None:
                await state.clear()
                unread = await get_unseen_count(session, viewer)
                await message.answer("⚠️ Hai già valutato questo profilo e la tua valutazione non è ancora stata vista. Riprova più tardi.", reply_markup=main_menu_kb(unread))
                return

            next_one = await get_next_candidate(session, viewer)
            unread = await get_unseen_count(session, viewer)

        if not next_one:
            await state.clear()
            await message.answer("✅ Voto inviato!\nNon ci sono altri profili per ora.", reply_markup=main_menu_kb(unread))
            return

        await state.set_state(RateFlow.rating)
        await state.update_data(target_id=next_one.tg_id, pending_message=None)
        await message.answer("✅ Voto inviato! ➡️ Prossimo profilo:")
        await send_candidate(message, next_one)
        return

    await message.answer("⚠️ Usa i pulsanti 👇", reply_markup=rating_kb())

@dp.message(RateFlow.message)
async def rate_message_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Messaggio vuoto. Riprova.")
        return
    if len(text) > 500:
        await message.answer("⚠️ Troppo lungo (max 500). Riprova.")
        return

    await state.update_data(pending_message=text)
    await state.set_state(RateFlow.rating)
    await message.answer("✅ Messaggio salvato. Ora scegli un voto 1–10 👇", reply_markup=rating_kb())


# ---------- Chi mi ha valutato ----------
@dp.message(F.text.startswith(BTN_WHO_RATED))
async def who_rated_me(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as session:
        me = await get_user_by_tg_id(session, message.from_user.id)
        if not me:
            await message.answer("⚠️ Non sei registrato. Premi /start.")
            return
        if me.blocked:
            await message.answer("🚫 Il tuo account è stato bloccato.")
            return

        # Отдаём ТОЛЬКО непросмотренные оценки, максимум 5 за раз
        items = await get_ratings_for_me_and_mark_seen(session, me, limit=5)
        # Сколько осталось непросмотренных после выдачи этой пачки
        unread_after = await get_unseen_count(session, me)

    if not items:
        await message.answer("📭 Nessuna valutazione ancora 🙂", reply_markup=main_menu_kb(unread_after))
        return

    shown = len(items)
    if unread_after > 0:
        header = f"📬 Ultime valutazioni: mostro {shown} (restano {unread_after})"
    else:
        header = f"📬 Ultime valutazioni: mostro {shown} (fine)"
    # ВАЖНО: тут обновляется кнопка "Chi mi ha valutato (N)" в клавиатуре
    await message.answer(header, reply_markup=main_menu_kb(unread_after))

    for rating, rater in items:
        if not rater:
            continue
        caption = (
            f"👤 *{rater.name}*  •  🎂 {rater.age}  •  📍 {rater.city}\n"
            f"🚻 {gender_it(rater.gender)}\n"
            f"📝 Bio: {rater.bio or '—'}\n\n"
            f"⭐ *Voto:* {rating.score}/10\n"
        )
        if rating.message:
            caption += f"💬 *Messaggio:* {rating.message}"
        await message.answer_photo(photo=rater.photo_file_id, caption=caption, parse_mode="Markdown")


# ---------- Classifica (1 сообщение, inline 1/2/3) ----------
async def build_leaderboard_caption(top, my_rank):
    lines = ["🏆 *Classifica TOP 3*"]
    for i, (u, avg, cnt) in enumerate(top, start=1):
        lines.append(f"{i}. {u.name} — ⭐ {float(avg):.2f}/10 • 📊 {int(cnt)}")
    if my_rank is None:
        lines.append("\n📍 La tua posizione: — (nessun voto ancora)")
    else:
        lines.append(f"\n📍 La tua posizione: *#{my_rank}*")
    lines.append("\n👇 Premi 1/2 per vedere la foto.")
    return "\n".join(lines)

@dp.message(F.text == BTN_LEADERBOARD)
async def leaderboard(message: Message, state: FSMContext):
    await state.clear()
    async with SessionLocal() as session:
        me = await get_user_by_tg_id(session, message.from_user.id)
        if not me:
            await message.answer("⚠️ Non sei registrato. Premi /start.")
            return
        if me.blocked:
            await message.answer("🚫 Il tuo account è stato bloccato.")
            return

        unread = await get_unseen_count(session, me)
        top = await get_top3(session)
        my_rank = await get_my_rank(session, me)

    if not top:
        await message.answer("🏆 Classifica vuota per ora 🙂", reply_markup=main_menu_kb(unread))
        return

    caption = await build_leaderboard_caption(top, my_rank)

    # стартуем с #1 (одним фото-сообщением)
    first_user = top[0][0]
    await message.answer_photo(
        photo=first_user.photo_file_id,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=leaderboard_inline_kb()
    )

@dp.callback_query(F.data.startswith("lb:"))
async def leaderboard_cb(call: CallbackQuery):
    action = call.data.split(":", 1)[1]
    # Inline переключение TOP 3
    if action not in ("1", "2", "3"):
        await call.answer()
        return

    idx = int(action) - 1

    async with SessionLocal() as session:
        me = await get_user_by_tg_id(session, call.from_user.id)
        if not me or me.blocked:
            await call.answer("🚫 Accesso negato", show_alert=True)
            return

        top = await get_top3(session)
        my_rank = await get_my_rank(session, me)

    if idx >= len(top):
        await call.answer("Non disponibile")
        return

    caption = await build_leaderboard_caption(top, my_rank)
    chosen_user = top[idx][0]

    # меняем фото в том же сообщении (одно сообщение, без спама)
    media = InputMediaPhoto(media=chosen_user.photo_file_id, caption=caption, parse_mode="Markdown")
    try:
        await call.message.edit_media(media=media, reply_markup=leaderboard_inline_kb())
    except Exception:
        # если Telegram не даёт edit_media (редко), отправим отдельным фото
        await call.message.answer_photo(photo=chosen_user.photo_file_id, caption=caption, parse_mode="Markdown", reply_markup=leaderboard_inline_kb())

    await call.answer()


# ---------- Admin: channels ----------
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠️ *Admin*\n\n"
        "📢 Canali obbligatori:\n"
        "• `/addchannel @channel` (aggiunge)\n"
        "• `/delchannel @channel` (rimuove)\n"
        "• `/listchannels` (lista)\n\n"
        "🚨 Segnalazioni arrivano automaticamente qui con bottoni.",
        parse_mode="Markdown"
    )


@dp.message(Command("unban"))
async def admin_unban(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Uso: `/unban 123456789`", parse_mode="Markdown")
        return
    tg_id = int(parts[1].strip())
    async with SessionLocal() as session:
        await unblock_user(session, tg_id)
    await message.answer(f"✅ Unbannato: {tg_id}")

@dp.message(Command("listchannels"))
async def admin_list_channels(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with SessionLocal() as session:
        items = await list_required_channels(session)
    if not items:
        await message.answer("📢 Nessun canale obbligatorio.")
        return
    lines = ["📢 Canali obbligatori:"]
    for ch in items:
        lines.append(f"• {ch.username}  ({ch.title or '—'})  link: {ch.link or '—'}")
    await message.answer("\n".join(lines))

@dp.message(Command("addchannel"))
async def admin_add_channel(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Uso: `/addchannel @channel` oppure `/addchannel -100123...`", parse_mode="Markdown")
        return
    username = parts[1].strip()

    # Попробуем узнать title, link
    title = None
    link = None
    try:
        chat = await bot.get_chat(username)
        title = chat.title
        if chat.username:
            link = f"https://t.me/{chat.username}"
    except Exception:
        pass

    async with SessionLocal() as session:
        try:
            await add_required_channel(session, username=username, title=title, link=link)
        except Exception:
            await message.answer("⚠️ Non posso aggiungere (forse уже esiste).")
            return


    await message.answer(f"✅ Aggiunto: {username}\nTitolo: {title or '—'}\nLink: {link or '—'}")


@dp.message(Command("delchannel"))
async def admin_del_channel(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Uso: `/delchannel @channel` oppure `/delchannel -100123...`", parse_mode="Markdown")
        return
    username = parts[1].strip()

    async with SessionLocal() as session:
        ok = await remove_required_channel(session, username=username)

    if ok:
        await message.answer(f"✅ Rimosso: {username}")
    else:
        await message.answer("⚠️ Canale non trovato.")


# ---------- Admin: reports list ----------
@dp.message(Command("reports"))
async def admin_reports(message: Message):
    if not is_admin(message.from_user.id):
        return

    async with SessionLocal() as session:
        items = await list_open_reports(session, limit=20, offset=0)

    if not items:
        await message.answer("✅ Nessuna segnalazione aperta.")
        return

    await message.answer(f"🚨 Segnalazioni aperte: {len(items)} (mostro le ultime 20)")
    for rep, reported, reporter in items:
        cap = (
            f"🚨 *SEGNALAZIONE* (ID: {rep.id})\n"
            f"👤 Segnalato: {reported.name} ({reported.tg_id})\n"
            f"📍 {reported.city} • 🎂 {reported.age} • {gender_it(reported.gender)}\n"
            f"📝 Bio: {reported.bio or '—'}\n"
            f"📸 PhotoVersion: {reported.photo_version}\n"
            f"🙋 Reporter: {rep.reporter_tg_id}\n"
            f"🕒 {rep.created_at}"
        )
        try:
            await message.answer_photo(
                photo=reported.photo_file_id,
                caption=cap,
                parse_mode="Markdown",
                reply_markup=admin_report_kb(rep.id),
            )
        except Exception:
            await message.answer(cap, parse_mode="Markdown", reply_markup=admin_report_kb(rep.id))


@dp.callback_query(F.data.startswith("rep:"))
async def admin_report_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Accesso negato", show_alert=True)
        return

    parts = call.data.split(":")
    if len(parts) != 3:
        await call.answer()
        return

    action, report_id_s = parts[1], parts[2]
    if not report_id_s.isdigit():
        await call.answer()
        return
    report_id = int(report_id_s)

    async with SessionLocal() as session:
        rep = await get_report(session, report_id)
        if not rep:
            await call.answer("Non trovato")
            return

        reported = await get_user_by_tg_id(session, int(rep.reported_tg_id))

        if action == "block":
            if reported:
                await block_user(session, reported.tg_id)
            await close_report(session, report_id)
            await call.answer("Utente bloccato")

            # обновим сообщение
            try:
                if call.message and call.message.caption:
                    new_cap = call.message.caption + "\n\n🚫 *BLOCCATO*"
                    await call.message.edit_caption(new_cap, parse_mode="Markdown", reply_markup=None)
            except Exception:
                pass
            return

        if action == "close":
            await close_report(session, report_id)
            await call.answer("Chiusa")
            try:
                if call.message and call.message.caption:
                    new_cap = call.message.caption + "\n\n✅ *CHIUSA*"
                    await call.message.edit_caption(new_cap, parse_mode="Markdown", reply_markup=None)
            except Exception:
                pass
            return

    await call.answer()


# ---------- Run ----------
async def main():
    await init_db(engine)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
