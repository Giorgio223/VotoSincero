from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

def main_menu(unseen_count: int = 0) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ Valuta")],
            [KeyboardButton(text=f"👀 Chi mi ha valutato ({unseen_count})")],
            [KeyboardButton(text="📊 Il mio profilo")],
        ],
        resize_keyboard=True
    )

def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛠 Required channels")],
            [KeyboardButton(text="🚨 Reports")],
            [KeyboardButton(text="⬅️ Back")],
        ],
        resize_keyboard=True
    )

def rating_top3_kb(target_tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1️⃣", callback_data=f"rate:{target_tg_id}:1"),
            InlineKeyboardButton(text="2️⃣", callback_data=f"rate:{target_tg_id}:2"),
            InlineKeyboardButton(text="3️⃣", callback_data=f"rate:{target_tg_id}:3"),
        ],
        [InlineKeyboardButton(text="🚨 Segnala", callback_data=f"report:{target_tg_id}")]
    ])

def required_channels_kb(channels: list[dict], after: str="check") -> InlineKeyboardMarkup:
    rows=[]
    for c in channels:
        title = c.get("title") or c.get("username") or "channel"
        link = c.get("link") or (f"https://t.me/{c['username'].lstrip('@')}" if c.get("username") else None)
        if link:
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
    rows.append([InlineKeyboardButton(text="✅ Ho fatto", callback_data=f"req:{after}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_channels_kb(items: list[dict]) -> InlineKeyboardMarkup:
    rows=[]
    for c in items:
        status = "✅" if c["is_active"] else "⛔"
        rows.append([InlineKeyboardButton(text=f"{status} {c.get('title') or c['username']}", callback_data=f"admch:{c['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Add channel", callback_data="admch:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_reports_kb(items: list[dict]) -> InlineKeyboardMarkup:
    rows=[]
    for r in items:
        rows.append([InlineKeyboardButton(text=f"#{r['id']} -> {r['reported_tg_id']}", callback_data=f"admr:{r['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_report_actions_kb(report_id: int, reported_tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Ban", callback_data=f"ban:{reported_tg_id}")],
        [InlineKeyboardButton(text="✅ Mark reviewed", callback_data=f"admr:done:{report_id}")],
    ])
