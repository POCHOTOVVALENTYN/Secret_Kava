# app/bot/keyboards/inline.py
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Telegram IDs of admin users who can see the admin panel button
ADMIN_IDS: set[int] = {660331103, 830196453}

def get_main_menu_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Returns the main startup keyboard interface for bot menu controls.
    If user_id belongs to an admin, shows the hidden 🔐 Admin button.
    """
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text="🏢 Про простір", callback_data="menu:about"),
        InlineKeyboardButton(text="🧠 Консультації", callback_data="menu:consultations")
    )
    builder.row(
        InlineKeyboardButton(text="🛋️ Оренда кабінету", callback_data="menu:rent_room"),
        InlineKeyboardButton(text="🎭 Оренда залу для заходів", callback_data="menu:host_event")
    )
    builder.row(
        InlineKeyboardButton(text="🍷 Жіноче коло", callback_data="menu:womens_circle"),
        InlineKeyboardButton(text="📅 Афіша заходів", callback_data="menu:events")
    )
    builder.row(
        InlineKeyboardButton(text="⭐ Відгуки клієнтів", callback_data="menu:reviews"),
        InlineKeyboardButton(text="📞 Контакти", callback_data="menu:contacts")
    )
    
    # Show Admin Panel button only to authorised admin users
    if user_id and user_id in ADMIN_IDS:
        builder.row(
            InlineKeyboardButton(text="🔐 Адмін меню", callback_data="menu:admin")
        )
    
    return builder.as_markup()

def get_format_keyboard() -> InlineKeyboardMarkup:
    """Returns consultation format options."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="💻 Онлайн (Zoom/Google Meet)", callback_data="format:online"),
        InlineKeyboardButton(text="🛋️ Офлайн (Студія/Кабінет)", callback_data="format:offline")
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад до спеціалістів", callback_data="back:psychologists")
    )
    return builder.as_markup()

def get_slots_keyboard(slots: list[str]) -> InlineKeyboardMarkup:
    """Arranges dynamic list of time slots in a 3-column structured grid."""
    builder = InlineKeyboardBuilder()
    
    for slot in slots:
        builder.add(InlineKeyboardButton(text=f"⏰ {slot}", callback_data=f"slot:{slot}"))
        
    builder.adjust(3)  # Forces 3 slot columns
    builder.row(InlineKeyboardButton(text="⬅️ Назад до дат", callback_data="back:date"))
    
    return builder.as_markup()

def get_payment_keyboard(invoice_url: str, amount: float = 1.0) -> InlineKeyboardMarkup:
    """Provides dynamic checkout URL triggers and cancellation flow."""
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text=f"💳 Сплатити передплату {int(amount)} грн (WayForPay)", url=invoice_url)
    )
    builder.row(
        InlineKeyboardButton(text="❌ Скасувати бронювання", callback_data="booking:cancel")
    )
    
    return builder.as_markup()

def get_nps_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    """Builds a robust rating grid row (1 to 10 score) for customer feedback triggers."""
    builder = InlineKeyboardBuilder()
    
    # Grid adjustments
    for score in range(1, 11):
        builder.add(InlineKeyboardButton(text=str(score), callback_data=f"nps:{booking_id}:{score}"))
        
    builder.adjust(5) # Splits 10 buttons nicely across 2 rows of 5
    return builder.as_markup()

def get_payment_retry_direct_keyboard(payment_id: int, invoice_url: str, amount: float = 1.0) -> InlineKeyboardMarkup:
    """Provides a payment button pointing to a retry invoice URL and a cancel button referencing the payment ID."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"💳 Сплатити передплату {int(amount)} грн (WayForPay)", url=invoice_url)
    )
    builder.row(
        InlineKeyboardButton(text="❌ Скасувати бронювання", callback_data=f"pay_cancel:{payment_id}")
    )
    return builder.as_markup()

