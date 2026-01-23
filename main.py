import asyncio
import os
import re
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select
from dotenv import load_dotenv

from db import (
    create_engine, create_sessionmaker, init_db,
    get_user_by_tg_id, create_user, get_unseen_count, get_rating_stats,
    get_candidate_to_rate, save_rating, fetch_unseen_ratings, mark_ratings_seen,
    get_required_channels, add_report, list_all_channels, add_required_channel,
    toggle_channel, list_reports, mark_report_reviewed, ban_user, unban_user,
)
from states import Reg, Rate, Admin
from keyboards import (
    main_menu, rating_top3_kb, required_channels_kb,
    admin_menu, admin_channels_kb, admin_reports_kb, admin_report_actions_kb
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_URL = os.getenv("DB_URL_LOCAL") or os.getenv("DB_URL") or ""
ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS","").split()) if x.strip().isdigit()}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not DB_URL:
    raise RuntimeError("DB_URL_LOCAL/DB_URL is missing")

engine = create_engine(DB_URL)
SessionLocal = create_sessionmaker(engine)

class BanGuardMiddleware(BaseMiddleware):
    def __init__(self, session_factory):
        super().__init__()
        self.session_factory = session_factory

    async def __call__(self, handler, event, data):
        tg_id = getattr(getattr(event, "from_user", None), "id", None)
        if tg_id is None:
            return await handler(event, data)
        async with self.session_factory() as session:
            u = await get_user_by_tg_id(session, tg_id)
            if u and getattr(u, "blocked", False):
                text = "Sei stato bannato, per essere sbannato scrivi a @gioorgioo"
                # message
                if isinstance(event, Message):
                    await event.answer(text)
                elif isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                return
        return await handler(event, data)

dp = Dispatcher()
dp.update.middleware(BanGuardMiddleware(SessionLocal))

async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def check_required_channels(bot: Bot, session, user_id: int) -> Optional[list[dict]]:
    chans = await get_required_channels(session)
    if not chans:
        return None
    missing=[]
    for c in chans:
        username = c.username
        if not username:
            continue
        try:
            member = await bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                missing.append({"username": username, "title": c.title, "link": c.link})
        except Exception:
            # if can't check (bot not admin in channel), still require join (best effort)
            missing.append({"username": username, "title": c.title, "link": c.link})
    return missing or None

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext, bot: Bot):
    async with SessionLocal() as session:
        u = await get_user_by_tg_id(session, message.from_user.id)
        unseen = await get_unseen_count(session, message.from_user.id)
    if u:
        await state.clear()
        await message.answer("Menu:", reply_markup=main_menu(unseen))
        return
    await state.set_state(Reg.name)
    await message.answer("Ciao! Scrivi il tuo nome oppure il tuo username seguito da @ (esempio @username)")

@dp.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Admin menu:", reply_markup=admin_menu())

@dp.message(F.text == "⬅️ Back")
async def admin_back(message: Message):
    async with SessionLocal() as session:
        unseen = await get_unseen_count(session, message.from_user.id)
    await message.answer("Menu:", reply_markup=main_menu(unseen))

# ----- registration -----
@dp.message(Reg.name)
async def reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        return
    await state.update_data(name=name)
    await state.set_state(Reg.age)
    await message.answer("Quanti anni hai? (18-99)")

@dp.message(Reg.age)
async def reg_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Scrivi un numero (18-99)")
    age = int(message.text)
    if age < 18 or age > 99:
        return await message.answer("Età non valida (18-99)")
    await state.update_data(age=age)
    await state.set_state(Reg.city)
    await message.answer("In che città vivi?")

@dp.message(Reg.city)
async def reg_city(message: Message, state: FSMContext):
    await state.update_data(city=message.text.strip()[:64])
    await state.set_state(Reg.bio)
    await message.answer("Scrivi una breve bio (opzionale). Invia '-' per saltare.")

@dp.message(Reg.bio)
async def reg_bio(message: Message, state: FSMContext):
    bio = message.text.strip()
    if bio == "-":
        bio = ""
    await state.update_data(bio=bio[:500])
    await state.set_state(Reg.gender)
    await message.answer("Sei maschio o femmina? (male/female)")

@dp.message(Reg.gender)
async def reg_gender(message: Message, state: FSMContext):
    g = message.text.strip().lower()
    if g not in ("male","female"):
        return await message.answer("Scrivi: male oppure female")
    await state.update_data(gender=g)
    await state.set_state(Reg.rate_pref)
    await message.answer("Chi vuoi valutare? (male/female/both)")

@dp.message(Reg.rate_pref)
async def reg_rate_pref(message: Message, state: FSMContext):
    rp = message.text.strip().lower()
    if rp not in ("male","female","both"):
        return await message.answer("Scrivi: male / female / both")
    await state.update_data(rate_pref=rp)
    await state.set_state(Reg.be_rated_by)
    await message.answer("Chi può valutarti? (male/female/both)")

@dp.message(Reg.be_rated_by)
async def reg_be_rated_by(message: Message, state: FSMContext):
    br = message.text.strip().lower()
    if br not in ("male","female","both"):
        return await message.answer("Scrivi: male / female / both")
    await state.update_data(be_rated_by=br)
    await state.set_state(Reg.photo)
    await message.answer("Invia una tua foto.")

@dp.message(Reg.photo, F.photo)
async def reg_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    async with SessionLocal() as session:
        await create_user(
            session,
            tg_id=message.from_user.id,
            name=data["name"],
            age=data["age"],
            city=data["city"],
            bio=data.get("bio",""),
            photo_file_id=file_id,
            gender=data["gender"],
            rate_pref=data["rate_pref"],
            be_rated_by=data["be_rated_by"],
        )
        unseen = await get_unseen_count(session, message.from_user.id)
    await state.clear()
    await message.answer("Profilo creato ✅", reply_markup=main_menu(unseen))

@dp.message(Reg.photo)
async def reg_photo_need(message: Message):
    await message.answer("Per favore invia una foto (non un file).")

# ----- profile -----
@dp.message(F.text == "📊 Il mio profilo")
async def my_profile(message: Message):
    async with SessionLocal() as session:
        u = await get_user_by_tg_id(session, message.from_user.id)
        unseen = await get_unseen_count(session, message.from_user.id)
        avg_, cnt_ = await get_rating_stats(session, message.from_user.id)
    if not u:
        return await message.answer("Usa /start per registrarti.")
    txt = f"👤 {u.name}, {u.age}\n📍 {u.city}\n⭐ Rating: {avg_:.2f} ({cnt_})"
    await message.answer_photo(u.photo_file_id, caption=txt, reply_markup=main_menu(unseen))

# ----- rate flow -----
@dp.message(F.text == "⭐ Valuta")
async def rate_start(message: Message, state: FSMContext, bot: Bot):
    async with SessionLocal() as session:
        viewer = await get_user_by_tg_id(session, message.from_user.id)
        if not viewer:
            return await message.answer("Usa /start per registrarti.")
        missing = await check_required_channels(bot, session, message.from_user.id)
        if missing:
            await message.answer("Devi iscriverti ai canali per usare il bot:", reply_markup=required_channels_kb(missing, after="rate"))
            return
        target = await get_candidate_to_rate(session, viewer)
        unseen = await get_unseen_count(session, message.from_user.id)
    if not target:
        return await message.answer("Nessun profilo disponibile ora.", reply_markup=main_menu(unseen))
    await state.set_state(Rate.waiting_score)
    await state.update_data(target_tg_id=target.tg_id, target_photo_version=target.photo_version)
    caption = f"👤 {target.name}, {target.age}\n📍 {target.city}\n{target.bio or ''}"
    await message.answer_photo(target.photo_file_id, caption=caption, reply_markup=rating_top3_kb(target.tg_id))

@dp.callback_query(F.data.startswith("req:"))
async def required_done(cb: CallbackQuery, state: FSMContext, bot: Bot):
    after = cb.data.split(":",1)[1]
    async with SessionLocal() as session:
        missing = await check_required_channels(bot, session, cb.from_user.id)
    if missing:
        await cb.answer("Ancora non sei iscritto a tutti.", show_alert=True)
        return
    await cb.answer("Ok!")
    if after == "rate":
        # trigger rate menu via message
        await cb.message.delete()
        fake = Message.model_validate(cb.message.model_dump())
        fake.from_user = cb.from_user
        await rate_start(fake, state, bot)
    else:
        await cb.message.delete()

@dp.callback_query(F.data.startswith("rate:"))
async def rate_cb(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    target_tg_id = int(parts[1])
    score = int(parts[2])
    data = await state.get_data()
    # ensure matches current target
    if data.get("target_tg_id") != target_tg_id:
        await cb.answer("Questo profilo non è più attivo.", show_alert=True)
        return
    async with SessionLocal() as session:
        viewer = await get_user_by_tg_id(session, cb.from_user.id)
        if not viewer:
            return await cb.answer("Registrati con /start", show_alert=True)
        await save_rating(session, viewer.tg_id, target_tg_id, int(data.get("target_photo_version",1)), score, None)
        unseen = await get_unseen_count(session, cb.from_user.id)
    await state.clear()
    await cb.answer("Voto salvato ✅")
    await cb.message.answer("Menu:", reply_markup=main_menu(unseen))
    await cb.message.delete()

@dp.callback_query(F.data.startswith("report:"))
async def report_cb(cb: CallbackQuery, state: FSMContext):
    target_tg_id = int(cb.data.split(":")[1])
    await state.update_data(report_target=target_tg_id)
    await state.set_state(Rate.waiting_message)
    await cb.answer()
    await cb.message.answer("Scrivi il motivo della segnalazione (o '-' per vuoto).")

@dp.message(Rate.waiting_message)
async def report_text(message: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("report_target")
    if not target:
        await state.clear()
        return
    text_ = message.text.strip()
    if text_ == "-":
        text_ = ""
    async with SessionLocal() as session:
        await add_report(session, message.from_user.id, int(target), text_)
        unseen = await get_unseen_count(session, message.from_user.id)
    await state.clear()
    await message.answer("Segnalazione inviata ✅", reply_markup=main_menu(unseen))

# ----- who rated me -----
@dp.message(F.text.startswith("👀 Chi mi ha valutato"))
async def who_rated_me(message: Message):
    async with SessionLocal() as session:
        u = await get_user_by_tg_id(session, message.from_user.id)
        if not u:
            return await message.answer("Usa /start per registrarti.")
        items = await fetch_unseen_ratings(session, message.from_user.id, limit=5)
        if not items:
            unseen = await get_unseen_count(session, message.from_user.id)
            return await message.answer("Nessuna nuova valutazione.", reply_markup=main_menu(unseen))
        # mark seen
        ids=[r.id for r in items]
        await mark_ratings_seen(session, message.from_user.id, ids)
        unseen = await get_unseen_count(session, message.from_user.id)

    # send list (max 5)
    txt = "📝 Nuove valutazioni:\n\n"
    for r in items:
        txt += f"⭐ {r.score}  | da {r.rater_tg_id}\n"
        if r.message:
            txt += f"💬 {r.message}\n"
        txt += f"🕒 {r.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
    await message.answer(txt.strip(), reply_markup=main_menu(unseen))

# ----- admin: required channels -----
@dp.message(F.text == "🛠 Required channels")
async def adm_channels(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    async with SessionLocal() as session:
        items = await list_all_channels(session)
    payload=[{"id":c.id,"username":c.username or "", "title":c.title or c.username or "", "is_active":c.is_active} for c in items]
    await message.answer("Channels:", reply_markup=admin_channels_kb(payload))

@dp.callback_query(F.data.startswith("admch:"))
async def admch_cb(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("No", show_alert=True)
    cmd = cb.data.split(":",1)[1]
    if cmd == "add":
        await state.set_state(Admin.add_channel)
        await cb.message.answer("Invia: @username | Titolo | link (link opzionale).")
        await cb.answer()
        return
    channel_id = int(cmd)
    async with SessionLocal() as session:
        await toggle_channel(session, channel_id)
        items = await list_all_channels(session)
    payload=[{"id":c.id,"username":c.username or "", "title":c.title or c.username or "", "is_active":c.is_active} for c in items]
    await cb.message.edit_text("Channels:", reply_markup=admin_channels_kb(payload))
    await cb.answer("OK")

@dp.message(Admin.add_channel)
async def adm_add_channel(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        return
    parts=[p.strip() for p in message.text.split("|")]
    username = parts[0] if parts else ""
    title = parts[1] if len(parts)>1 else ""
    link = parts[2] if len(parts)>2 else ""
    if username and not username.startswith("@"):
        username = "@"+username
    async with SessionLocal() as session:
        await add_required_channel(session, username or None, title or None, link or None)
        items = await list_all_channels(session)
    payload=[{"id":c.id,"username":c.username or "", "title":c.title or c.username or "", "is_active":c.is_active} for c in items]
    await state.clear()
    await message.answer("Added ✅", reply_markup=admin_channels_kb(payload))

# ----- admin: reports -----
@dp.message(F.text == "🚨 Reports")
async def adm_reports(message: Message):
    if not await is_admin(message.from_user.id):
        return
    async with SessionLocal() as session:
        items = await list_reports(session, limit=20)
    payload=[{"id":r.id, "reported_tg_id":r.reported_tg_id} for r in items]
    if not payload:
        return await message.answer("No reports.")
    await message.answer("Reports:", reply_markup=admin_reports_kb(payload))

@dp.callback_query(F.data.startswith("admr:"))
async def admr_cb(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("No", show_alert=True)
    parts=cb.data.split(":")
    if len(parts)>=3 and parts[1]=="done":
        report_id=int(parts[2])
        async with SessionLocal() as session:
            await mark_report_reviewed(session, report_id)
        await cb.answer("Done")
        return
    report_id=int(parts[1])
    async with SessionLocal() as session:
        from db import Report
        res = await session.execute(select(Report).where(Report.id==report_id))
        r = res.scalar_one_or_none()
    if not r:
        return await cb.answer("Not found", show_alert=True)
    text_ = f"Report #{r.id}\nReporter: {r.reporter_tg_id}\nReported: {r.reported_tg_id}\n\n{r.text}"
    await cb.message.answer(text_, reply_markup=admin_report_actions_kb(r.id, r.reported_tg_id))
    await cb.answer()

@dp.callback_query(F.data.startswith("ban:"))
async def ban_cb(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("No", show_alert=True)
    tg_id=int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        await ban_user(session, tg_id)
    await cb.answer("Banned ✅", show_alert=True)

@dp.message(Command("unban"))
async def unban_cmd(message: Message):
    if not await is_admin(message.from_user.id):
        return
    m = re.search(r"/unban\s+(\d+)", message.text or "")
    if not m:
        return await message.answer("Uso: /unban <tg_id>")
    tg_id=int(m.group(1))
    async with SessionLocal() as session:
        await unban_user(session, tg_id)
    await message.answer(f"Unbanned {tg_id} ✅")

async def main():
    bot = Bot(BOT_TOKEN)
    await init_db(engine)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
