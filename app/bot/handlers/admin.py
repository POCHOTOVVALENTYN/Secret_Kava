# app/bot/handlers/admin.py
"""
Task-oriented Admin Panel.
Access restricted to ADMIN_IDS only.

Sections:
  💰 Ціни та тарифи
    - Ціни консультацій  (Реєстр спеціалістів: Ціна_Онлайн, Ціна_Офлайн)
    - Ціни кабінетів     (Реєстр Кабінетів: Тариф_годинний, Вечірній_тариф)
    - Ціни заходів       (Реєстр Заходів (Афіши): Ціна)
  📋 Реєстри (поточний місяць)
    - Консультації онлайн
    - Консультації офлайн
    - Бронювання кабінетів
    - Реєстр заходів (актуальні)
    - Реєстр Афіш (хто записався)

Column layout in Google Sheets (0-indexed):
  Реєстр спеціалістів:
    0=ID  1=Ім'я  2=Спеціалізація  3=Досвід  4=Ціна_Онлайн  5=Ціна_Офлайн  6=Активний
  Реєстр Кабінетів:
    0=ID  1=Назва  2=Опис  3=Тариф_годинний  4=Вечірній_тариф  5=Активний
  Реєстр Заходів (Афіши):
    0=ID  1=Назва  2=Ведучий  3=Дата  4=Ліміт місць  5=Ціна  6=Місяць  7=Статус
  Бронювання до спеціаліста:
    0=ID  1=Клієнт  2=Телефон  3=Спеціаліст  4=Формат  5=Дата  6=Час  7=Статус оплати  8=Сума
  Бронювання кабінету:
    0=ID  1=Клієнт  2=Телефон  3=Кабінет  4=Дата  5=Час  6=Статус оплати  7=Сума
  Бронювання на заходи (Афіши):
    0=ID  1=Клієнт  2=Телефон  3=Захід  4=Дата  5=Час  6=Статус оплати  7=Сума
"""
import asyncio
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from structlog import get_logger

from app.bot.states.admin import AdminMenuFSM
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.review import Review


logger = get_logger()
router = Router(name="admin_router")

# ─── Access Control ───────────────────────────────────────────────────────────
ADMIN_IDS: set[int] = {660331103, 830196453}

def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ─── Sheet / Column Config ────────────────────────────────────────────────────
SHEET_SPECIALISTS  = "Реєстр спеціалістів"
SHEET_ROOMS        = "Реєстр Кабінетів"
SHEET_EVENTS_REG   = "Заявки організаторів на заходи (Афіши)"
SHEET_BOOK_CONSULT = "Бронювання до спеціаліста"
SHEET_BOOK_ROOMS   = "Бронювання кабінету"
SHEET_BOOK_EVENTS  = "Бронювання на заходи (Афіши)"

# Column letters for price editing (1-indexed spreadsheet row = data_row_idx + 2)
COL_SPEC_ONLINE  = "E"   # Ціна_Онлайн
COL_SPEC_OFFLINE = "F"   # Ціна_Офлайн
COL_ROOM_HOURLY  = "D"   # Тариф_годинний
COL_ROOM_EVENING = "E"   # Вечірній_тариф
COL_EVENT_PRICE  = "F"   # Ціна (after Ліміт місць in col E)

ROWS_PER_PAGE = 5

# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _edit_admin_msg(bot, chat_id: int, msg_id: int, text: str, reply_markup) -> None:
    """Редагує головне повідомлення адмін-панелі замість надсилання нового."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    except Exception:
        pass


def _fmt_price(val: str) -> str:
    return f"{val} грн" if val else "—"

def _current_month_label() -> str:
    return datetime.now().strftime("%m.%Y")

def _is_current_month(date_str: str) -> bool:
    """Check if a date string (any format containing DD.MM.YYYY or YYYY-MM-DD) is in current month."""
    now = datetime.now()
    try:
        # Try DD.MM.YYYY
        if "." in date_str:
            parts = date_str.split(".")
            if len(parts) >= 2:
                return int(parts[1]) == now.month and (len(parts) < 3 or int(parts[2]) == now.year)
        # Try YYYY-MM-DD
        elif "-" in date_str:
            parts = date_str.split("-")
            if len(parts) >= 2:
                return int(parts[1]) == now.month and int(parts[0]) == now.year
    except (ValueError, IndexError):
        pass
    return False

def _safe(row: list, idx: int, default: str = "—") -> str:
    return str(row[idx]).strip() if idx < len(row) else default

def _trunc(s: str, n: int = 14) -> str:
    return s if len(s) <= n else s[:n-1] + "…"

# ─── Keyboards ────────────────────────────────────────────────────────────────

def _kb_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💰 Ціни та тарифи",    callback_data="adm:prices"))
    b.row(InlineKeyboardButton(text="📋 Реєстри",            callback_data="adm:regs"))
    b.row(InlineKeyboardButton(text="📝 Модерація відгуків", callback_data="adm:reviews"))
    b.row(InlineKeyboardButton(text="📅 Вільні слоти",        callback_data="adm:slots_menu"))
    b.row(InlineKeyboardButton(text="🏠 Головне меню",       callback_data="menu:main"))
    return b.as_markup()

def _kb_prices() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👤 Ціни консультацій", callback_data="adm:p:spec"))
    b.row(InlineKeyboardButton(text="🏢 Ціни кабінетів",    callback_data="adm:p:room"))
    b.row(InlineKeyboardButton(text="🎭 Ціни заходів",       callback_data="adm:p:event"))
    b.row(InlineKeyboardButton(text="⬅️ Адмін меню",        callback_data="adm:main"))
    return b.as_markup()

def _kb_registries() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    month = _current_month_label()
    b.row(InlineKeyboardButton(text=f"💻 Консультації онлайн ({month})",  callback_data="adm:r:con_on"))
    b.row(InlineKeyboardButton(text=f"🛋 Консультації офлайн ({month})",  callback_data="adm:r:con_off"))
    b.row(InlineKeyboardButton(text=f"🏠 Бронювання кабінетів ({month})", callback_data="adm:r:rooms"))
    b.row(InlineKeyboardButton(text="🎭 Реєстр заходів (актуальні)",       callback_data="adm:r:ev_reg"))
    b.row(InlineKeyboardButton(text=f"🎟 Реєстр Афіш ({month})",           callback_data="adm:r:afisha"))
    b.row(InlineKeyboardButton(text="⬅️ Адмін меню",                       callback_data="adm:main"))
    return b.as_markup()

def _kb_item_list(items: list[tuple[int, str]], prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    """Generic list of (row_idx, label) buttons."""
    b = InlineKeyboardBuilder()
    for row_idx, label in items:
        b.row(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{row_idx}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb))
    return b.as_markup()

def _kb_spec_prices(row_idx: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✏️ Змінити онлайн",  callback_data=f"adm:pf:on:{row_idx}"),
        InlineKeyboardButton(text="✏️ Змінити офлайн",  callback_data=f"adm:pf:off:{row_idx}"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="adm:p:spec"))
    return b.as_markup()

def _kb_room_prices(row_idx: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✏️ Змінити тариф",   callback_data=f"adm:pf:hr:{row_idx}"),
        InlineKeyboardButton(text="✏️ Змінити вечірній", callback_data=f"adm:pf:ev:{row_idx}"),
    )
    b.row(InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="adm:p:room"))
    return b.as_markup()

def _kb_event_price(row_idx: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✏️ Змінити ціну", callback_data=f"adm:pf:ep:{row_idx}"))
    b.row(InlineKeyboardButton(text="⬅️ Назад до списку", callback_data="adm:p:event"))
    return b.as_markup()

def _kb_cancel_price(back_cb: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Скасувати", callback_data=back_cb))
    return b.as_markup()

def _kb_registry_nav(reg_key: str, page: int, total: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:rp:{reg_key}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total}", callback_data="adm:noop"))
    if page < total - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:rp:{reg_key}:{page+1}"))
    b.row(*nav)
    b.row(InlineKeyboardButton(text="⬅️ До реєстрів", callback_data="adm:regs"))
    return b.as_markup()

# ─── Entry / Main Menu ────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:admin")
async def admin_entry(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫 Доступ заборонено", show_alert=True)
        return
    await state.set_state(AdminMenuFSM.MainMenu)
    await call.message.edit_text(
        "🔐 *Адмін Панель*\n\nОберіть розділ:",
        parse_mode="Markdown", reply_markup=_kb_main()
    )
    await call.answer()

@router.callback_query(F.data == "adm:main")
async def admin_main(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫 Доступ заборонено", show_alert=True)
        return
    await state.set_state(AdminMenuFSM.MainMenu)
    await call.message.edit_text(
        "🔐 *Адмін Панель*\n\nОберіть розділ:",
        parse_mode="Markdown", reply_markup=_kb_main()
    )
    await call.answer()

# ─── Prices Menu ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:prices")
async def admin_prices_menu(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await state.set_state(AdminMenuFSM.PricesMenu)
    await call.message.edit_text(
        "💰 *Ціни та тарифи*\n\nОберіть категорію:",
        parse_mode="Markdown", reply_markup=_kb_prices()
    )
    await call.answer()

# ─── Specialist Prices ────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:p:spec")
async def admin_spec_list(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳ Завантаження...")

    rows = await _load(sheets, SHEET_SPECIALISTS)
    data_rows = rows[1:] if len(rows) > 1 else []

    items = []
    for i, row in enumerate(data_rows):
        name = _safe(row, 1)
        if name and name != "—":
            items.append((i, f"👤 {name}"))

    if not items:
        await call.message.edit_text(
            "⚠️ Спеціалістів не знайдено в таблиці.",
            reply_markup=_kb_prices()
        )
        return

    await state.update_data(adm_spec_rows=rows)
    await state.set_state(AdminMenuFSM.SelectItem)
    await call.message.edit_text(
        "👤 *Ціни консультацій*\n\nОберіть спеціаліста:",
        parse_mode="Markdown",
        reply_markup=_kb_item_list(items, "adm:pi:spec", "adm:prices")
    )

@router.callback_query(F.data.startswith("adm:pi:spec:"))
async def admin_spec_card(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    row_idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    rows = data.get("adm_spec_rows", [])
    # row_idx = index in data_rows (rows[1:]), so actual = rows[row_idx + 1]
    actual_idx = row_idx + 1
    row = rows[actual_idx] if actual_idx < len(rows) else []

    name    = _safe(row, 1)
    online  = _safe(row, 4)
    offline = _safe(row, 5)

    await state.update_data(adm_price_row_idx=row_idx, adm_price_section="spec")
    await state.set_state(AdminMenuFSM.ViewPrices)
    await call.message.edit_text(
        f"👤 *{name}*\n\n"
        f"💻 Онлайн: *{_fmt_price(online)}*\n"
        f"🛋 Офлайн: *{_fmt_price(offline)}*\n\n"
        f"Оберіть поле для редагування:",
        parse_mode="Markdown",
        reply_markup=_kb_spec_prices(row_idx)
    )
    await call.answer()

# ─── Room Prices ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:p:room")
async def admin_room_list(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳ Завантаження...")

    rows = await _load(sheets, SHEET_ROOMS)
    data_rows = rows[1:] if len(rows) > 1 else []

    items = []
    for i, row in enumerate(data_rows):
        name = _safe(row, 1)
        if name and name != "—":
            items.append((i, f"🏢 {name}"))

    if not items:
        await call.message.edit_text(
            "⚠️ Кабінетів не знайдено в таблиці.",
            reply_markup=_kb_prices()
        )
        return

    await state.update_data(adm_room_rows=rows)
    await state.set_state(AdminMenuFSM.SelectItem)
    await call.message.edit_text(
        "🏢 *Ціни кабінетів*\n\nОберіть кабінет:",
        parse_mode="Markdown",
        reply_markup=_kb_item_list(items, "adm:pi:room", "adm:prices")
    )

@router.callback_query(F.data.startswith("adm:pi:room:"))
async def admin_room_card(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    row_idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    rows = data.get("adm_room_rows", [])
    actual_idx = row_idx + 1
    row = rows[actual_idx] if actual_idx < len(rows) else []

    name    = _safe(row, 1)
    hourly  = _safe(row, 3)
    evening = _safe(row, 4)

    await state.update_data(adm_price_row_idx=row_idx, adm_price_section="room")
    await state.set_state(AdminMenuFSM.ViewPrices)
    await call.message.edit_text(
        f"🏢 *{name}*\n\n"
        f"⏱ Тариф: *{_fmt_price(hourly)}*/год\n"
        f"🌙 Вечірній: *{_fmt_price(evening)}*/год\n\n"
        f"Оберіть поле для редагування:",
        parse_mode="Markdown",
        reply_markup=_kb_room_prices(row_idx)
    )
    await call.answer()

# ─── Event Prices ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:p:event")
async def admin_event_list(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳ Завантаження...")

    rows = await _load(sheets, SHEET_EVENTS_REG)
    data_rows = rows[1:] if len(rows) > 1 else []

    items = []
    for i, row in enumerate(data_rows):
        name = _safe(row, 1)
        if name and name != "—":
            items.append((i, f"🎭 {name}"))

    if not items:
        await call.message.edit_text(
            "⚠️ Заходів не знайдено в таблиці.",
            reply_markup=_kb_prices()
        )
        return

    await state.update_data(adm_event_rows=rows)
    await state.set_state(AdminMenuFSM.SelectItem)
    await call.message.edit_text(
        "🎭 *Ціни заходів*\n\nОберіть захід:",
        parse_mode="Markdown",
        reply_markup=_kb_item_list(items, "adm:pi:event", "adm:prices")
    )

@router.callback_query(F.data.startswith("adm:pi:event:"))
async def admin_event_card(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    row_idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    rows = data.get("adm_event_rows", [])
    actual_idx = row_idx + 1
    row = rows[actual_idx] if actual_idx < len(rows) else []

    name  = _safe(row, 1)
    date  = _safe(row, 3)
    price = _safe(row, 5)

    await state.update_data(adm_price_row_idx=row_idx, adm_price_section="event")
    await state.set_state(AdminMenuFSM.ViewPrices)
    await call.message.edit_text(
        f"🎭 *{name}*\n"
        f"📅 {date}\n\n"
        f"💵 Ціна: *{_fmt_price(price)}*\n\n"
        f"Оберіть поле для редагування:",
        parse_mode="Markdown",
        reply_markup=_kb_event_price(row_idx)
    )
    await call.answer()

# ─── Price Field Selection → Enter New Value ──────────────────────────────────

# Field codes: on=онлайн, off=офлайн, hr=тариф, ev=вечірній, ep=ціна захід
_PRICE_FIELD_META = {
    "on":  (SHEET_SPECIALISTS, COL_SPEC_ONLINE,  "Ціна онлайн",      "adm:p:spec"),
    "off": (SHEET_SPECIALISTS, COL_SPEC_OFFLINE, "Ціна офлайн",      "adm:p:spec"),
    "hr":  (SHEET_ROOMS,       COL_ROOM_HOURLY,  "Тариф/год",        "adm:p:room"),
    "ev":  (SHEET_ROOMS,       COL_ROOM_EVENING, "Вечірній тариф",   "adm:p:room"),
    "ep":  (SHEET_EVENTS_REG,  COL_EVENT_PRICE,  "Ціна заходу",      "adm:p:event"),
}

@router.callback_query(F.data.startswith("adm:pf:"))
async def admin_price_field(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    # adm:pf:{field_code}:{row_idx}
    parts = call.data.split(":")
    field_code = parts[2]
    row_idx = int(parts[3])

    meta = _PRICE_FIELD_META.get(field_code)
    if not meta:
        await call.answer("❌ Невідоме поле", show_alert=True)
        return

    sheet_name, col_letter, field_label, back_cb = meta
    # spreadsheet row: header=row1, data starts row2 → row_idx+2
    spreadsheet_row = row_idx + 2

    await state.update_data(
        adm_edit_sheet=sheet_name,
        adm_edit_col=col_letter,
        adm_edit_row=spreadsheet_row,
        adm_edit_field=field_label,
        adm_edit_back=back_cb,
        adm_edit_item_idx=row_idx,
        adm_msg_id=call.message.message_id,   # зберігаємо для edit замість answer
        adm_chat_id=call.message.chat.id,     # зберігаємо для edit замість answer
    )
    await state.set_state(AdminMenuFSM.EnterNewPrice)

    await call.message.edit_text(
        f"✏️ *{field_label}*\n\n"
        f"Введіть нову ціну (числом, у гривнях):",
        parse_mode="Markdown",
        reply_markup=_kb_cancel_price(back_cb)
    )
    await call.answer()

@router.message(AdminMenuFSM.EnterNewPrice)
async def admin_save_price(message: Message, state: FSMContext, sheets=None) -> None:
    if not _is_admin(message.from_user.id):
        return

    raw = message.text.strip().replace(",", ".").replace(" ", "")
    try:
        await message.delete()
    except Exception:
        pass

    # Отримуємо FSM дані одразу — потрібні msg_id/chat_id навіть при помилці валідації
    data = await state.get_data()
    msg_id  = data.get("adm_msg_id")
    chat_id = data.get("adm_chat_id")
    back_cb = data.get("adm_edit_back", "adm:prices")

    # Validate numeric input
    try:
        value = float(raw)
        if value < 0:
            raise ValueError
    except ValueError:
        await _edit_admin_msg(
            message.bot, chat_id, msg_id,
            "⚠️ *Некоректне значення*\n\n"
            "Введіть суму числом (наприклад: `1200` або `1500.50`):",
            reply_markup=_kb_cancel_price(back_cb)
        )
        return  # залишаємося в стані EnterNewPrice — юзер вводить ще раз

    sheet_name      = data.get("adm_edit_sheet", "")
    col_letter      = data.get("adm_edit_col", "A")
    spreadsheet_row = data.get("adm_edit_row", 2)
    field_label     = data.get("adm_edit_field", "")
    section         = data.get("adm_price_section", "")
    row_idx         = data.get("adm_edit_item_idx", 0)

    # Write to Google Sheets
    if sheets and sheet_name:
        try:
            await sheets.update_cell(
                sheet_name, spreadsheet_row, col_letter,
                int(value) if value == int(value) else value
            )
        except Exception as e:
            logger.error("admin_price_update_failed", error=str(e))
            await _edit_admin_msg(
                message.bot, chat_id, msg_id,
                "❌ *Помилка збереження*\n\nСпробуйте ще раз.",
                reply_markup=_kb_cancel_price(back_cb)
            )
            return

    # Reload updated row and show refreshed card
    rows = []
    if sheets:
        rows = await _load(sheets, sheet_name)

    await state.set_state(AdminMenuFSM.ViewPrices)

    value_str    = str(int(value)) if value == int(value) else str(value)
    confirm_text = f"✅ *Збережено!* {field_label}: *{value_str} грн*\n\n"

    if section == "spec" and rows:
        actual  = rows[row_idx + 1] if (row_idx + 1) < len(rows) else []
        await state.update_data(adm_spec_rows=rows)
        name    = _safe(actual, 1)
        online  = _safe(actual, 4)
        offline = _safe(actual, 5)
        await _edit_admin_msg(
            message.bot, chat_id, msg_id,
            confirm_text +
            f"👤 *{name}*\n💻 Онлайн: *{_fmt_price(online)}*\n🛋 Офлайн: *{_fmt_price(offline)}*\n\nОберіть поле:",
            reply_markup=_kb_spec_prices(row_idx)
        )
    elif section == "room" and rows:
        actual  = rows[row_idx + 1] if (row_idx + 1) < len(rows) else []
        await state.update_data(adm_room_rows=rows)
        name    = _safe(actual, 1)
        hourly  = _safe(actual, 3)
        evening = _safe(actual, 4)
        await _edit_admin_msg(
            message.bot, chat_id, msg_id,
            confirm_text +
            f"🏢 *{name}*\n⏱ Тариф: *{_fmt_price(hourly)}*/год\n🌙 Вечірній: *{_fmt_price(evening)}*/год\n\nОберіть поле:",
            reply_markup=_kb_room_prices(row_idx)
        )
    elif section == "event" and rows:
        actual = rows[row_idx + 1] if (row_idx + 1) < len(rows) else []
        await state.update_data(adm_event_rows=rows)
        name  = _safe(actual, 1)
        date  = _safe(actual, 3)
        price = _safe(actual, 5)
        await _edit_admin_msg(
            message.bot, chat_id, msg_id,
            confirm_text +
            f"🎭 *{name}*\n📅 {date}\n💵 Ціна: *{_fmt_price(price)}*\n\nОберіть поле:",
            reply_markup=_kb_event_price(row_idx)
        )
    else:
        await _edit_admin_msg(
            message.bot, chat_id, msg_id,
            confirm_text,
            reply_markup=_kb_cancel_price(back_cb)
        )

# ─── Registries Menu ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:regs")
async def admin_regs_menu(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await state.set_state(AdminMenuFSM.RegistriesMenu)
    await call.message.edit_text(
        f"📋 *Реєстри*\n_Дані за {_current_month_label()}_\n\nОберіть розділ:",
        parse_mode="Markdown",
        reply_markup=_kb_registries()
    )
    await call.answer()

# ─── Generic Registry Loader ──────────────────────────────────────────────────

async def _show_registry(
    call: CallbackQuery,
    state: FSMContext,
    sheets,
    reg_key: str,
    title: str,
    sheet_name: str,
    col_indices: list[int],       # which columns to show
    col_headers: list[str],       # header labels for display
    filter_fn,                    # lambda row -> bool
    page: int = 0,
) -> None:
    """Generic registry viewer with month filter and pagination."""
    await call.message.edit_text(f"⏳ _Завантаження {title}..._", parse_mode="Markdown")

    rows = await _load(sheets, sheet_name)
    data_rows = rows[1:] if len(rows) > 1 else []

    # Apply filter
    filtered = [r for r in data_rows if filter_fn(r)]
    total = max(1, -(-len(filtered) // ROWS_PER_PAGE))
    page = max(0, min(page, total - 1))

    start = page * ROWS_PER_PAGE
    page_rows = filtered[start: start + ROWS_PER_PAGE]

    await state.update_data(adm_reg_key=reg_key, adm_reg_rows=filtered,
                            adm_reg_title=title, adm_reg_sheet=sheet_name,
                            adm_reg_cols=col_indices, adm_reg_heads=col_headers,
                            adm_reg_filter="")
    await state.set_state(AdminMenuFSM.ViewingRegistry)

    lines = [f"📋 *{title}*", f"_Місяць: {_current_month_label()} • {len(filtered)} записів_\n"]
    if not page_rows:
        lines.append("_(за цей місяць записів немає)_")
    else:
        for row in page_rows:
            cells = [_trunc(_safe(row, ci)) for ci in col_indices]
            lines.append("• " + " | ".join(cells))

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_kb_registry_nav(reg_key, page, total)
    )

@router.callback_query(F.data.startswith("adm:rp:"))
async def admin_registry_page(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    # adm:rp:{reg_key}:{page}
    parts = call.data.split(":")
    reg_key = parts[2]
    page = int(parts[3])

    data = await state.get_data()
    filtered = data.get("adm_reg_rows", [])
    title = data.get("adm_reg_title", "Реєстр")
    col_indices = data.get("adm_reg_cols", [])
    total = max(1, -(-len(filtered) // ROWS_PER_PAGE))
    page = max(0, min(page, total - 1))

    start = page * ROWS_PER_PAGE
    page_rows = filtered[start: start + ROWS_PER_PAGE]

    lines = [f"📋 *{title}*", f"_Місяць: {_current_month_label()} • {len(filtered)} записів_\n"]
    if not page_rows:
        lines.append("_(за цей місяць записів немає)_")
    else:
        for row in page_rows:
            cells = [_trunc(_safe(row, ci)) for ci in col_indices]
            lines.append("• " + " | ".join(cells))

    try:
        await call.message.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=_kb_registry_nav(reg_key, page, total)
        )
    except Exception:
        pass
    await call.answer()

# ─── Individual Registry Handlers ─────────────────────────────────────────────

# Бронювання до спеціаліста columns:
# 0=ID  1=Клієнт  2=Телефон  3=Спеціаліст  4=Формат  5=Дата  6=Час  7=Статус оплати  8=Сума
_CONSULT_COLS = [1, 2, 3, 5, 6, 7]
_CONSULT_HEADS = ["Клієнт", "Телефон", "Спеціаліст", "Дата", "Час", "Статус"]

@router.callback_query(F.data == "adm:r:con_on")
async def reg_consult_online(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳")
    await _show_registry(
        call, state, sheets,
        reg_key="con_on",
        title="Консультації онлайн",
        sheet_name=SHEET_BOOK_CONSULT,
        col_indices=_CONSULT_COLS,
        col_headers=_CONSULT_HEADS,
        filter_fn=lambda r: _safe(r, 4).lower() == "онлайн" and _is_current_month(_safe(r, 5)),
    )

@router.callback_query(F.data == "adm:r:con_off")
async def reg_consult_offline(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳")
    await _show_registry(
        call, state, sheets,
        reg_key="con_off",
        title="Консультації офлайн",
        sheet_name=SHEET_BOOK_CONSULT,
        col_indices=_CONSULT_COLS,
        col_headers=_CONSULT_HEADS,
        filter_fn=lambda r: _safe(r, 4).lower() == "офлайн" and _is_current_month(_safe(r, 5)),
    )

# Бронювання кабінету columns:
# 0=ID  1=Клієнт  2=Телефон  3=Кабінет  4=Дата  5=Час  6=Статус оплати  7=Сума
@router.callback_query(F.data == "adm:r:rooms")
async def reg_rooms(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳")
    await _show_registry(
        call, state, sheets,
        reg_key="rooms",
        title="Бронювання кабінетів",
        sheet_name=SHEET_BOOK_ROOMS,
        col_indices=[1, 2, 3, 4, 5, 6],
        col_headers=["Клієнт", "Телефон", "Кабінет", "Дата", "Час", "Статус"],
        filter_fn=lambda r: _is_current_month(_safe(r, 4)),
    )

# Реєстр Заходів (Афіши) — show only active events
# 0=ID  1=Назва  2=Ведучий  3=Дата  4=Ліміт місць  5=Ціна  6=Місяць  7=Статус
@router.callback_query(F.data == "adm:r:ev_reg")
async def reg_events(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳")

    rows = await _load(sheets, SHEET_EVENTS_REG)
    data_rows = rows[1:] if len(rows) > 1 else []
    filtered = [r for r in data_rows if _safe(r, 7).lower() in ("актуальний", "анонс")]
    total = max(1, -(-len(filtered) // ROWS_PER_PAGE))

    await state.update_data(adm_reg_key="ev_reg", adm_reg_rows=filtered,
                            adm_reg_title="Реєстр заходів", adm_reg_cols=[1, 3, 4, 5, 7])
    await state.set_state(AdminMenuFSM.ViewingRegistry)

    lines = [f"🎭 *Реєстр заходів*", f"_Актуальні та анонси • {len(filtered)} заходів_\n"]
    if not filtered:
        lines.append("_(актуальних заходів немає)_")
    else:
        for row in filtered[:ROWS_PER_PAGE]:
            name   = _trunc(_safe(row, 1), 18)
            date   = _safe(row, 3)
            limit  = _safe(row, 4)
            price  = _safe(row, 5)
            status = _safe(row, 7)
            lines.append(f"• *{name}*\n  📅 {date} | 👥 Ліміт: {limit} | 💵 {price} грн | {status}")

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_kb_registry_nav("ev_reg", 0, total)
    )

# Бронювання на заходи (Афіши) columns:
# 0=ID  1=Клієнт  2=Телефон  3=Захід  4=Дата  5=Час  6=Статус оплати  7=Сума
@router.callback_query(F.data == "adm:r:afisha")
async def reg_afisha(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
    await call.answer("⏳")
    await _show_registry(
        call, state, sheets,
        reg_key="afisha",
        title="Реєстр Афіш",
        sheet_name=SHEET_BOOK_EVENTS,
        col_indices=[1, 2, 3, 4, 6],
        col_headers=["Клієнт", "Телефон", "Захід", "Дата", "Статус"],
        filter_fn=lambda r: _is_current_month(_safe(r, 4)),
    )

# ─── Shared Loader ────────────────────────────────────────────────────────────

async def _load(sheets, sheet_name: str) -> list[list]:
    if sheets is None:
        return []
    try:
        return await sheets.read_sheet(sheet_name)
    except Exception as e:
        logger.error("admin_sheet_load_failed", sheet=sheet_name, error=str(e))
        return []

# ─── Noop ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm:noop")
async def admin_noop(call: CallbackQuery) -> None:
    await call.answer()


# ─── Review Moderation ────────────────────────────────────────────────────────

async def _update_sheets_review_status(sheets, client_name: str, comment: str, new_status: str) -> None:
    if not sheets:
        return
    try:
        rows = await sheets.read_sheet("Реєстр відгуків")
        for idx, row in enumerate(rows):
            if idx == 0:
                continue
            row_client = str(row[1]).strip() if len(row) > 1 else ""
            row_comment = str(row[3]).strip() if len(row) > 3 else ""
            if row_client == client_name.strip() and row_comment == comment.strip():
                # Update status at column E (5th column -> index 4, update_cell is 1-indexed so column 5 is "E")
                await sheets.update_cell("Реєстр відгуків", idx + 1, "E", new_status)
                logger.info("sheets_review_status_updated", row=idx + 1, status=new_status)
                break
    except Exception as e:
        logger.error("failed_to_update_sheets_review_status", error=str(e))


@router.callback_query(F.data == "adm:reviews")
async def admin_reviews_list(call: CallbackQuery, db_session: AsyncSession, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫 Доступ заборонено", show_alert=True)
        return

    await state.set_state(AdminMenuFSM.ModeratingReviews)
    
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    
    try:
        query = (
            select(Review)
            .where(Review.is_moderated == False)
            .order_by(Review.id.asc())
            .options(selectinload(Review.user))
        )
        result = await db_session.execute(query)
        pending = result.scalars().all()
    except Exception as e:
        logger.error("failed_to_fetch_pending_reviews", error=str(e))
        await call.answer("❌ Помилка завантаження відгуків", show_alert=True)
        return

    if not pending:
        b = InlineKeyboardBuilder()
        b.row(InlineKeyboardButton(text="⬅️ Адмін меню", callback_data="adm:main"))
        await call.message.edit_text(
            "📝 *Модерація відгуків*\n\nНемає нових відгуків на модерації.",
            parse_mode="Markdown",
            reply_markup=b.as_markup()
        )
        await call.answer()
        return

    # Show the first pending review
    rev = pending[0]
    author = rev.user.first_name
    if rev.user.last_name:
        author += f" {rev.user.last_name}"
    if rev.user.username:
        author += f" (@{rev.user.username})"
        
    stars = "⭐" * min(max(rev.rating, 1), 5)
    
    text = (
        f"📝 *Модерація відгуків* (Залишилось: {len(pending)})\n\n"
        f"👤 *Клієнт:* {author}\n"
        f"⭐️ *Оцінка:* {stars} ({rev.rating})\n"
        f"💬 *Відгук:*\n«{rev.comment}»\n\n"
        f"Оберіть дію:"
    )
    
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"adm:rev:approve:{rev.id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"adm:rev:reject:{rev.id}")
    )
    b.row(InlineKeyboardButton(text="⬅️ Адмін меню", callback_data="adm:main"))
    
    await call.message.edit_text(
        text=text,
        parse_mode="Markdown",
        reply_markup=b.as_markup()
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:rev:approve:"))
async def admin_review_approve(
    call: CallbackQuery,
    db_session: AsyncSession,
    state: FSMContext,
    sheets=None
) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
        
    review_id = int(call.data.split(":")[-1])
    
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    
    try:
        query = select(Review).where(Review.id == review_id).options(selectinload(Review.user))
        result = await db_session.execute(query)
        review = result.scalar_one_or_none()
    except Exception as e:
        logger.error("failed_to_fetch_review_for_approval", id=review_id, error=str(e))
        await call.answer("❌ Помилка бази даних", show_alert=True)
        return
        
    if not review:
        await call.answer("⚠️ Відгук не знайдено", show_alert=True)
        await admin_reviews_list(call, db_session, state)
        return
        
    # Update DB
    review.is_moderated = True
    await db_session.commit()
    
    # Notify user
    try:
        await call.bot.send_message(
            chat_id=review.user.telegram_id,
            text="🎉 *Ваш відгук успішно опубліковано!* Дякуємо за зворотний зв'язок!",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning("failed_to_notify_user_on_review_approval", user_id=review.user.telegram_id, error=str(e))
        
    # Update Google Sheets
    client_name = review.user.first_name
    if review.user.last_name:
        client_name += f" {review.user.last_name}"
    await _update_sheets_review_status(sheets, client_name, review.comment, "Опубліковано")
    
    await call.answer("✅ Відгук схвалено", show_alert=True)
    # Refresh list
    await admin_reviews_list(call, db_session, state)


@router.callback_query(F.data.startswith("adm:rev:reject:"))
async def admin_review_reject(
    call: CallbackQuery,
    db_session: AsyncSession,
    state: FSMContext,
    sheets=None
) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
        
    review_id = int(call.data.split(":")[-1])
    
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    
    try:
        query = select(Review).where(Review.id == review_id).options(selectinload(Review.user))
        result = await db_session.execute(query)
        review = result.scalar_one_or_none()
    except Exception as e:
        logger.error("failed_to_fetch_review_for_rejection", id=review_id, error=str(e))
        await call.answer("❌ Помилка бази даних", show_alert=True)
        return
        
    if not review:
        await call.answer("⚠️ Відгук не знайдено", show_alert=True)
        await admin_reviews_list(call, db_session, state)
        return
        
    # Update Google Sheets first (we need review attributes before delete)
    client_name = review.user.first_name
    if review.user.last_name:
        client_name += f" {review.user.last_name}"
    comment = review.comment
    user_telegram_id = review.user.telegram_id
    
    # Delete from DB
    await db_session.delete(review)
    await db_session.commit()
    
    # Notify user
    try:
        await call.bot.send_message(
            chat_id=user_telegram_id,
            text="⚠️ *Ваш відгук відхилено модератором.*",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning("failed_to_notify_user_on_review_rejection", user_id=user_telegram_id, error=str(e))
        
    await _update_sheets_review_status(sheets, client_name, comment, "Відхилено")
    
    await call.answer("❌ Відгук відхилено", show_alert=True)
    # Refresh list
    await admin_reviews_list(call, db_session, state)


# ─── Specialist Slots Management ──────────────────────────────────────────────

def _generate_admin_calendar(psychologist_id: int, slots_dates: set[str]) -> InlineKeyboardMarkup:
    from datetime import datetime
    import calendar
    
    now = datetime.now()
    year = now.year
    month = now.month
    
    # Months in Ukrainian
    month_names = {
        1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень", 5: "Травень", 6: "Червень",
        7: "Липень", 8: "Серпень", 9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
    }
    
    b = InlineKeyboardBuilder()
    
    # Month title header row
    b.row(InlineKeyboardButton(text=f"📅 {month_names[month]} {year}", callback_data="ignore"))
    
    # Weekdays header row
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    b.row(*[InlineKeyboardButton(text=wd, callback_data="ignore") for wd in weekdays])
    
    # Get calendar grid list of weeks
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)
    
    for week in month_days:
        row_buttons = []
        for day in week:
            if day == 0:
                row_buttons.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date_str = f"{year}-{month:02d}-{day:02d}"
                # Render differently if past day
                is_past = False
                try:
                    day_dt = datetime(year, month, day)
                    if day_dt.date() < now.date():
                        is_past = True
                except ValueError:
                    pass
                    
                if is_past:
                    row_buttons.append(InlineKeyboardButton(text="·", callback_data="ignore"))
                else:
                    # Highlight if day has slots configured
                    label = f"{day}🟢" if date_str in slots_dates else str(day)
                    row_buttons.append(InlineKeyboardButton(text=label, callback_data=f"adm:slot_date:{date_str}"))
        b.row(*row_buttons)
        
    b.row(InlineKeyboardButton(text="⬅️ Адмін меню", callback_data="adm:main"))
    return b.as_markup()


async def _update_sheets_slot_status(sheets, spec_name: str, date_str: str, time_str: str, new_status: str) -> None:
    if not sheets:
        return
    try:
        rows = await sheets.read_sheet("Вільні слоти")
        found = False
        
        # Convert date format if needed (Google Sheets stores as DD.MM.YYYY)
        parsed_date_dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_dmy = parsed_date_dt.strftime("%d.%m.%Y")
        
        for idx, row in enumerate(rows):
            if idx == 0:
                continue
            row_date = str(row[0]).strip()
            row_spec = str(row[1]).strip()
            row_time = str(row[2]).strip()
            
            date_matches = (row_date == date_str or row_date == date_dmy)
            if date_matches and row_spec.lower() == spec_name.strip().lower() and row_time == time_str:
                # Update Status (column D -> index 3, column 4 is "D")
                await sheets.update_cell("Вільні слоти", idx + 1, "D", new_status)
                logger.info("sheets_slot_status_updated", row=idx + 1, status=new_status)
                found = True
                break
                
        if not found and new_status == "Вільний":
            row_data = [date_dmy, spec_name, time_str, new_status]
            await sheets.append_row("Вільні слоти", row_data)
            logger.info("sheets_slot_appended", spec=spec_name, date=date_dmy, time=time_str)
    except Exception as e:
        logger.error("failed_to_update_sheets_slot_status", error=str(e))


async def _update_sheets_room_slot_status(sheets, date_str: str, time_str: str, new_status: str) -> None:
    if not sheets:
        return
    try:
        rows = await sheets.read_sheet("ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ")
        found = False
        
        parsed_date_dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_dmy = parsed_date_dt.strftime("%d.%m.%Y")
        
        for idx, row in enumerate(rows):
            if idx == 0:
                continue
            row_date = str(row[0]).strip()
            row_time = str(row[1]).strip()
            
            date_matches = (row_date == date_str or row_date == date_dmy)
            if date_matches and row_time == time_str:
                # Update Status (column C -> index 2, column 3 is "C")
                await sheets.update_cell("ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ", idx + 1, "C", new_status)
                logger.info("sheets_room_slot_status_updated", row=idx + 1, status=new_status)
                found = True
                break
                
        if not found and new_status == "Вільний":
            row_data = [date_dmy, time_str, new_status]
            await sheets.append_row("ВІЛЬНІ СЛОТИ ОРЕНДА КАБІНЕТУ", row_data)
            logger.info("sheets_room_slot_appended", date=date_dmy, time=time_str)
    except Exception as e:
        logger.error("failed_to_update_sheets_room_slot_status", error=str(e))


@router.callback_query(F.data == "adm:slots_menu")
async def admin_slots_menu(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫 Доступ заборонено", show_alert=True)
        return
        
    await call.answer()
    await state.set_state(AdminMenuFSM.ManageSlots)
    
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="Анна Зозуля 👩‍⚕️", callback_data="adm:slot_spec:1"))
    b.row(InlineKeyboardButton(text="Вільні слоти кабінет 🏢", callback_data="adm:slot_room"))
    b.row(InlineKeyboardButton(text="⬅️ Адмін меню", callback_data="adm:main"))
    
    await call.message.edit_text(
        "📅 *Керування вільними слотами*\n\nОберіть спеціаліста або кабінет для редагування графіку:",
        parse_mode="Markdown",
        reply_markup=b.as_markup()
    )


@router.callback_query(F.data.startswith("adm:slot_spec:"))
async def admin_slot_spec_calendar(call: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
        
    await call.answer()
    psych_id = int(call.data.split(":")[-1])
    await state.update_data(psych_id=psych_id, is_room=False)
    await state.set_state(AdminMenuFSM.SelectSlotDate)
    
    from app.database.models.booking import SpecialistSlot
    query = select(SpecialistSlot.date).where(
        SpecialistSlot.psychologist_id == psych_id,
        SpecialistSlot.is_booked == False
    ).distinct()
    res = await db_session.execute(query)
    slots_dates = set(res.scalars().all())
    
    markup = _generate_admin_calendar(psych_id, slots_dates)
    await call.message.edit_text(
        "📅 *Оберіть дату в календарі для налаштування годин спеціаліста:*\n\n"
        "🟢 — позначено дні, які вже мають додані слоти.",
        parse_mode="Markdown",
        reply_markup=markup
    )


@router.callback_query(F.data == "adm:slot_room")
async def admin_room_slot_calendar(call: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
        
    await call.answer()
    await state.update_data(psych_id=None, is_room=True)
    await state.set_state(AdminMenuFSM.SelectSlotDate)
    
    from app.database.models.booking import RoomRentalSlot
    query = select(RoomRentalSlot.date).where(
        RoomRentalSlot.room_id == 1,
        RoomRentalSlot.is_booked == False
    ).distinct()
    res = await db_session.execute(query)
    slots_dates = set(res.scalars().all())
    
    markup = _generate_admin_calendar(0, slots_dates)
    await call.message.edit_text(
        "📅 *Оберіть дату в календарі для налаштування годин кабінету:*\n\n"
        "🟢 — позначено дні, які вже мають додані слоти.",
        parse_mode="Markdown",
        reply_markup=markup
    )


@router.callback_query(F.data.startswith("adm:slot_date:"), AdminMenuFSM.SelectSlotDate)
async def admin_slot_date_hours(call: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
        
    await call.answer()
    date_str = call.data.split(":")[-1]
    await state.update_data(selected_slot_date=date_str)
    
    data = await state.get_data()
    is_room = data.get("is_room", False)
    
    if is_room:
        from app.database.models.booking import RoomRentalSlot
        query = select(RoomRentalSlot).where(
            RoomRentalSlot.room_id == 1,
            RoomRentalSlot.date == date_str
        ).order_by(RoomRentalSlot.time.asc())
    else:
        psych_id = data["psych_id"]
        from app.database.models.booking import SpecialistSlot
        query = select(SpecialistSlot).where(
            SpecialistSlot.psychologist_id == psych_id,
            SpecialistSlot.date == date_str
        ).order_by(SpecialistSlot.time.asc())
    
    res = await db_session.execute(query)
    slots = res.scalars().all()
    
    b = InlineKeyboardBuilder()
    
    text_parts = [f"📅 *Налаштування слотів на {date_str}:*\n"]
    if not slots:
        text_parts.append("📭 Немає жодного активного слоту на цей день.")
    else:
        text_parts.append("Активні слоти:")
        for s in slots:
            status_label = "🔒 Заброньовано" if s.is_booked else "🟢 Вільний"
            text_parts.append(f"• *{s.time}* ({status_label})")
            
            if not s.is_booked:
                b.row(
                    InlineKeyboardButton(text=f"🗑️ Видалити {s.time}", callback_data=f"adm:slot_del:{s.id}")
                )
            else:
                b.row(
                    InlineKeyboardButton(text=f"🔒 {s.time} (Зайнято)", callback_data="ignore")
                )
                
    b.row(InlineKeyboardButton(text="➕ Додати новий слот", callback_data="adm:slot_add_time"))
    
    if is_room:
        b.row(InlineKeyboardButton(text="⬅️ Назад до календаря", callback_data="adm:slot_room"))
    else:
        psych_id = data["psych_id"]
        b.row(InlineKeyboardButton(text="⬅️ Назад до календаря", callback_data=f"adm:slot_spec:{psych_id}"))
    
    await call.message.edit_text(
        text="\n".join(text_parts),
        parse_mode="Markdown",
        reply_markup=b.as_markup()
    )


@router.callback_query(F.data.startswith("adm:slot_del:"))
async def admin_slot_delete(call: CallbackQuery, state: FSMContext, db_session: AsyncSession, sheets=None) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
        
    slot_id = int(call.data.split(":")[-1])
    data = await state.get_data()
    is_room = data.get("is_room", False)
    
    if is_room:
        from app.database.models.booking import RoomRentalSlot
        query = select(RoomRentalSlot).where(RoomRentalSlot.id == slot_id)
        res = await db_session.execute(query)
        slot = res.scalar_one_or_none()
        
        if not slot:
            await call.answer("⚠️ Слот не знайдено!", show_alert=True)
            return
            
        if slot.is_booked:
            await call.answer("🚫 Не можна видалити вже заброньований слот!", show_alert=True)
            return
            
        date_str = slot.date
        time_str = slot.time
        
        await db_session.delete(slot)
        await db_session.commit()
        
        await _update_sheets_room_slot_status(sheets, date_str, time_str, "Видалено")
        
        await call.answer("🗑️ Слот успішно видалено", show_alert=True)
    else:
        from app.database.models.booking import SpecialistSlot
        from app.database.models.psychologist import Psychologist
        
        query = select(SpecialistSlot).where(SpecialistSlot.id == slot_id).options(selectinload(SpecialistSlot.psychologist))
        res = await db_session.execute(query)
        slot = res.scalar_one_or_none()
        
        if not slot:
            await call.answer("⚠️ Слот не знайдено!", show_alert=True)
            return
            
        if slot.is_booked:
            await call.answer("🚫 Не можна видалити вже заброньований слот!", show_alert=True)
            return
            
        spec_name = slot.psychologist.name
        date_str = slot.date
        time_str = slot.time
        
        await db_session.delete(slot)
        await db_session.commit()
        
        await _update_sheets_slot_status(sheets, spec_name, date_str, time_str, "Видалено")
        
        await call.answer("🗑️ Слот успішно видалено", show_alert=True)
        
    call.data = f"adm:slot_date:{date_str}"
    await admin_slot_date_hours(call, state, db_session)


@router.callback_query(F.data == "adm:slot_add_time")
async def admin_slot_add_time_start(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer("🚫", show_alert=True)
        return
        
    await call.answer()
    await state.set_state(AdminMenuFSM.EnterSlotTime)
    
    data = await state.get_data()
    date_str = data["selected_slot_date"]
    
    sent_msg = await call.message.edit_text(
        f"✍️ *Додавання слоту на {date_str}*\n\n"
        f"Будь ласка, введіть час у форматі *ГГ:ХХ* (наприклад, `10:00`, `14:30`, `18:00`):",
        parse_mode="Markdown"
    )
    await state.update_data(prompt_message_id=sent_msg.message_id)


@router.message(AdminMenuFSM.EnterSlotTime)
async def admin_slot_add_time_process(message: Message, state: FSMContext, db_session: AsyncSession, sheets=None) -> None:
    if not _is_admin(message.from_user.id):
        return
        
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")
    date_str = data["selected_slot_date"]
    is_room = data.get("is_room", False)
    
    try:
        await message.delete()
    except Exception:
        pass
        
    time_input = message.text.strip() if message.text else ""
    import re
    if not re.match(r"^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$", time_input):
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_message_id,
                text=f"⚠️ *Неправильний формат часу!*\n\n"
                     f"✍️ *Додавання слоту на {date_str}*\n\n"
                     f"Будь ласка, введіть час у форматі *ГГ:ХХ* (наприклад, `10:00`):",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return
        
    if is_room:
        from app.database.models.booking import RoomRentalSlot
        
        dup_query = select(RoomRentalSlot).where(
            RoomRentalSlot.room_id == 1,
            RoomRentalSlot.date == date_str,
            RoomRentalSlot.time == time_input
        )
        dup_res = await db_session.execute(dup_query)
        dup = dup_res.scalar_one_or_none()
        
        if dup:
            if dup.is_booked:
                dup.is_booked = False
                await db_session.commit()
            else:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=prompt_message_id,
                        text=f"⚠️ *Такий слот вже існує!*\n\n"
                             f"✍️ *Додавання слоту на {date_str}*\n\n"
                             f"Будь ласка, введіть час у форматі *ГГ:ХХ* (наприклад, `10:00`):",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                return
        else:
            new_slot = RoomRentalSlot(
                room_id=1,
                date=date_str,
                time=time_input,
                is_booked=False
            )
            db_session.add(new_slot)
            await db_session.commit()
            
        await _update_sheets_room_slot_status(sheets, date_str, time_input, "Вільний")
    else:
        psych_id = data["psych_id"]
        from app.database.models.booking import SpecialistSlot
        from app.database.models.psychologist import Psychologist
        
        dup_query = select(SpecialistSlot).where(
            SpecialistSlot.psychologist_id == psych_id,
            SpecialistSlot.date == date_str,
            SpecialistSlot.time == time_input
        )
        dup_res = await db_session.execute(dup_query)
        dup = dup_res.scalar_one_or_none()
        
        if dup:
            if dup.is_booked:
                dup.is_booked = False
                await db_session.commit()
            else:
                try:
                    await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=prompt_message_id,
                        text=f"⚠️ *Такий слот вже існує!*\n\n"
                             f"✍️ *Додавання слоту на {date_str}*\n\n"
                             f"Будь ласка, введіть час у форматі *ГГ:ХХ* (наприклад, `10:00`):",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                return
        else:
            new_slot = SpecialistSlot(
                psychologist_id=psych_id,
                date=date_str,
                time=time_input,
                is_booked=False
            )
            db_session.add(new_slot)
            await db_session.commit()
            
        psych_query = select(Psychologist).where(Psychologist.id == psych_id)
        psych_res = await db_session.execute(psych_query)
        psych = psych_res.scalar_one()
        
        await _update_sheets_slot_status(sheets, psych.name, date_str, time_input, "Вільний")
        
    await state.set_state(AdminMenuFSM.SelectSlotDate)
    
    from aiogram.types import CallbackQuery
    fake_call = CallbackQuery(
        id="0",
        from_user=message.from_user,
        chat_instance="0",
        message=message,
        data=f"adm:slot_date:{date_str}"
    )
    fake_call.message.message_id = prompt_message_id
    
    await admin_slot_date_hours(fake_call, state, db_session)

