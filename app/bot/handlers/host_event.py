# app/bot/handlers/host_event.py
import re
import os
import asyncio
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, FSInputFile
from aiogram.fsm.context import FSMContext
from structlog import get_logger

from app.bot.states.booking import HostEventFSM
from app.bot.keyboards.inline import get_payment_keyboard, get_main_menu_keyboard
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
        "💳 Для реєстрації заявки вноситься передплата: *100.00 UAH* (кошти зараховуються в рахунок оренди).\n\n"
        "✍️ *Будь ласка, введіть назву вашого заходу:*"
    )
    
    # Simple cancel button
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
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
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{title}*\n\n"
                    f"✍️ *Введіть ім'я ведучого/організатора заходу:*",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        pass
        
    await state.set_state(HostEventFSM.EnterHost)

@router.message(HostEventFSM.EnterHost)
async def process_event_host(message: Message, state: FSMContext) -> None:
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
                        f"⚠️ *Введіть коректне ім'я ведучого (мінімум 2 символи):*\n"
                        f"✍️ *Введіть ім'я ведучого/організатора заходу:*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(host=host)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{data['title']}*\n"
                    f"👤 Ведучий: *{host}*\n\n"
                    f"✍️ *Введіть бажану дату та час проведення заходу (наприклад, 25.06 о 18:00):*",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        pass
        
    await state.set_state(HostEventFSM.EnterDate)

@router.message(HostEventFSM.EnterDate)
async def process_event_date(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    
    try:
        await message.delete()
    except Exception:
        pass
        
    date_str = message.text.strip() if message.text else ""
    if not date_str or len(date_str) < 4:
        try:
            await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                caption=f"🎭 Захід: *{data['title']}*\n"
                        f"👤 Ведучий: *{data['host']}*\n\n"
                        f"⚠️ *Введіть коректну дату та час (наприклад, 25.06 о 18:00):*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(date=date_str)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{data['title']}*\n"
                    f"👤 Ведучий: *{data['host']}*\n"
                    f"📅 Дата: *{date_str}*\n\n"
                    f"✍️ *Введіть ліміт місць (кількість учасників від 4 до 20 включно):*",
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
    except Exception:
        pass
        
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
                        f"📅 Дата: *{data['date']}*\n\n"
                        f"⚠️ *Введіть коректне ціле число від 4 до 20 для ліміту місць:*",
                parse_mode="Markdown",
                reply_markup=message.bot.reply_markup
            )
        except Exception:
            pass
        return
        
    await state.update_data(limit=limit)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{data['title']}*\n"
                    f"👤 Ведучий: *{data['host']}*\n"
                    f"📅 Дата: *{data['date']}*\n"
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
                        f"📅 Дата: *{data['date']}*\n"
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
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="booking:cancel"))
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=f"🎭 Захід: *{data['title']}*\n"
                    f"👤 Ведучий: *{data['host']}*\n"
                    f"📅 Дата: *{data['date']}*\n"
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
                        f"👤 Ведучий: *{data['host']}*\n\n"
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
    
    # Generate WayForPay invoice for 100 UAH prepayment
    invoice_url, invoice_id = await booking_service.create_host_event_invoice(
        user_id=current_user.id,
        title=data["title"],
        host=data["host"],
        date_str=data["date"],
        limit=data["limit"],
        price=data["price"],
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
        
    summary_text = (
        f"💳 *Розрахунок реєстрації заходу:*\n\n"
        f"🎭 Назва: *{data['title']}*\n"
        f"👤 Ведучий: *{data['host']}*\n"
        f"📅 Дата: *{data['date']}*\n"
        f"👥 Ліміт місць: *{data['limit']}*\n"
        f"💵 Вартість для клієнтів: *{data['price']} грн*\n"
        f"👤 Заявник: *{data['client_name']}*\n"
        f"📞 Телефон: *{phone}*\n\n"
        f"💳 Передплата: *100.00 UAH*\n\n"
        f"⚠️ _Заявка буде надіслана на модерацію та внесена до афіші після успішної оплати передплати._"
    )
    
    try:
        await message.bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            caption=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url, amount=100.0)
        )
    except Exception:
        await message.answer(
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_payment_keyboard(invoice_url, amount=100.0)
        )
        
    if phone_prompt_msg_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=phone_prompt_msg_id)
        except Exception:
            pass
            
    await state.set_state(HostEventFSM.ConfirmAndPay)
