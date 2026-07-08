# app/bot/handlers/rental.py
import os
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, FSInputFile
from aiogram.fsm.context import FSMContext
from structlog import get_logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.states.booking import RoomRentalFSM, SpaceLeaseFSM
from app.services.booking import BookingService
from app.database.models.user import User
from app.bot.keyboards.inline import get_payment_keyboard, get_main_menu_keyboard

logger = get_logger()
router = Router(name="rental_router")

import time

_ROOMS_CACHE = {
    "data": None,
    "last_updated": None
}
CACHE_TTL_SECONDS = 10
_OFFICE_PHOTO_FILE_ID = None
_SPACE_PHOTO_FILE_ID = None




@router.callback_query(F.data == "menu:rent_room")
async def start_rental_flow(call: CallbackQuery, state: FSMContext, sheets=None) -> None:
    """Entry point for room rentals. Displays offices galleries."""
    await call.answer()
    
    global _ROOMS_CACHE
    now = time.time()
    rooms_data = None
    
    if _ROOMS_CACHE["data"] and _ROOMS_CACHE["last_updated"] and (now - _ROOMS_CACHE["last_updated"] < CACHE_TTL_SECONDS):
        rooms_data = _ROOMS_CACHE["data"]
        logger.info("loaded_rooms_from_cache")
        
    # If not cached, we need to show a loader and fetch it
    if not rooms_data:
        # Show loading status immediately without deleting the message if possible
        loading_text = "⏳ *Завантажуємо інформацію про кабінети... Будь ласка, зачекайте.*"
        if call.message.photo or call.message.video or call.message.document:
            try:
                await call.message.edit_caption(
                    caption=loading_text,
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        else:
            try:
                await call.message.edit_text(
                    text=loading_text,
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        # Default fallback room data
        rooms_data = [
            {
                "id": 1,
                "name": "Головний кабінет",
                "desc": "м'які терапевтичні крісла, торшер, фліпчарт, папір",
                "hourly": 200.0,
                "evening": 150.0
            }
        ]
        
        if sheets:
            try:
                rows = await sheets.read_sheet("Реєстр Кабінетів")
                if len(rows) > 1:
                    loaded_rooms = []
                    for r in rows[1:]:
                        if len(r) >= 5:
                            r_name = r[1]
                            if not r_name or r_name == "—":
                                continue
                            r_id = int(r[0]) if r[0].isdigit() else 1
                            r_desc = r[2]
                            r_hourly = float(r[3]) if r[3] else 200.0
                            r_evening = float(r[4]) if r[4] else 150.0
                            
                            # Check active status if present
                            is_active = True
                            if len(r) > 5:
                                is_active = (r[5].strip().upper() in ("ТАК", "YES", "TRUE", "1", ""))
                                
                            if is_active:
                                loaded_rooms.append({
                                    "id": r_id,
                                    "name": r_name,
                                    "desc": r_desc,
                                    "hourly": r_hourly,
                                    "evening": r_evening
                                })
                    if loaded_rooms:
                        rooms_data = loaded_rooms
                        _ROOMS_CACHE["data"] = rooms_data
                        _ROOMS_CACHE["last_updated"] = now
                        logger.info("rooms_cache_updated")
            except Exception as e:
                logger.error("error_loading_rooms_from_sheets", error=str(e))

    # Build dynamic description text
    text_parts = ["🛋️ *Наш затишний кабінет для оренди:*\n"]
    for rd in rooms_data:
        text_parts.append(
            f"• *{rd['name']}* — {rd['desc']}\n"
            f"  Вартість: *{rd['hourly']:.0f} UAH/год*\n"
            f"  Вечірній тариф (з 18:00 до 20:00): *{rd['evening']:.0f} UAH/год*\n"
        )
    text_parts.append("🎁 *Знижка 10% при бронюванні від 3-х годин поспіль!*")
    offices_text = "\n".join(text_parts)
    
    # Inline offices selector
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    for rd in rooms_data:
        builder.row(
            InlineKeyboardButton(text=f"{rd['name']} 🛋️", callback_data=f"select_room:{rd['id']}")
        )
    builder.row(InlineKeyboardButton(text="⬅️ Головне меню", callback_data="menu:home"))
    
    markup = builder.as_markup()

    # If the message already has a photo/caption, edit it seamlessly
    if call.message.photo or call.message.document:
        try:
            await call.message.edit_caption(
                caption=offices_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            await state.update_data(main_msg_id=call.message.message_id)
            await state.set_state(RoomRentalFSM.SelectRoom)
            return
        except Exception:
            pass

    global _OFFICE_PHOTO_FILE_ID
    # Try sending photo or video/animation first if they exist
    import os
    from aiogram.types import FSInputFile
    photo_path = "app/bot/assets/office_photo.jpg"
    video_path = "app/bot/assets/rental_video.gif"
    
    async def clean_old_msg():
        try:
            await call.message.delete()
        except Exception:
            pass

    if _OFFICE_PHOTO_FILE_ID:
        try:
            sent_msg = await call.message.answer_photo(
                photo=_OFFICE_PHOTO_FILE_ID,
                caption=offices_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            await state.update_data(main_msg_id=sent_msg.message_id)
            await clean_old_msg()
            await state.set_state(RoomRentalFSM.SelectRoom)
            return
        except Exception as e:
            logger.warning("failed_to_send_rental_photo_via_cached_file_id_falling_back", error=str(e))

    if os.path.exists(photo_path):
        try:
            photo_file = FSInputFile(photo_path)
            sent_msg = await call.message.answer_photo(
                photo=photo_file,
                caption=offices_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            if sent_msg.photo:
                _OFFICE_PHOTO_FILE_ID = sent_msg.photo[-1].file_id
                logger.info("stored_office_photo_file_id", file_id=_OFFICE_PHOTO_FILE_ID)
            await state.update_data(main_msg_id=sent_msg.message_id)
            await clean_old_msg()
            await state.set_state(RoomRentalFSM.SelectRoom)
            return
        except Exception as e:
            logger.error("failed_to_send_rental_photo", error=str(e))
    elif os.path.exists(video_path):
        try:
            video_file = FSInputFile(video_path)
            sent_msg = await call.message.answer_animation(
                animation=video_file,
                caption=offices_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            await state.update_data(main_msg_id=sent_msg.message_id)
            await clean_old_msg()
            await state.set_state(RoomRentalFSM.SelectRoom)
            return
        except Exception as e:
            logger.error("failed_to_send_rental_animation", error=str(e))

    # Fallback to text edit or answer
    try:
        await call.message.edit_text(
            text=offices_text,
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception:
        sent_msg = await call.message.answer(
            text=offices_text,
            parse_mode="Markdown",
            reply_markup=markup
        )
        await state.update_data(main_msg_id=sent_msg.message_id)
        await clean_old_msg()
            
    await state.set_state(RoomRentalFSM.SelectRoom)


def _generate_room_calendar(active_dates: set[str]) -> InlineKeyboardMarkup:
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
                    row_buttons.append(InlineKeyboardButton(text=str(day), callback_data=f"rent_date:{date_str}"))
                else:
                    row_buttons.append(InlineKeyboardButton(text="·", callback_data="ignore"))
        b.row(*row_buttons)
        
    b.row(InlineKeyboardButton(text="⬅️ Назад до кабінетів", callback_data="menu:rent_room"))
    return b.as_markup()


@router.callback_query(F.data.startswith("select_room:"), RoomRentalFSM.SelectRoom)
async def process_room_selection(
    call: CallbackQuery, 
    state: FSMContext, 
    db_session: AsyncSession,
    booking_service: BookingService,
    sheets=None
) -> None:
    """Processes room choice and requests booking date."""
    room_id = int(call.data.split(":")[1])
    
    # Show loading status immediately by editing the existing message caption or text
    loading_text = "⏳ *Завантажуємо актуальний розклад... Будь ласка, зачекайте.*"
    target_msg = call.message
    
    if call.message.photo or call.message.video or call.message.document:
        try:
            await call.message.edit_caption(
                caption=loading_text,
                parse_mode="Markdown"
            )
        except Exception:
            pass
    else:
        try:
            await call.message.edit_text(
                text=loading_text,
                parse_mode="Markdown"
            )
        except Exception:
            pass
            
    await call.answer()
    
    room_name = "Головний кабінет"
    
    global _ROOMS_CACHE
    if _ROOMS_CACHE["data"]:
        for rd in _ROOMS_CACHE["data"]:
            if rd["id"] == room_id:
                room_name = rd["name"]
                break
    else:
        if sheets:
            try:
                rows = await sheets.read_sheet("Реєстр Кабінетів")
                for r in rows[1:]:
                    if len(r) >= 2 and r[0].isdigit() and int(r[0]) == room_id:
                        room_name = r[1]
                        break
            except Exception:
                pass
            
    await state.update_data(room_id=room_id, room_name=room_name)
    
    now = datetime.now()
    active_dates = await booking_service.get_available_room_dates(room_id, now.year, now.month)
    
    markup = _generate_room_calendar(active_dates)
    
    # Replace the loading message with the calendar message
    msg_text = "📅 *Оберіть бажану дату оренди:*\n\n_(активні дати клікабельні, неактивні позначені крапкою)_"
    if target_msg.photo or target_msg.video or target_msg.document:
        try:
            await target_msg.edit_caption(
                caption=msg_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
        except Exception:
            try:
                sent_msg = await call.message.answer(
                    text=msg_text,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
                await state.update_data(main_msg_id=sent_msg.message_id)
                await call.message.delete()
            except Exception:
                pass
    else:
        try:
            await target_msg.edit_text(
                text=msg_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
        except Exception:
            try:
                sent_msg = await call.message.answer(
                    text=msg_text,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
                await state.update_data(main_msg_id=sent_msg.message_id)
                await call.message.delete()
            except Exception:
                pass
    await state.set_state(RoomRentalFSM.SelectDate)


@router.callback_query(F.data.startswith("rent_date:"), RoomRentalFSM.SelectDate)
async def process_rental_date(
    call: CallbackQuery, 
    state: FSMContext,
    db_session: AsyncSession,
    booking_service: BookingService
) -> None:
    """Processes date choice and prompts hours choice selection."""
    await call.answer()
    selected_date_str = call.data.split(":")[1]
    await state.update_data(selected_date=selected_date_str)
    
    data = await state.get_data()
    room_id = data["room_id"]
    
    free_times = await booking_service.get_available_room_slots(room_id, selected_date_str)
            
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    
    builder = InlineKeyboardBuilder()
    for t in free_times:
        builder.add(InlineKeyboardButton(text=f"⏰ {t}", callback_data=f"rent_time:{t}"))
        
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="⬅️ Назад до дат", callback_data="back:rent_date"))
    
    msg_text = f"⏰ *Оберіть час початку оренди кабінету на {selected_date_str}:*"
    if call.message.photo or call.message.video or call.message.document:
        await call.message.edit_caption(
            caption=msg_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    else:
        await call.message.edit_text(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    await state.set_state(RoomRentalFSM.SelectSlot)


@router.callback_query(F.data.startswith("rent_time:"), RoomRentalFSM.SelectSlot)
async def process_rental_slot(
    call: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    booking_service: BookingService
) -> None:
    """Processes start time choice and prompts duration selection based on consecutive slot availability."""
    await call.answer()
    selected_time = call.data.split(":", 1)[1]
    await state.update_data(selected_time=selected_time)
    
    data = await state.get_data()
    room_id = data["room_id"]
    selected_date = data["selected_date"]
    
    non_locked_free_times = set(await booking_service.get_available_room_slots(room_id, selected_date))
            
    # Check consecutive duration availability
    start_dt = datetime.strptime(selected_time, "%H:%M")
    consecutive_options = []
    for duration in (1, 2, 3, 4):
        is_consecutive_free = True
        for offset in range(duration):
            check_time_str = (start_dt + timedelta(hours=offset)).strftime("%H:%M")
            if check_time_str not in non_locked_free_times:
                is_consecutive_free = False
                break
        if is_consecutive_free:
            consecutive_options.append(duration)
            
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    
    builder = InlineKeyboardBuilder()
    # Offer options based on availability
    for duration in consecutive_options:
        text_label = f"{duration} год. ⏰"
        if duration >= 3:
            text_label = f"{duration} год. 🎁 (Пакет)"
        builder.add(InlineKeyboardButton(text=text_label, callback_data=f"duration:{duration}"))
        
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="⬅️ Назад до годин", callback_data=f"back:rent_slot:{selected_date}"))
    
    msg_text = f"⏰ *Оберіть тривалість оренди починаючи з {selected_time}:*"
    if call.message.photo or call.message.video or call.message.document:
        await call.message.edit_caption(
            caption=msg_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    else:
        await call.message.edit_text(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    await state.set_state(RoomRentalFSM.SelectDuration)


@router.callback_query(F.data.startswith("duration:"), RoomRentalFSM.SelectDuration)
async def process_rental_duration(
    call: CallbackQuery, 
    state: FSMContext,
    booking_service: BookingService,
    sheets=None
) -> None:
    """Calculates rates, acquires Redis locks, and prompts for name."""
    hours = int(call.data.split(":")[1])
    data = await state.get_data()
    room_id = data["room_id"]
    selected_date = data["selected_date"]
    selected_time = data["selected_time"]
    
    # Try locking slots in Redis
    locked = await booking_service.lock_room_rental_slots(
        room_id=room_id,
        date_str=selected_date,
        time_str=selected_time,
        duration_hours=hours,
        user_id=call.from_user.id
    )
    if not locked:
        await call.answer("⚠️ Обраний час вже заблоковано іншим клієнтом. Оберіть інший час.", show_alert=True)
        return
        
    await call.answer()
    
    # Default pricing fallbacks
    base_rate = 200.0
    evening_rate = 150.0
    
    # Use cached rooms if available to avoid API latency
    global _ROOMS_CACHE
    found_in_cache = False
    if _ROOMS_CACHE["data"]:
        for rd in _ROOMS_CACHE["data"]:
            if rd["id"] == room_id:
                base_rate = rd["hourly"]
                evening_rate = rd["evening"]
                found_in_cache = True
                break
                
    if not found_in_cache and sheets:
        try:
            rows = await sheets.read_sheet("Реєстр Кабінетів")
            for r in rows[1:]:
                if len(r) >= 5 and r[0].isdigit() and int(r[0]) == room_id:
                    base_rate = float(r[3]) if r[3] else 200.0
                    evening_rate = float(r[4]) if r[4] else 150.0
                    break
        except Exception as e:
            logger.error("error_reading_rates_for_duration", error=str(e))
            
    total_price = 0.0
    start_hour_dt = datetime.strptime(selected_time, "%H:%M")
    # Dynamic logic: iterate over hours to check if hour is in evening range (18:00 to 20:00)
    for hour_offset in range(hours):
        current_hour = (start_hour_dt + timedelta(hours=hour_offset)).hour
        if 18 <= current_hour < 20:
            rate = evening_rate
        else:
            rate = base_rate
        total_price += rate
        
    # Package Discounts (10% off for 3+ hours)
    if hours >= 3:
        total_price *= 0.90
        
    await state.update_data(
        hours=hours,
        total_price=total_price
    )
    
    await call.message.edit_text(
        text="👤 *Будь ласка, введіть Ваше Ім'я та Прізвище для договору оренди:*",
        parse_mode="Markdown"
    )
    await state.set_state(RoomRentalFSM.EnterName)

@router.message(RoomRentalFSM.EnterName)
async def process_rental_name(message: Message, state: FSMContext) -> None:
    """Saves renter name and prompts for phone contact."""
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    # Delete the user's text message immediately to keep the chat clean
    try:
        await message.delete()
    except Exception:
        pass

    if not message.text or len(message.text.strip()) < 3:
        # Edit the main message to show error
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                text="⚠️ *Введіть коректне ім'я та прізвище (мінімум 3 символи):*\n\n"
                     "👤 *Будь ласка, введіть Ваше Ім'я та Прізвище для договору оренди:*",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return
        
    from aiogram.utils.keyboard import ReplyKeyboardBuilder
    from aiogram.types import KeyboardButton
    
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Надіслати контакт", request_contact=True))
    
    await state.update_data(client_name=message.text.strip())
    
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            text=f"👤 *Ім'я:* {message.text.strip()}\n\n"
                 f"⏳ _Чекаємо на надання номера телефону..._",
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    # Since ReplyKeyboardMarkup cannot be edited into an existing inline message,
    # we send a temporary message and store its message ID to delete it later
    sent_msg = await message.answer(
        text="📞 *Надішліть Ваш номер телефону за допомогою кнопки нижче або введіть його вручну у форматі +380XXXXXXXXX:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )
    await state.update_data(phone_prompt_msg_id=sent_msg.message_id)
    await state.set_state(RoomRentalFSM.EnterPhone)

@router.message(RoomRentalFSM.EnterPhone)
async def process_rental_phone(
    message: Message, 
    state: FSMContext, 
    booking_service: BookingService,
    current_user: User
) -> None:
    """Validates phone and displays final rental invoice summary."""
    import re
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    phone_prompt_msg_id = data.get("phone_prompt_msg_id")
    
    # Delete the user's message immediately
    try:
        await message.delete()
    except Exception:
        pass
        
    phone = None
    if message.contact:
        phone = message.contact.phone_number
        if not phone.startswith("+"):
            phone = f"+{phone}"
    elif message.text:
        phone = message.text.strip()
        
    if not phone or not re.match(r"^\+380\d{9}$", phone):
        # Update the main message to indicate formatting error
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
    
    # Create invoice with client name and phone
    invoice_url, invoice_id = await booking_service.create_rental_invoice(
        user_id=current_user.id,
        room_id=data["room_id"],
        date_str=data["selected_date"],
        time_str=data["selected_time"],
        hours=data["hours"],
        price=data["total_price"],
        client_name=data["client_name"],
        client_phone=phone
    )
    
    from aiogram.types import ReplyKeyboardRemove
    await state.update_data(invoice_id=invoice_id)
    
    # Hide reply keyboard
    dummy = await message.answer("⏳", reply_markup=ReplyKeyboardRemove())
    try:
        await dummy.delete()
    except Exception:
        pass
    
    prepay_amount = 50.0
    summary_text = (
        f"💳 *Розрахунок вартості оренди кабінету:*\n\n"
        f"🏢 Кабінет: *{data.get('room_name', 'Головний кабінет')}*\n"
        f"📅 Дата: *{data['selected_date']}*\n"
        f"⏰ Тривалість: *{data['hours']} год.*\n"
        f"👤 Клієнт: *{data['client_name']}*\n"
        f"📞 Телефон: *{phone}*\n"
        f"💵 Загальна вартість: *{data['total_price']:.2f} UAH* "
        f"{'(Враховано знижку 10%!)' if data['hours'] >= 3 else ''}\n"
        f"💳 Передплата: *50.00 UAH* (решта {data['total_price'] - prepay_amount:.2f} UAH сплачується при зустрічі)\n\n"
        f"⚠️ _Зарезервований час буде заблоковано в сітці після успішного внесення передплати._"
    )
    
    # Edit the main message to present the invoice summary
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url, amount=50.0)
        )
    except Exception:
        # Fallback if editing fails
        await message.answer(
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url, amount=50.0)
        )
        
    if phone_prompt_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=phone_prompt_msg_id)
        except Exception:
            pass

    await state.set_state(RoomRentalFSM.ConfirmAndPay)



@router.callback_query(F.data == "back:rent_date")
async def back_to_rent_date(
    call: CallbackQuery, 
    state: FSMContext,
    booking_service: BookingService
) -> None:
    """Handles going back to the room selection / calendar date screen from duration."""
    await call.answer()
    data = await state.get_data()
    room_id = data.get("room_id", 1)
    
    now = datetime.now()
    active_dates = await booking_service.get_available_room_dates(room_id, now.year, now.month)
    
    markup = _generate_room_calendar(active_dates)
    
    msg_text = "📅 *Оберіть бажану дату оренди:*\n\n_(активні дати клікабельні, неактивні позначені крапкою)_"
    if call.message.photo or call.message.video or call.message.document:
        await call.message.edit_caption(
            caption=msg_text,
            parse_mode="Markdown",
            reply_markup=markup
        )
    else:
        await call.message.edit_text(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=markup
        )
    await state.set_state(RoomRentalFSM.SelectDate)


@router.callback_query(F.data.startswith("back:rent_slot:"))
async def back_to_rent_slot(
    call: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    booking_service: BookingService
) -> None:
    """Handles going back to selecting start time from duration choice."""
    await call.answer()
    selected_date_str = call.data.split(":")[2]
    
    data = await state.get_data()
    room_id = data.get("room_id", 1)
    
    free_times = await booking_service.get_available_room_slots(room_id, selected_date_str)
            
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    
    builder = InlineKeyboardBuilder()
    for t in free_times:
        builder.add(InlineKeyboardButton(text=f"⏰ {t}", callback_data=f"rent_time:{t}"))
        
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="⬅️ Назад до дат", callback_data="back:rent_date"))
    
    msg_text = f"⏰ *Оберіть час початку оренди кабінету на {selected_date_str}:*"
    if call.message.photo or call.message.video or call.message.document:
        await call.message.edit_caption(
            caption=msg_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    else:
        await call.message.edit_text(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    await state.set_state(RoomRentalFSM.SelectSlot)


@router.callback_query(F.data == "booking:cancel", RoomRentalFSM.ConfirmAndPay)
async def cancel_rental_checkout(call: CallbackQuery, state: FSMContext) -> None:
    """Gracefully handles rental checkout abandonment and FSM clearance."""
    await call.answer()
    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])
    for msg_id in messages_to_delete:
        try:
            await call.message.bot.delete_message(chat_id=call.message.chat.id, message_id=msg_id)
        except Exception:
            pass
    await state.clear()
    msg_text = "❌ Бронювання оренди скасовано. Ви можете почати спочатку з головного меню."
    markup = get_main_menu_keyboard(user_id=call.from_user.id)
    if call.message.photo or call.message.video or call.message.document:
        await call.message.edit_caption(
            caption=msg_text,
            reply_markup=markup
        )
    else:
        await call.message.edit_text(
            text=msg_text,
            reply_markup=markup
        )

