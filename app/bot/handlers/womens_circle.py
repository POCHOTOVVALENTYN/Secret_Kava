# app/bot/handlers/womens_circle.py
import re
import os
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, FSInputFile
from aiogram.fsm.context import FSMContext
from structlog import get_logger

from app.bot.states.booking import WomensCircleFSM
from app.bot.keyboards.inline import get_payment_keyboard, get_main_menu_keyboard
from app.services.booking import BookingService
from app.database.models.user import User

logger = get_logger()
router = Router(name="womens_circle_router")


@router.callback_query(F.data == "menu:womens_circle")
async def process_womens_circle_menu(
    call: CallbackQuery,
    state: FSMContext
) -> None:
    """Displays Women's Circle description and signup action button."""
    # Show loading status immediately
    loading_text = "⏳ *Завантажуємо інформацію про Жіноче коло... Будь ласка, зачекайте.*"
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
    await state.clear()
    await state.update_data(main_msg_id=call.message.message_id)

    desc_text = (
        "«Жіноче коло» в Secret Kava — це атмосферні вечори за переглядом «Відчайдушних домогосподарок», "
        "келихом вина або чашкою кави та щирими розмовами ☕️🍷\n\n"
        "Обговорюємо героїнь, психологію стосунків і просто добре проводимо час ✨\n\n"
        "Вартість участі — *400 грн* 🫶"
    )

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📅 Записатися на зустріч 🍷", callback_data="womens_circle:register"))
    builder.row(InlineKeyboardButton(text="⬅️ Головне меню", callback_data="menu:home"))

    asset_path = "app/bot/assets/womens_circle_poster.png"
    
    if os.path.exists(asset_path):
        try:
            photo = FSInputFile(asset_path)
            sent_msg = await call.message.answer_photo(
                photo=photo,
                caption=desc_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
            await state.update_data(main_msg_id=sent_msg.message_id)
            try:
                await call.message.delete()
            except Exception:
                pass
            return
        except Exception as e:
            logger.error("failed_to_send_womens_circle_poster", error=str(e))

    # Fallback text message if photo fails to send
    if call.message.photo or call.message.document:
        try:
            await call.message.delete()
        except Exception:
            pass
        sent_msg = await call.message.answer(
            text=desc_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        await state.update_data(main_msg_id=sent_msg.message_id)
    else:
        try:
            await call.message.edit_text(
                text=desc_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
        except Exception:
            pass


@router.callback_query(F.data == "womens_circle:register")
async def start_registration(
    call: CallbackQuery,
    state: FSMContext,
    booking_service: BookingService
) -> None:
    """Displays list of upcoming Friday dates for Women's Circle booking."""
    await call.answer()
    
    fridays = await booking_service.get_upcoming_fridays()
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    
    for f in fridays:
        if f["available"]:
            btn_text = f"🍷 {f['label']} (Вільні місця)"
            callback_data = f"wc_date:{f['date_str']}"
        else:
            btn_text = f"🚫 {f['label']} (Місць немає)"
            callback_data = "wc_full"
            
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=callback_data))
        
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:womens_circle"))
    
    msg_text = (
        "📅 *Оберіть бажану дату зустрічі «Жіноче коло»:*\n\n"
        "⏰ Час проведення: щоп'ятниці з *17:00 до 20:00*.\n"
        "Лише п'ятниці з вільними місцями клікабельні."
    )
    
    if call.message.photo or call.message.document:
        try:
            await call.message.delete()
        except Exception:
            pass
        sent_msg = await call.message.answer(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        await state.update_data(main_msg_id=sent_msg.message_id)
    else:
        try:
            await call.message.edit_text(
                text=msg_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
        except Exception:
            pass
            
    await state.set_state(WomensCircleFSM.SelectDate)


@router.callback_query(F.data == "wc_full")
async def process_full_date(call: CallbackQuery) -> None:
    """Answers with alert if date is full."""
    await call.answer("⚠️ На жаль, усі 17 місць на цю дату вже заброньовано. Оберіть іншу п'ятницю.", show_alert=True)


@router.callback_query(F.data.startswith("wc_date:"), WomensCircleFSM.SelectDate)
async def process_date(
    call: CallbackQuery,
    state: FSMContext
) -> None:
    """Saves selected date and prompts renter name."""
    await call.answer()
    selected_date = call.data.split(":")[1]
    await state.update_data(selected_date=selected_date)
    
    await call.message.edit_text(
        text="👤 *Будь ласка, введіть Ваше Ім'я та Прізвище для реєстрації:*",
        parse_mode="Markdown"
    )
    await state.set_state(WomensCircleFSM.EnterName)


@router.message(WomensCircleFSM.EnterName)
async def process_name(message: Message, state: FSMContext) -> None:
    """Validates name and requests phone contact details."""
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass

    if not message.text or len(message.text.strip()) < 3:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                text="⚠️ *Введіть коректне ім'я та прізвище (мінімум 3 символи):*\n\n"
                     "👤 *Будь ласка, введіть Ваше Ім'я та Прізвище для реєстрації:*",
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
    
    sent_msg = await message.answer(
        text="📞 *Надішліть Ваш номер телефону за допомогою кнопки нижче або введіть його вручну у форматі +380XXXXXXXXX:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True)
    )
    await state.update_data(phone_prompt_msg_id=sent_msg.message_id)
    await state.set_state(WomensCircleFSM.EnterPhone)


@router.message(WomensCircleFSM.EnterPhone)
async def process_phone(
    message: Message, 
    state: FSMContext, 
    booking_service: BookingService,
    current_user: User
) -> None:
    """Validates phone contact, generates Monobank prepayment checkout link."""
    import re
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
        
    if not phone or not re.match(r"^\+380\d{9}$", phone):
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
    
    # Generate event date/time string combined (event is Friday 17:00 - 20:00)
    event_start_str = f"{data['selected_date']} 17:00"
    
    # Create invoice for Women's Circle (Event ID = 99)
    invoice_url, invoice_id = await booking_service.create_event_invoice(
        user_id=current_user.id,
        event_id=99,
        event_name="Жіноче коло",
        date_str=event_start_str,
        price=400.0,
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
        
    # Standard date format for display
    dt_obj = datetime.strptime(data["selected_date"], "%Y-%m-%d")
    display_date = dt_obj.strftime("%d.%m.%Y")
    
    summary_text = (
        f"🍷 *Реєстрація на «Жіноче коло»:*\n\n"
        f"📅 Дата: *{display_date} (П'ятниця)*\n"
        f"⏰ Час: *17:00 - 20:00*\n"
        f"👤 Учасниця: *{data['client_name']}*\n"
        f"📞 Телефон: *{phone}*\n\n"
        f"💵 Вартість участі: *400.00 UAH*\n"
        f"💳 Передплата: *200.00 UAH* (решта 200.00 UAH сплачується при зустрічі)\n\n"
        f"⚠️ *Скасування та повернення передплати можливе не пізніше ніж за 24 години до початку заходу.*"
    )
    
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url, amount=200.0)
        )
    except Exception:
        await message.answer(
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url, amount=200.0)
        )
        
    if phone_prompt_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=phone_prompt_msg_id)
        except Exception:
            pass

    await state.set_state(WomensCircleFSM.ConfirmAndPay)
