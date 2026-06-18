# app/bot/handlers/booking.py
import re
import asyncio
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from structlog import get_logger

from aiogram.exceptions import TelegramBadRequest
from app.bot.states.booking import BookingFSM
from app.services.booking import BookingService
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.bot.keyboards.inline import get_format_keyboard, get_slots_keyboard, get_payment_keyboard, get_main_menu_keyboard

logger = get_logger()
router = Router(name="booking_router")

# Strict Ukrainian Phone format validation (+380XXXXXXXXX)
PHONE_REGEX = re.compile(r"^\+380\d{9}$")

@router.callback_query(F.data == "menu:consultations")
async def start_booking_flow(call: CallbackQuery, state: FSMContext) -> None:
    """Entry point for consultation bookings. Prompts choice of therapists."""
    # Seed list of mock therapists for MVP selection
    therapists_text = (
        "🧠 *Наші спеціалісти:*\n\n"
        "1. *Анна Зозуля* — Засновниця, психолог\n\n"
        "✨ _Оберіть свого терапевта для запису:_"
    )
    
    # Inline selection panel
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Анна Зозуля 👩‍⚕️", callback_data="select_psych:1")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Головне меню", callback_data="menu:home"))
    
    if call.message.photo or call.message.document:
        sent_msg = await call.message.answer(
            text=therapists_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        await state.update_data(main_msg_id=sent_msg.message_id)
        try:
            await call.message.delete()
        except Exception:
            pass
    else:
        await state.update_data(main_msg_id=call.message.message_id)
        try:
            await call.message.edit_text(
                text=therapists_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
        except Exception:
            pass
    await state.set_state(BookingFSM.SelectPsychologist)
    await call.answer()

@router.callback_query(F.data.startswith("select_psych:"), BookingFSM.SelectPsychologist)
async def process_psych_selection(call: CallbackQuery, state: FSMContext) -> None:
    """Processes therapist selection and proceeds to format choice."""
    psych_id = int(call.data.split(":")[1])
    await state.update_data(psych_id=psych_id)
    
    await call.message.edit_text(
        text="✨ *Оберіть бажаний формат консультації:*",
        parse_mode="Markdown",
        reply_markup=get_format_keyboard()
    )
    await state.set_state(BookingFSM.SelectFormat)
    await call.answer()

def _generate_client_calendar(psychologist_id: int, active_dates: set[str]) -> InlineKeyboardMarkup:
    from datetime import datetime
    import calendar
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    from aiogram.types import InlineKeyboardMarkup
    
    now = datetime.now()
    year = now.year
    month = now.month
    
    # Months in Ukrainian
    month_names = {
        1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень", 5: "Травень", 6: "Червень",
        7: "Липень", 8: "Серпень", 9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
    }
    
    b = InlineKeyboardBuilder()
    
    # Month title
    b.row(InlineKeyboardButton(text=f"📅 {month_names[month]} {year}", callback_data="ignore"))
    
    # Weekdays header row
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
    b.row(*[InlineKeyboardButton(text=wd, callback_data="ignore") for wd in weekdays])
    
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)
    
    for week in month_days:
        row_buttons = []
        for day in week:
            if day == 0:
                row_buttons.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date_str = f"{year}-{month:02d}-{day:02d}"
                is_active = date_str in active_dates
                
                # Check if day is past
                is_past = False
                try:
                    day_dt = datetime(year, month, day)
                    if day_dt.date() < now.date():
                        is_past = True
                except ValueError:
                    pass
                
                if is_active and not is_past:
                    row_buttons.append(InlineKeyboardButton(text=str(day), callback_data=f"date:{date_str}"))
                else:
                    row_buttons.append(InlineKeyboardButton(text="·", callback_data="ignore"))
        b.row(*row_buttons)
        
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back:format"))
    return b.as_markup()


@router.callback_query(F.data.startswith("format:"), BookingFSM.SelectFormat)
async def process_format_selection(call: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    """Processes choice of Online vs Offline consultation."""
    from sqlalchemy import select
    from app.database.models.booking import SpecialistSlot
    from datetime import datetime
    
    format_type = call.data.split(":")[1]
    await state.update_data(format=format_type)
    
    data = await state.get_data()
    psych_id = data["psych_id"]
    
    now = datetime.now()
    year = now.year
    month = now.month
    
    # Select available dates for this psychologist in current month
    prefix = f"{year}-{month:02d}-"
    query = select(SpecialistSlot.date).where(
        SpecialistSlot.psychologist_id == psych_id,
        SpecialistSlot.is_booked == False,
        SpecialistSlot.date.like(f"{prefix}%")
    ).distinct()
    
    res = await db_session.execute(query)
    active_dates = set(res.scalars().all())
    
    markup = _generate_client_calendar(psych_id, active_dates)
    
    await call.message.edit_text(
        text="📅 *Оберіть зручну дату для зустрічі:*\n\n"
             "_(активні дати клікабельні, неактивні позначені крапкою)_",
        parse_mode="Markdown",
        reply_markup=markup
    )
    await state.set_state(BookingFSM.SelectDate)
    await call.answer()

@router.callback_query(F.data.startswith("date:"), BookingFSM.SelectDate)
async def process_date_selection(
    call: CallbackQuery, 
    state: FSMContext, 
    booking_service: BookingService
) -> None:
    """Fetches slots for selected date and prompts time choice."""
    selected_date_str = call.data.split(":")[1] # YYYY-MM-DD
    state_data = await state.get_data()
    
    # Query Google Calendar synchronized slots via Booking Service
    slots = await booking_service.get_available_slots(
        psychologist_id=state_data["psych_id"],
        date_str=selected_date_str
    )
    
    if not slots:
        await call.answer("⚠️ На цю дату немає вільних слотів! Спробуйте іншу.", show_alert=True)
        return
        
    await state.update_data(selected_date=selected_date_str)
    await call.message.edit_text(
        text=f"⏰ *Доступний час на {selected_date_str}:*",
        parse_mode="Markdown",
        reply_markup=get_slots_keyboard(slots)
    )
    await state.set_state(BookingFSM.SelectSlot)
    await call.answer()

async def delete_message_after_delay(message: Message, delay: int = 10) -> None:
    """Helper to delete a message after a specified delay in seconds."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

@router.callback_query(F.data.startswith("slot:"), BookingFSM.SelectSlot)
async def process_slot_selection(
    call: CallbackQuery, 
    state: FSMContext, 
    booking_service: BookingService
) -> None:
    """Locks the selected time slot and asks for user's full name."""
    slot_time = call.data[5:] # Extract HH:MM directly to preserve full value (e.g., slot:09:00 -> 09:00)
    state_data = await state.get_data()
    user_id = call.from_user.id
    
    # Distributed Redis Lock to prevent race conditions
    locked = await booking_service.lock_time_slot(
        psychologist_id=state_data["psych_id"],
        date_str=state_data["selected_date"],
        time_str=slot_time,
        user_id=user_id
    )
    
    if not locked:
        await call.answer("❌ Цей слот щойно забронював інший користувач. Спробуйте інший час.", show_alert=True)
        return
        
    await state.update_data(
        selected_time=slot_time,
        prompt_message_id=call.message.message_id
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    cancel_builder = InlineKeyboardBuilder()
    cancel_builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))

    await call.message.edit_text(
        text="👤 *Будь ласка, введіть Ваше Ім'я та Прізвище:*",
        parse_mode="Markdown",
        reply_markup=cancel_builder.as_markup()
    )
    await state.set_state(BookingFSM.EnterName)
    await call.answer()

@router.message(BookingFSM.EnterName)
async def process_name(message: Message, state: FSMContext) -> None:
    """Saves user name and prompts for phone contact."""
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    # Delete the user's typed name message immediately
    try:
        await message.delete()
    except Exception:
        pass

    # Handle explicit cancellation commands/words typed by user
    if message.text and message.text.strip().lower() in ["скасувати", "cancel", "❌ скасувати"]:
        await state.clear()
        await message.answer(
            text="🌿 Оберіть бажану опцію меню нижче:",
            reply_markup=get_main_menu_keyboard(user_id=message.from_user.id)
        )
        if main_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=main_msg_id)
            except Exception:
                pass
        temp_msg = await message.answer("❌ Бронювання скасовано.")
        asyncio.create_task(delete_message_after_delay(temp_msg, 10))
        return

    if not message.text or len(message.text.strip()) < 3:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                text="⚠️ *Введіть коректне ім'я та прізвище (мінімум 3 символи):*\n\n"
                     "👤 *Будь ласка, введіть Ваше Ім'я та Прізвище:*",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return
        
    from aiogram.utils.keyboard import ReplyKeyboardBuilder
    from aiogram.types import KeyboardButton
    
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Надіслати контакт", request_contact=True))
    builder.row(KeyboardButton(text="❌ Скасувати"))
    
    await state.update_data(client_name=message.text.strip())
    
    sent_msg = await message.answer(
        text="📞 *Надішліть Ваш номер телефону за допомогою кнопки нижче або введіть його вручну у форматі +380XXXXXXXXX:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )
    await state.update_data(phone_prompt_msg_id=sent_msg.message_id)
    await state.set_state(BookingFSM.EnterPhone)

@router.message(BookingFSM.EnterPhone)
async def process_phone(
    message: Message, 
    state: FSMContext, 
    booking_service: BookingService,
    current_user: User
) -> None:
    """Validates phone number format and displays invoice checkout details."""
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    phone_prompt_msg_id = data.get("phone_prompt_msg_id")

    # Delete the user's typed phone message or contact immediately
    try:
        await message.delete()
    except Exception:
        pass

    # Handle explicit cancellation request
    if message.text and message.text.strip().lower() in ["скасувати", "cancel", "❌ скасувати"]:
        await state.clear()
        
        from aiogram.types import ReplyKeyboardRemove
        dummy = await message.answer("⏳", reply_markup=ReplyKeyboardRemove())
        try:
            await dummy.delete()
        except Exception:
            pass
            
        await message.answer(
            text="🌿 Оберіть бажану опцію меню нижче:",
            reply_markup=get_main_menu_keyboard(user_id=message.from_user.id)
        )
        if main_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=main_msg_id)
            except Exception:
                pass
        if phone_prompt_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=phone_prompt_msg_id)
            except Exception:
                pass
        temp_msg = await message.answer("❌ Бронювання скасовано.")
        asyncio.create_task(delete_message_after_delay(temp_msg, 10))
        return

    phone = None
    if message.contact:
        phone = message.contact.phone_number
        if not phone.startswith("+"):
            phone = f"+{phone}"
    elif message.text:
        phone = message.text.strip()
        
    if not phone or not PHONE_REGEX.match(phone):
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                text="⚠️ *Номер телефону не відповідає формату!*\n\n"
                     "👤 *Ім'я:* " + data["client_name"] + "\n"
                     "Будь ласка, введіть телефон у форматі `+380XXXXXXXXX`",
                parse_mode="Markdown"
            )
        except Exception:
            pass
            
        from aiogram.utils.keyboard import ReplyKeyboardBuilder
        from aiogram.types import KeyboardButton
        builder = ReplyKeyboardBuilder()
        builder.add(KeyboardButton(text="📱 Надіслати контакт", request_contact=True))
        builder.row(KeyboardButton(text="❌ Скасувати"))
        
        sent_msg = await message.answer(
            text="📞 *Надішліть Ваш номер телефону за допомогою кнопки нижче або введіть його вручну:*",
            parse_mode="Markdown",
            reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
        )
        if phone_prompt_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=phone_prompt_msg_id)
            except Exception:
                pass
        await state.update_data(phone_prompt_msg_id=sent_msg.message_id)
        return

    await state.update_data(client_phone=phone)
    data = await state.get_data()
    
    # Calculate price based on format and psychologist rates
    price_info = await booking_service.calculate_price(
        psychologist_id=data["psych_id"],
        booking_format=data["format"]
    )
    
    # Generate dynamic invoices URLs from provider APIs
    invoice_url, invoice_id = await booking_service.create_consultation_invoice(
        user_id=current_user.id,
        psychologist_id=data["psych_id"],
        format_type=data["format"],
        date_str=data["selected_date"],
        time_str=data["selected_time"],
        price=price_info,
        client_name=data["client_name"],
        client_phone=phone
    )
    
    from aiogram.types import ReplyKeyboardRemove
    await state.update_data(invoice_id=invoice_id)
    
    # Remove reply keyboard
    dummy = await message.answer("⏳", reply_markup=ReplyKeyboardRemove())
    try:
        await dummy.delete()
    except Exception:
        pass

    summary_text = (
        f"🎯 *Підтвердження бронювання:*\n\n"
        f"👤 Клієнт: {data['client_name']}\n"
        f"📞 Телефон: {phone}\n"
        f"🕒 Дата й час: {data['selected_date']} о {data['selected_time']}\n"
        f"💵 Загальна вартість: *{price_info:.2f} UAH*\n"
        f"💳 Передплата: *50.00 UAH* (решта {price_info - 50.00:.2f} UAH сплачується при зустрічі)\n\n"
        f"⚠️ *Слот зарезервовано на 15 хвилин для внесення передплати.*"
    )

    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url)
        )
    except Exception:
        await message.answer(
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url)
        )
    
    # Delete the previous bot prompt only AFTER the final message is sent
    if phone_prompt_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=phone_prompt_msg_id)
        except Exception:
            pass

    await state.set_state(BookingFSM.ConfirmAndPay)

@router.callback_query(F.data == "booking:cancel")
async def cancel_booking_checkout(call: CallbackQuery, state: FSMContext) -> None:
    """Gracefully handles checkout abandonment and FSM clearance."""
    data = await state.get_data()
    phone_prompt_msg_id = data.get("phone_prompt_msg_id")
    prompt_message_id = data.get("prompt_message_id")
    
    # 1. Delete temporary prompt messages
    if phone_prompt_msg_id:
        try:
            await call.message.bot.delete_message(chat_id=call.message.chat.id, message_id=phone_prompt_msg_id)
        except Exception:
            pass
    if prompt_message_id:
        try:
            await call.message.bot.delete_message(chat_id=call.message.chat.id, message_id=prompt_message_id)
        except Exception:
            pass
            
    # 2. Remove reply keyboard if active
    from aiogram.types import ReplyKeyboardRemove
    try:
        dummy = await call.message.answer("⏳", reply_markup=ReplyKeyboardRemove())
        await dummy.delete()
    except Exception:
        pass

    # Reset FSM state
    await state.clear()
    
    # Edit or send the main menu message depending on whether it's a photo or text message
    if call.message.photo:
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(
            text="🌿 Оберіть бажану опцію меню нижче:",
            reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
        )
    else:
        try:
            await call.message.edit_text(
                text="🌿 Оберіть бажану опцію меню нижче:",
                reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
            )
        except Exception:
            await call.message.answer(
                text="🌿 Оберіть бажану опцію меню нижче:",
                reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
            )
    
    # Send a temporary notice that will delete itself in 10 seconds (or 3 seconds for HostEventFSM)
    current_state = await state.get_state()
    delay = 3 if (current_state and "HostEventFSM" in current_state) else 10
    try:
        temp_msg = await call.message.answer("❌ Скасовано.")
        asyncio.create_task(delete_message_after_delay(temp_msg, delay))
    except Exception:
        pass
    
    await call.answer()


@router.callback_query(F.data == "menu:home")
async def return_home_callback(call: CallbackQuery, state: FSMContext) -> None:
    """Returns to home state."""
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")
    phone_prompt_msg_id = data.get("phone_prompt_msg_id")
    
    # 1. Delete previous bot prompts
    if prompt_message_id and prompt_message_id != call.message.message_id:
        try:
            await call.message.bot.delete_message(chat_id=call.message.chat.id, message_id=prompt_message_id)
        except Exception:
            pass
    if phone_prompt_msg_id:
        try:
            await call.message.bot.delete_message(chat_id=call.message.chat.id, message_id=phone_prompt_msg_id)
        except Exception:
            pass
            
    # 2. Remove reply keyboard if active
    from aiogram.types import ReplyKeyboardRemove
    try:
        dummy = await call.message.answer("⏳", reply_markup=ReplyKeyboardRemove())
        await dummy.delete()
    except Exception:
        pass
            
    await state.clear()
    welcome_text = "🌿 Оберіть бажану опцію меню нижче:"
    try:
        if call.message.photo or call.message.document:
            await call.message.answer(
                text=welcome_text,
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
            )
            try:
                await call.message.delete()
            except Exception:
                pass
        else:
            await call.message.edit_text(
                text=welcome_text,
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
            )
    except Exception:
        pass
    await call.answer()

@router.callback_query(F.data == "back:psychologists")
async def back_to_psychologists(call: CallbackQuery, state: FSMContext) -> None:
    await start_booking_flow(call, state)

@router.callback_query(F.data == "back:format")
async def back_to_format(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    psych_id = data.get("psych_id")
    if psych_id:
        # Avoid modifying frozen Pydantic model field (call.data)
        # Instead, invoke the target handler directly after updating the state
        await state.update_data(psych_id=psych_id)
        await call.message.edit_text(
            text="✨ *Оберіть бажаний формат консультації:*",
            parse_mode="Markdown",
            reply_markup=get_format_keyboard()
        )
        await state.set_state(BookingFSM.SelectFormat)
        await call.answer()
    else:
        await start_booking_flow(call, state)

@router.callback_query(F.data == "back:date")
async def back_to_date(call: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    data = await state.get_data()
    format_type = data.get("format")
    if format_type:
        from sqlalchemy import select
        from app.database.models.booking import SpecialistSlot
        from datetime import datetime
        
        psych_id = data["psych_id"]
        now = datetime.now()
        year = now.year
        month = now.month
        
        # Select available dates for this psychologist in current month
        prefix = f"{year}-{month:02d}-"
        query = select(SpecialistSlot.date).where(
            SpecialistSlot.psychologist_id == psych_id,
            SpecialistSlot.is_booked == False,
            SpecialistSlot.date.like(f"{prefix}%")
        ).distinct()
        
        res = await db_session.execute(query)
        active_dates = set(res.scalars().all())
        
        markup = _generate_client_calendar(psych_id, active_dates)
        
        await call.message.edit_text(
            text="📅 *Оберіть зручну дату для зустрічі:*\n\n"
                 "_(активні дати клікабельні, неактивні позначені крапкою)_",
            parse_mode="Markdown",
            reply_markup=markup
        )
        await state.set_state(BookingFSM.SelectDate)
        await call.answer()
    else:
        await start_booking_flow(call, state)
