# app/bot/handlers/events.py
import re
import asyncio
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from structlog import get_logger

from app.bot.states.booking import EventFSM
from app.bot.keyboards.inline import get_main_menu_keyboard
from app.services.booking import BookingService
from app.database.models.user import User

logger = get_logger()
router = Router(name="events_router")

# Cached file_id for the events poster to speed up delivery
_EVENTS_PHOTO_FILE_ID = None


@router.callback_query(F.data == "menu:events")
async def process_events_menu(
    call: CallbackQuery,
    booking_service: BookingService
) -> None:
    """Displays active and future studio events list."""
    # Show loading status immediately
    loading_text = "⏳ *Завантажуємо афішу заходів... Будь ласка, зачекайте.*"
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
    
    import os
    from aiogram.types import FSInputFile
    
    events = await booking_service.get_cached_events()
    
    if not events:
        events_text = (
            "📅 *Афіша наших актуальних заходів:*\n\n"
            "📭 Наразі немає активних заходів. Спробуйте пізніше!\n\n"
            "🌿 _Поверніться до головного меню нижче:_"
        )
    else:
        events_text = "📅 *Афіша наших актуальних заходів:*\n\n"
        for i, event in enumerate(events, 1):
            if event["status"] == "Актуальний":
                status_label = "🔥 (Актуальний)"
                details = f"Вартість: *{event['price']:.2f} UAH* | 🎟️ Вільних місць: *{event['seats_left']} з {event['limit']}*"
            else:
                status_label = "📢 (Анонс — Скоро у продажу)"
                details = "Очікуйте у продажу найближчим часом"
                
            events_text += (
                f"*{i}. {event['title']}* {status_label}\n"
                f"   • Ведучий: {event['host']}\n"
                f"   • Дата й час: {event['date']}\n"
                f"   • {details}\n\n"
            )
        events_text += "✨ _Оберіть опцію для бронювання або підписки на сповіщення:_"
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    
    for event in events:
        if event["status"] == "Актуальний":
            builder.row(InlineKeyboardButton(text=f"Бронювати: {event['title'][:20]}... 🎟️", callback_data=f"event_book:{event['id']}"))
        else:
            builder.row(InlineKeyboardButton(text=f"Нагадати: {event['title'][:20]}... 📢", callback_data=f"event_notify:{event['id']}"))
            
    builder.row(InlineKeyboardButton(text="⬅️ Головне меню", callback_data="menu:home"))
    
    global _EVENTS_PHOTO_FILE_ID
    markup = builder.as_markup()
    
    if _EVENTS_PHOTO_FILE_ID:
        try:
            # Send photo using cached file_id
            await call.message.answer_photo(
                photo=_EVENTS_PHOTO_FILE_ID,
                caption=events_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            try:
                await call.message.delete()
            except Exception:
                pass
            await call.answer()
            return
        except Exception as e:
            logger.warning("failed_to_send_events_via_cached_file_id_falling_back", error=str(e))
            
    # Path to events poster asset
    asset_path = os.path.join(os.path.dirname(__file__), "..", "assets", "events_poster.png")
    
    if os.path.exists(asset_path):
        photo = FSInputFile(asset_path)
        sent_msg = await call.message.answer_photo(
            photo=photo,
            caption=events_text,
            parse_mode="Markdown",
            reply_markup=markup
        )
        
        if sent_msg.photo:
            _EVENTS_PHOTO_FILE_ID = sent_msg.photo[-1].file_id
            logger.info("stored_events_photo_file_id", file_id=_EVENTS_PHOTO_FILE_ID)
            
        try:
            await call.message.delete()
        except Exception:
            pass
    else:
        # Fallback to editing or sending text
        if call.message.photo or call.message.document:
            await call.message.answer(
                text=events_text,
                parse_mode="Markdown",
                reply_markup=markup
            )
            try:
                await call.message.delete()
            except Exception:
                pass
        else:
            try:
                await call.message.edit_text(
                    text=events_text,
                    parse_mode="Markdown",
                    reply_markup=markup
                )
            except Exception:
                pass
    await call.answer()

@router.callback_query(F.data.startswith("event_notify:"))
async def process_event_announcement_notify(call: CallbackQuery) -> None:
    """Captures client interest as warm leads for future releases."""
    event_id = int(call.data.split(":")[1])
    
    # Save lead notification state locally or in Redis
    logger.info("warm_lead_registered_for_future_event", event_id=event_id, user=call.from_user.id)
    
    await call.answer("🔔 Дякуємо! Ми надішлемо вам сповіщення, щойно відкриється продаж квитків.", show_alert=True)

async def delete_message_after_delay(message: Message, delay: int = 10) -> None:
    """Helper to delete a message after a specified delay in seconds."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

@router.callback_query(F.data.startswith("event_book:"))
async def process_event_booking_start(
    call: CallbackQuery, 
    state: FSMContext,
    booking_service: BookingService
) -> None:
    """Starts checkout wizard for verified event seats booking."""
    event_id = int(call.data.split(":")[1])
    
    # Get dynamic events list to find the event title and price
    events = await booking_service.get_cached_events()
    event = next((e for e in events if e["id"] == event_id), None)
    
    if not event:
        await call.answer("❌ Захід не знайдено!", show_alert=True)
        return
        
    user_id = call.from_user.id
    lock_key = f"lock:event_seat:{event_id}:{user_id}"
    has_lock = await booking_service.redis.get(lock_key)
    
    if not has_lock and event["seats_left"] <= 0:
        await call.answer("⚠️ Вибачте, на цей захід більше немає вільних місць!", show_alert=True)
        return
        
    await booking_service.redis.set(lock_key, "1", ex=900)
        
    try:
        await call.message.delete()
    except Exception:
        pass
        
    sent_msg = await call.message.answer(
        text="👤 *Будь ласка, введіть Ваше Ім'я для квитка:*",
        parse_mode="Markdown"
    )
    
    await state.update_data(
        event_id=event_id,
        event_name=event["title"],
        event_price=event["price"],
        event_date=event["date"],
        prompt_message_id=sent_msg.message_id
    )
    await state.set_state(EventFSM.EnterName)
    await call.answer()

@router.message(EventFSM.EnterName)
async def process_event_client_name(message: Message, state: FSMContext) -> None:
    """Prompts for ticket contact phone."""
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")

    # Delete the user's typed name message immediately
    try:
        await message.delete()
    except Exception:
        pass

    if not message.text or len(message.text.strip()) < 3:
        err_msg = await message.answer("⚠️ Введіть коректне ім'я (мінімум 3 символи):")
        if prompt_message_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_message_id)
            except Exception:
                pass
        await state.update_data(prompt_message_id=err_msg.message_id)
        return
        
    from aiogram.utils.keyboard import ReplyKeyboardBuilder
    from aiogram.types import KeyboardButton
    
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="📱 Надіслати контакт", request_contact=True))
    
    await state.update_data(event_client_name=message.text.strip())
    sent_msg = await message.answer(
        text="📞 *Надішліть Ваш номер телефону для відправки квитка (натисніть кнопку нижче або введіть вручну у форматі +380XXXXXXXXX):*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )
    if prompt_message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_message_id)
        except Exception:
            pass
    await state.update_data(prompt_message_id=sent_msg.message_id)
    await state.set_state(EventFSM.EnterPhone)

@router.message(EventFSM.EnterPhone)
async def process_event_client_phone(
    message: Message, 
    state: FSMContext,
    booking_service: BookingService,
    current_user: User
) -> None:
    """Generates standard payment parameters or details checks."""
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")

    # Delete the user's typed phone message or contact immediately
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
        err_msg = await message.reply("⚠️ Формат має бути +380XXXXXXXXX. Спробуйте ще раз або натисніть кнопку:")
        if prompt_message_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_message_id)
            except Exception:
                pass
        await state.update_data(prompt_message_id=err_msg.message_id)
        return

    await state.update_data(event_client_phone=phone)
    data = await state.get_data()
    event_price = float(data.get("event_price", 0.0))
    
    from aiogram.types import ReplyKeyboardRemove
    # Remove reply keyboard
    dummy = await message.answer("⏳ Обробка...", reply_markup=ReplyKeyboardRemove())

    # Confirm booking directly without requiring prepayment
    await booking_service.confirm_event_booking_without_prepayment(
        user_id=current_user.id,
        event_id=int(data.get("event_id", 1)),
        event_name=data.get("event_name", ""),
        date_str=data.get("event_date", ""),
        price=event_price,
        client_name=data["event_client_name"],
        client_phone=phone,
        telegram_id=message.from_user.id
    )
    
    # Clean up prompts
    try:
        await dummy.delete()
    except Exception:
        pass
        
    if prompt_message_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_message_id)
        except Exception:
            pass
            
    await state.clear()
    return

@router.callback_query(F.data == "booking:cancel", EventFSM.ConfirmAndPay)
async def cancel_event_checkout(
    call: CallbackQuery, 
    state: FSMContext,
    booking_service: BookingService
) -> None:
    """Gracefully handles event checkout abandonment and FSM clearance."""
    data = await state.get_data()
    event_id = data.get("event_id")
    if event_id:
        lock_key = f"lock:event_seat:{event_id}:{call.from_user.id}"
        await booking_service.redis.delete(lock_key)
        
    # Reset FSM state
    await state.clear()
    
    # Edit the main message back to the main menu immediately
    await call.message.edit_text(
        text="🌿 Оберіть бажану опцію меню нижче:",
        reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
    )
    
    # Send a temporary notice that will delete itself in 10 seconds
    temp_msg = await call.message.answer("❌ Бронювання місця на захід скасовано.")
    asyncio.create_task(delete_message_after_delay(temp_msg, 10))
    
    await call.answer()


