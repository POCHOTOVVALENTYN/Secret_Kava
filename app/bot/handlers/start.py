# app/bot/handlers/start.py
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from app.bot.keyboards.inline import get_main_menu_keyboard
from structlog import get_logger

logger = get_logger()
router = Router(name="start_router")

# Cached file_id for the space interior image to speed up photo delivery
_ABOUT_PHOTO_FILE_ID = None


@router.message(CommandStart())
async def process_start_command(message: Message, state: FSMContext) -> None:
    """Welcomes client users and displays premium space presentation card."""
    data = await state.get_data()
    messages_to_delete = data.get("messages_to_delete", [])
    for msg_id in messages_to_delete:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=msg_id)
        except Exception:
            pass

    try:
        await message.delete()
    except Exception:
        pass

    await state.clear()
    welcome_text = (
        "✨ *Ласкаво просимо до нашого Психологічного Простору!* ✨\n\n"
        "Тут ви знайдете затишну атмосферу, професійну підтримку кваліфікованих "
        "психотерапевтів, комфортні кабінети для роботи та простір для розвитку.\n\n"
        "🌿 *Оберіть бажану опцію меню нижче:*"
    )

    user_id = message.from_user.id if message.from_user else None
    await message.answer(
        text=welcome_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard(user_id=user_id)
    )
    logger.info("user_started_bot", user_id=user_id)


@router.callback_query(F.data == "menu:main")
async def process_main_menu_callback(call: CallbackQuery, state: FSMContext) -> None:
    """Returns user to main menu (used from admin panel and other sections)."""
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
        
    await state.clear()
    welcome_text = (
        "✨ *Ласкаво просимо до нашого Психологічного Простору!* ✨\n\n"
        "Тут ви знайдете затишну атмосферу, професійну підтримку кваліфікованих "
        "психотерапевтів, комфортні кабінети для роботи та простір для розвитку.\n\n"
        "🌿 *Оберіть бажану опцію меню нижче:*"
    )
    user_id = call.from_user.id if call.from_user else None
    
    if call.message.photo or call.message.document:
        await call.message.answer(
            text=welcome_text,
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id=user_id)
        )
        try:
            await call.message.delete()
        except Exception:
            pass
    else:
        try:
            await call.message.edit_text(
                text=welcome_text,
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id=user_id)
            )
        except Exception:
            pass
    await call.answer()


@router.callback_query(F.data == "menu:contacts")
async def process_contacts_callback(call: CallbackQuery) -> None:
    """Displays contact sheet and map locations."""
    contacts_text = (
        "📞 *Наші контакти:*\n\n"
        "📍 *Адреса:* м. Одеса, вул. Канатна, 100/4\n"
        "⏰ *Графік роботи:* Щодня з 08:00 до 20:00\n"
        "📱 *Телефон:* +380938390509\n\n"
        "🛋️ _Завжди раді бачити вас у нашому просторі!_"
    )

    user_id = call.from_user.id if call.from_user else None
    await call.message.edit_text(
        text=contacts_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard(user_id=user_id)
    )
    await call.answer()


@router.callback_query(F.data == "menu:about")
async def process_about_callback(call: CallbackQuery) -> None:
    """Displays information about the space with a cozy aesthetic photo."""
    loading_text = "⏳ *Завантажуємо інформацію про простір... Будь ласка, зачекайте.*"
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
    
    import os
    from aiogram.types import FSInputFile
    
    about_text = (
        "🏢 *Про наш простір:*\n\n"
        "Secret Kava  — це затишне місце безпеки, прийняття та розвитку.\n"
        "Ми створили унікальну атмосферу гармонії терапевтичного простору та спокою "
        "за чашкою запашної кави. Тут ви знайдете підтримку найкращих спеціалістів, "
        "цікаві заходи та простір для відновлення внутрішніх сил.\n\n"
        "📍 м. Одеса, вул. Канатна, 100/4"
    )

    global _ABOUT_PHOTO_FILE_ID
    user_id = call.from_user.id if call.from_user else None
    
    async def clean_old_msg():
        try:
            await call.bot.delete_message(chat_id=call.message.chat.id, message_id=target_msg.message_id)
        except Exception:
            pass
        try:
            await call.message.delete()
        except Exception:
            pass

    if _ABOUT_PHOTO_FILE_ID:
        try:
            # Send new photo using Telegram file_id (instantaneous)
            await call.message.answer_photo(
                photo=_ABOUT_PHOTO_FILE_ID,
                caption=about_text,
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id=user_id)
            )
            await clean_old_msg()
            return
        except Exception as e:
            logger.warning("failed_to_send_via_cached_file_id_falling_back", error=str(e))

    # Path to assets image
    asset_path = os.path.join(os.path.dirname(__file__), "..", "assets", "secret_cava_interior.jpg")
    
    if os.path.exists(asset_path):
        photo = FSInputFile(asset_path)
        sent_msg = await call.message.answer_photo(
            photo=photo,
            caption=about_text,
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id=user_id)
        )
        
        # Save file_id for future instant loads
        if sent_msg.photo:
            _ABOUT_PHOTO_FILE_ID = sent_msg.photo[-1].file_id
            logger.info("stored_about_photo_file_id", file_id=_ABOUT_PHOTO_FILE_ID)

        await clean_old_msg()
    else:
        # Fallback to editing text if image is missing
        try:
            await target_msg.edit_text(
                text=about_text,
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id=user_id)
            )
        except Exception:
            pass
