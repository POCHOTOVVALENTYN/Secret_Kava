# app/bot/handlers/host_event.py
import re
import os
import asyncio
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from structlog import get_logger

from app.bot.states.booking import HostEventFSM
from app.bot.keyboards.inline import get_payment_keyboard
from app.services.booking import BookingService
from app.database.models.user import User

logger = get_logger()
router = Router(name="host_event_router")

# Cached file_id for the banner
_HOST_EVENT_PHOTO_FILE_ID = None

PHONE_REGEX = re.compile(r"^\+380\d{9}$")

@router.callback_query(F.data == "menu:host_event")
async def start_host_event_flow(call: CallbackQuery, state: FSMContext) -> None:
    """Entry point for hosting an event registration."""
    loading_text = "⏳ *Завантажуємо форму організації заходу... Будь ласка, зачекайте.*"
    target_msg = None
    if call.message.photo or call.message.video or call.message.document:
        try:
            await call.message.delete()
        except Exception:
            pass
        target_msg = await call.message.answer(
            text=loading_text,
            parse_mode="Markdown"
        )
    else:
        try:
            await call.message.edit_text(
                text=loading_text,
                parse_mode="Markdown"
            )
        except Exception:
            pass
        target_msg = call.message
        
    await call.answer()
    await state.clear()
    await state.update_data(main_msg_id=target_msg.message_id)
    
    welcome_text = (
        "🎭 *Оренда залу для заходів* 🎭\n\n"
        "Бажаєте провести свій захід, лекцію, воркшоп чи групову зустріч у нашому затишному просторі?\n"
        "Надішліть заявку на проведення, заповнивши коротку анкету.\n\n"
        "💳 Для реєстрації заявки вноситься передплата: *50.00 UAH* (кошти зараховуються в рахунок оренди).\n\n"
        "✍️ *Будь ласка, введіть назву вашого заходу:*"
    )
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    global _HOST_EVENT_PHOTO_FILE_ID
    asset_path = "app/bot/assets/host_event_banner.png"
    
    async def clean_old_msg():
        try:
            await call.bot.delete_message(chat_id=call.message.chat.id, message_id=target_msg.message_id)
        except Exception:
            pass
        try:
            await call.message.delete()
        except Exception:
            pass

    if _HOST_EVENT_PHOTO_FILE_ID:
        try:
            sent_msg = await call.message.answer_photo(
                photo=_HOST_EVENT_PHOTO_FILE_ID,
                caption=welcome_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
            await state.update_data(main_msg_id=sent_msg.message_id)
            await clean_old_msg()
            await state.set_state(HostEventFSM.EnterTitle)
            return
        except Exception as e:
            logger.warning("failed_to_send_host_banner_via_cached_file_id", error=str(e))
            
    if os.path.exists(asset_path):
        try:
            photo = FSInputFile(asset_path)
            sent_msg = await call.message.answer_photo(
                photo=photo,
                caption=welcome_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
            if sent_msg.photo:
                _HOST_EVENT_PHOTO_FILE_ID = sent_msg.photo[-1].file_id
                logger.info("stored_host_event_photo_file_id", file_id=_HOST_EVENT_PHOTO_FILE_ID)
            await state.update_data(main_msg_id=sent_msg.message_id)
            await clean_old_msg()
            await state.set_state(HostEventFSM.EnterTitle)
            return
        except Exception as e:
            logger.error("failed_to_send_host_banner_photo", error=str(e))
            
    # Fallback to text
    try:
        await target_msg.edit_text(
            text=welcome_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        sent_msg = await call.message.answer(
            text=welcome_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        await state.update_data(main_msg_id=sent_msg.message_id)
        await clean_old_msg()
        
    await state.set_state(HostEventFSM.EnterTitle)

@router.message(HostEventFSM.EnterTitle)
async def process_event_title(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    title = message.text.strip() if message.text else ""
    if not title or len(title) < 3:
        try:
            await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                caption="⚠️ *Назва заходу має містити щонайменше 3 символи!*\n\n"
                        "✍️ *Будь ласка, введіть назву вашого заходу:*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(title=title)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{title}*\n\n✍️ *Введіть ім'я ведучого/організатора заходу:*",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        pass
        
    await state.set_state(HostEventFSM.EnterHost)

@router.message(HostEventFSM.EnterHost)
async def process_event_host(message: Message, state: FSMContext, booking_service: BookingService) -> None:
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    host = message.text.strip() if message.text else ""
    if not host or len(host) < 2:
        try:
            await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                caption=f"🎭 Захід: *{data['title']}*\n\n"
                        f"⚠️ *Введіть ім'я ведучого (мінімум 2 символи):*\n"
                        f"✍️ *Введіть ім'я ведучого/організатора заходу:*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(host=host)

    now = datetime.now()
    active_dates = await booking_service.get_available_room_dates(room_id=2, year=now.year, month=now.month)
    markup = _generate_host_event_calendar(active_dates)
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{data['title']}*\n"
                    f"👤 Ведучий: *{host}*\n\n"
                    f"📅 *Оберіть дату оренди залу:*\n\n_(активні дати клікабельні, неактивні позначені крапкою)_",
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception:
        pass
        
    await state.set_state(HostEventFSM.SelectDate)

def _generate_host_event_calendar(active_dates: set[str]) -> InlineKeyboardMarkup:
    import calendar
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    now = datetime.now()
    year = now.year
    month = now.month
    
    month_names = {
        1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень", 5: "Травень", 6: "Червень",
        7: "Липень", 8: "Серпень", 9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"
    }
    
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"📅 {month_names[month]} {year}", callback_data="ignore"))
    
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
                
                is_past = False
                try:
                    day_dt = datetime(year, month, day)
                    if day_dt.date() < now.date():
                        is_past = True
                except ValueError:
                    pass
                
                if is_active and not is_past:
                    row_buttons.append(InlineKeyboardButton(text=str(day), callback_data=f"host_event_date:{date_str}"))
                else:
                    row_buttons.append(InlineKeyboardButton(text="·", callback_data="ignore"))
        b.row(*row_buttons)
        
    b.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    return b.as_markup()

@router.callback_query(F.data.startswith("host_event_date:"), HostEventFSM.SelectDate)
async def process_host_event_date(
    call: CallbackQuery, 
    state: FSMContext,
    booking_service: BookingService
) -> None:
    await call.answer()
    selected_date_str = call.data.split(":")[1]
    await state.update_data(selected_date=selected_date_str)
    
    free_times = await booking_service.get_available_room_slots(room_id=2, date_str=selected_date_str)
            
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    builder = InlineKeyboardBuilder()
    for t in free_times:
        builder.add(InlineKeyboardButton(text=f"⏰ {t}", callback_data=f"host_event_time:{t}"))
        
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="⬅️ Назад до дат", callback_data="back:host_event_date"))
    
    data = await state.get_data()
    await call.message.edit_caption(
        caption=f"🎭 Захід: *{data['title']}*\n"
                f"👤 Ведучий: *{data['host']}*\n\n"
                f"⏰ *Оберіть час початку оренди залу на {selected_date_str}:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await state.set_state(HostEventFSM.SelectSlot)

@router.callback_query(F.data.startswith("host_event_time:"), HostEventFSM.SelectSlot)
async def process_host_event_slot(
    call: CallbackQuery,
    state: FSMContext,
    booking_service: BookingService
) -> None:
    await call.answer()
    selected_time = call.data.split(":", 1)[1]
    await state.update_data(selected_time=selected_time)
    
    data = await state.get_data()
    selected_date = data["selected_date"]
    
    non_locked_free_times = set(await booking_service.get_available_room_slots(room_id=2, date_str=selected_date))
            
    start_dt = datetime.strptime(selected_time, "%H:%M")
    consecutive_options = []
    for duration in (1, 2, 3, 4, 5, 6):
        is_consecutive_free = True
        for offset in range(duration):
            check_time_str = (start_dt + timedelta(hours=offset)).strftime("%H:%M")
            if check_time_str not in non_locked_free_times:
                is_consecutive_free = False
                break
        if is_consecutive_free:
            consecutive_options.append(duration)
            
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    builder = InlineKeyboardBuilder()
    for duration in consecutive_options:
        text_label = f"{duration} год. ⏰"
        builder.add(InlineKeyboardButton(text=text_label, callback_data=f"host_event_dur:{duration}"))
        
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="⬅️ Назад до годин", callback_data=f"back:host_event_slot:{selected_date}"))
    
    await call.message.edit_caption(
        caption=f"🎭 Захід: *{data['title']}*\n"
                f"👤 Ведучий: *{data['host']}*\n"
                f"📅 Дата: *{selected_date}* | Початок: *{selected_time}*\n\n"
                f"⏰ *Оберіть тривалість оренди залу:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await state.set_state(HostEventFSM.SelectDuration)

@router.callback_query(F.data.startswith("host_event_dur:"), HostEventFSM.SelectDuration)
async def process_host_event_duration(
    call: CallbackQuery, 
    state: FSMContext,
    booking_service: BookingService
) -> None:
    hours = int(call.data.split(":")[1])
    data = await state.get_data()
    selected_date = data["selected_date"]
    selected_time = data["selected_time"]
    
    locked = await booking_service.lock_room_rental_slots(
        room_id=2,
        date_str=selected_date,
        time_str=selected_time,
        duration_hours=hours,
        user_id=call.from_user.id
    )
    if not locked:
        await call.answer("⚠️ Обраний час вже заблоковано іншим клієнтом. Оберіть інший час.", show_alert=True)
        return
        
    await call.answer()
    await state.update_data(hours=hours)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))

    await call.message.edit_caption(
        caption=f"🎭 Захід: *{data['title']}*\n"
                f"👤 Ведучий: *{data['host']}*\n"
                f"📅 Дата: *{selected_date}* | Час: *{selected_time}* ({hours} год.)\n\n"
                f"✍️ *Введіть ліміт місць (кількість учасників від 4 до 20 включно):*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await state.set_state(HostEventFSM.EnterLimit)

@router.message(HostEventFSM.EnterLimit)
async def process_event_limit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    limit_input = message.text.strip() if message.text else ""
    try:
        limit = int(limit_input)
        if limit < 4 or limit > 20:
            raise ValueError
    except ValueError:
        try:
            await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                caption=f"🎭 Захід: *{data['title']}*\n"
                        f"👤 Ведучий: *{data['host']}*\n"
                        f"📅 Дата: *{data['selected_date']}* | Час: *{data['selected_time']}* ({data['hours']} год.)\n\n"
                        f"⚠️ *Введіть коректне ціле число від 4 до 20 для ліміту місць:*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(limit=limit)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{data['title']}*\n"
                    f"👤 Ведучий: *{data['host']}*\n"
                    f"📅 Дата: *{data['selected_date']}* | Час: *{data['selected_time']}* ({data['hours']} год.)\n"
                    f"👥 Ліміт місць: *{limit}*\n\n"
                    f"✍️ *Введіть вартість участі для одного клієнта (у грн, наприклад 400, або 0 якщо безкоштовно):*",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        pass
        
    await state.set_state(HostEventFSM.EnterPrice)

@router.message(HostEventFSM.EnterPrice)
async def process_event_price(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    price_input = message.text.strip().replace(",", ".") if message.text else ""
    try:
        price = float(price_input)
        if price < 0:
            raise ValueError
    except ValueError:
        try:
            await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                caption=f"🎭 Захід: *{data['title']}*\n"
                        f"👤 Ведучий: *{data['host']}*\n"
                        f"📅 Дата: *{data['selected_date']}* | Час: *{data['selected_time']}* ({data['hours']} год.)\n"
                        f"👥 Ліміт місць: *{data['limit']}*\n\n"
                        f"⚠️ *Введіть коректне число для ціни (мінімум 0):*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(price=price)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{data['title']}*\n"
                    f"👤 Ведучий: *{data['host']}*\n"
                    f"📅 Дата: *{data['selected_date']}* | Час: *{data['selected_time']}* ({data['hours']} год.)\n"
                    f"👥 Ліміт: *{data['limit']}* | 💵 Вартість: *{price} грн*\n\n"
                    f"👤 *Будь ласка, введіть Ваше Ім'я та Прізвище (для договору оренди):*",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        pass
        
    await state.set_state(HostEventFSM.EnterName)

@router.message(HostEventFSM.EnterName)
async def process_host_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    name = message.text.strip() if message.text else ""
    if not name or len(name) < 3:
        try:
            await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                caption=f"🎭 Захід: *{data['title']}*\n"
                        f"👤 Ведучий: *{data['host']}*\n"
                        f"📅 Дата: *{data['selected_date']}* | Час: *{data['selected_time']}* ({data['hours']} год.)\n\n"
                        f"⚠️ *Введіть коректне ім'я та прізвище (мінімум 3 символи):*\n"
                        f"👤 *Будь ласка, введіть Ваше Ім'я та Прізвище (для договору оренди):*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(client_name=name)
    
    from aiogram.utils.keyboard import ReplyKeyboardBuilder
    from aiogram.types import KeyboardButton
    
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Надіслати контакт", request_contact=True))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"👤 *Організатор:* {name}\n\n"
                    f"⏳ _Чекаємо на надання номера телефону..._",
            parse_mode="Markdown"
        )
    except Exception:
        pass
        
    sent_msg = await message.answer(
        text="📞 *Надішліть Ваш номер телефону за допомогою кнопки нижче або введіть його вручну у форматі +380XXXXXXXXX:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )
    await state.update_data(phone_prompt_msg_id=sent_msg.message_id)
    await state.set_state(HostEventFSM.EnterPhone)

@router.message(HostEventFSM.EnterPhone)
async def process_host_phone(
    message: Message, 
    state: FSMContext, 
    booking_service: BookingService,
    current_user: User
) -> None:
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    phone_prompt_msg_id = data.get("phone_prompt_msg_id")
    
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
        
    if not phone or not PHONE_REGEX.match(phone):
        try:
            await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                caption="⚠️ *Номер телефону не відповідає формату!*\n\n"
                        f"👤 *Організатор:* " + data["client_name"] + "\n"
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
    
    invoice_url, invoice_id = await booking_service.create_host_event_invoice(
        user_id=current_user.id,
        title=data["title"],
        host=data["host"],
        date_str=data["selected_date"],
        time_str=data["selected_time"],
        hours=data["hours"],
        limit=data["limit"],
        price=data["price"],
        client_name=data["client_name"],
        client_phone=phone
    )
    
    from aiogram.types import ReplyKeyboardRemove
    await state.update_data(invoice_id=invoice_id)
    
    dummy = await message.answer("⏳", reply_markup=ReplyKeyboardRemove())
    try:
        await dummy.delete()
    except Exception:
        pass
        
    summary_text = (
        f"💳 *Розрахунок реєстрації заходу:*\n\n"
        f"🎭 Назва: *{data['title']}*\n"
        f"👤 Ведучий: *{data['host']}*\n"
        f"📅 Дата: *{data['selected_date']}* | Час: *{data['selected_time']}* ({data['hours']} год.)\n"
        f"👥 Ліміт місць: *{data['limit']}*\n"
        f"💵 Вартість для клієнтів: *{data['price']} грн*\n"
        f"👤 Заявник: *{data['client_name']}*\n"
        f"📞 Телефон: *{phone}*\n\n"
        f"💳 Передплата: *50.00 UAH*\n\n"
        f"⚠️ _Заявка буде надіслана на модерацію та внесена до афіші після успішної оплати передплати._"
    )
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url, amount=50.0)
        )
    except Exception:
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
            
    await state.set_state(HostEventFSM.ConfirmAndPay)

@router.callback_query(F.data == "back:host_event_date")
async def back_to_host_event_date(
    call: CallbackQuery, 
    state: FSMContext,
    booking_service: BookingService
) -> None:
    await call.answer()
    data = await state.get_data()
    
    now = datetime.now()
    active_dates = await booking_service.get_available_room_dates(room_id=2, year=now.year, month=now.month)
    markup = _generate_host_event_calendar(active_dates)
    
    await call.message.edit_caption(
        caption=f"🎭 Захід: *{data['title']}*\n"
                f"👤 Ведучий: *{data['host']}*\n\n"
                f"📅 *Оберіть дату оренди залу:*\n\n_(активні дати клікабельні, неактивні позначені крапкою)_",
        parse_mode="Markdown",
        reply_markup=markup
    )
    await state.set_state(HostEventFSM.SelectDate)

@router.callback_query(F.data.startswith("back:host_event_slot:"))
async def back_to_host_event_slot(
    call: CallbackQuery,
    state: FSMContext,
    booking_service: BookingService
) -> None:
    await call.answer()
    selected_date_str = call.data.split(":")[2]
    
    free_times = await booking_service.get_available_room_slots(room_id=2, date_str=selected_date_str)
            
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    
    builder = InlineKeyboardBuilder()
    for t in free_times:
        builder.add(InlineKeyboardButton(text=f"⏰ {t}", callback_data=f"host_event_time:{t}"))
        
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="⬅️ Назад до дат", callback_data="back:host_event_date"))
    
    data = await state.get_data()
    await call.message.edit_caption(
        caption=f"🎭 Захід: *{data['title']}*\n"
                f"👤 Ведучий: *{data['host']}*\n\n"
                f"⏰ *Оберіть час початку оренди залу на {selected_date_str}:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await state.set_state(HostEventFSM.SelectSlot)
