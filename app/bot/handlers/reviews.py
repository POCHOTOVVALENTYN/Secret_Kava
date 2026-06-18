# app/bot/handlers/reviews.py
from datetime import datetime
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from app.database.models.review import Review
from app.database.models.user import User
from app.bot.keyboards.inline import get_payment_keyboard, get_main_menu_keyboard

logger = get_logger()
router = Router(name="reviews_router")

class ReviewFSM(StatesGroup):
    SelectRating = State()
    EnterReviewText = State()
    EnterNpsFeedback = State()

@router.callback_query(F.data == "menu:reviews")
async def process_reviews_menu(call: CallbackQuery, db_session: AsyncSession) -> None:
    """Displays curated reviews list and prompts to leave new review."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    
    try:
        query = (
            select(Review)
            .where(Review.is_moderated == True)
            .order_by(Review.id.desc())
            .limit(5)
            .options(selectinload(Review.user))
        )
        result = await db_session.execute(query)
        reviews = result.scalars().all()
    except Exception as e:
        logger.error("failed_to_fetch_reviews_from_db", error=str(e))
        reviews = []
        
    if reviews:
        reviews_formatted = []
        for r in reviews:
            stars = "⭐" * min(max(r.rating, 1), 5)
            author = r.user.first_name
            if r.user.last_name:
                author += f" {r.user.last_name}"
            reviews_formatted.append(f"{stars}\n«{r.comment}» — {author}")
        reviews_text = (
            "⭐ *Відгуки наших клієнтів:*\n\n" +
            "\n\n".join(reviews_formatted) +
            "\n\n🌿 _Ваша думка дуже важлива для нас! Ви можете залишити свій відгук нижче:_"
        )
    else:
        reviews_text = (
            "⭐ *Відгуки наших клієнтів:*\n\n"
            "🌿 *Наразі відгуків немає. Будьте першим, хто поділиться своїм враженням!*\n\n"
            "_Ваша думка дуже важлива для нас! Ви можете залишити свій відгук нижче:_"
        )
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✍️ Залишити відгук", callback_data="reviews:write"),
        InlineKeyboardButton(text="⬅️ Головне меню", callback_data="menu:home")
    )
    
    if call.message.photo or call.message.document:
        await call.message.answer(
            text=reviews_text,
            parse_mode="Markdown",
            reply_markup=builder.as_markup()
        )
        try:
            await call.message.delete()
        except Exception:
            pass
    else:
        try:
            await call.message.edit_text(
                text=reviews_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
        except Exception:
            pass
    await call.answer()

@router.callback_query(F.data == "reviews:write")
async def process_write_review_start(call: CallbackQuery, state: FSMContext) -> None:
    """Prompts client for rating stars selection."""
    await state.update_data(main_msg_id=call.message.message_id)
    
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⭐", callback_data="review_rating:1"),
        InlineKeyboardButton(text="⭐⭐", callback_data="review_rating:2"),
        InlineKeyboardButton(text="⭐⭐⭐", callback_data="review_rating:3")
    )
    builder.row(
        InlineKeyboardButton(text="⭐⭐⭐⭐", callback_data="review_rating:4"),
        InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data="review_rating:5")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад до відгуків", callback_data="menu:reviews"))
    
    await call.message.edit_text(
        text="⭐ *Будь ласка, оберіть вашу оцінку від 1 до 5 зірок:*",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await state.set_state(ReviewFSM.SelectRating)
    await call.answer()

@router.callback_query(F.data.startswith("review_rating:"), ReviewFSM.SelectRating)
async def process_review_rating(call: CallbackQuery, state: FSMContext) -> None:
    """Processes chosen star rating and requests text comment."""
    rating = int(call.data.split(":")[1])
    await state.update_data(review_rating=rating)
    
    await call.message.edit_text(
        text=f"✍️ *Ваша оцінка: {'⭐' * rating}*\n\nБудь ласка, напишіть Ваше враження про наш простір або сесію:",
        parse_mode="Markdown"
    )
    await state.set_state(ReviewFSM.EnterReviewText)
    await call.answer()
 
@router.message(ReviewFSM.EnterReviewText)
async def save_client_review(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    current_user: User,
    sheets=None
) -> None:
    """Saves new reviews as pending moderation to avoid spam publication."""
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    rating = data.get("review_rating", 5)

    # Delete the user's typed review immediately
    try:
        await message.delete()
    except Exception:
        pass

    if not message.text or len(message.text.strip()) < 10:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                text=f"⚠️ *Будь ласка, напишіть більш розгорнутий відгук (мінімум 10 символів):*\n\n"
                     f"✍️ *Ваша оцінка: {'⭐' * rating}*\n\nБудь ласка, напишіть Ваше враження про наш простір або сесію:",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return

    review_text = message.text.strip()
    
    # Save review in PostgreSQL DB
    try:
        new_review = Review(
            user_id=current_user.id,
            rating=rating,
            comment=review_text,
            is_moderated=False
        )
        db_session.add(new_review)
        await db_session.flush()
        
        # Save to Google Sheets
        if sheets:
            now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            client_name = current_user.first_name
            if current_user.last_name:
                client_name += f" {current_user.last_name}"
            # Columns: Дата створення | Клієнт | Оцінка | Текст відгуку | Статус
            row_data = [now_str, client_name, f"{rating} зірок", review_text, "На модерації"]
            await sheets.append_row("Реєстр відгуків", row_data)
            
        logger.info("review_saved_pending_moderation", comment=review_text)
    except Exception as e:
        logger.error("failed_to_save_review_in_db", error=str(e))
        
    await state.clear()
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            text="✅ *Дякуємо за ваш відгук!* Він буде опублікований після перевірки модератором.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id=message.from_user.id)
        )
    except Exception:
        await message.answer(
            text="✅ *Дякуємо за ваш відгук!* Він буде опублікований після перевірки модератором.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id=message.from_user.id)
        )

@router.callback_query(F.data.startswith("nps:"))
async def process_nps_rating(
    call: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession
) -> None:
    """Captures NPS callback scores triggered after session completions and prompts for details."""
    params = call.data.split(":")
    booking_id = int(params[1])
    score = int(params[2])
    
    logger.info("nps_score_captured", booking=booking_id, score=score)
    await state.update_data(nps_booking_id=booking_id, nps_score=score, main_msg_id=call.message.message_id)
    
    if score <= 6:
        await call.message.edit_text(
            text=f"💚 *Дякуємо за оцінку ({score}/10)!*\n\n"
                 f"Ми дуже шкодуємо, що ви залишилися незадоволені. Будь ласка, напишіть кількома словами, що саме пішло не так або як ми можемо покращити наш сервіс:",
            parse_mode="Markdown"
        )
        await state.set_state(ReviewFSM.EnterNpsFeedback)
    elif score >= 9:
        await call.message.edit_text(
            text=f"💚 *Дякуємо за оцінку ({score}/10)!*\n\n"
                 f"Раді чути! Будь ласка, напишіть короткий відгук про роботу нашого простору, щоб ми могли опублікувати його у розділі відгуків:",
            parse_mode="Markdown"
        )
        await state.set_state(ReviewFSM.EnterNpsFeedback)
    else:
        # Neutral feedback (7-8), just thank them and do not prompt
        await call.message.edit_text(
            text=f"💚 *Дякуємо за оцінку ({score}/10)!* Ви допомагаєте нам ставати кращими.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
        )
        await state.clear()
    await call.answer()

@router.message(ReviewFSM.EnterNpsFeedback)
async def save_nps_feedback(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    current_user: User,
    sheets=None
) -> None:
    """Saves detailed NPS review text to the DB and Google Sheets."""
    data = await state.get_data()
    main_msg_id = data.get("main_msg_id")
    score = data.get("nps_score", 10)
    
    # Convert NPS score (1-10) to stars (1-5)
    rating = max(1, min(5, round(score / 2)))

    try:
        await message.delete()
    except Exception:
        pass

    if not message.text or len(message.text.strip()) < 5:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=main_msg_id,
                text=f"⚠️ *Будь ласка, напишіть трохи детальніше (мінімум 5 символів):*\n\n"
                     f"💚 *Дякуємо за оцінку ({score}/10)!* Будь ласка, поділіться вашими думками:",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return
        
    text = message.text.strip()
    
    try:
        # Save as review in DB
        new_review = Review(
            user_id=current_user.id,
            rating=rating,
            comment=text,
            is_moderated=False
        )
        db_session.add(new_review)
        await db_session.flush()
        
        # Save to Google Sheets
        if sheets:
            now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            client_name = current_user.first_name
            if current_user.last_name:
                client_name += f" {current_user.last_name}"
            row_data = [now_str, client_name, f"{score}/10 (NPS)", text, "На модерації"]
            await sheets.append_row("Реєстр відгуків", row_data)
            
        logger.info("nps_feedback_saved", comment=text)
    except Exception as e:
        logger.error("failed_to_save_nps_feedback", error=str(e))
        
    await state.clear()
    try:
        await message.bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=main_msg_id,
            text="💚 *Дякуємо за ваш фідбек!* Ваша думка допомагає нам ставати кращими.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id=message.from_user.id)
        )
    except Exception:
        await message.answer(
            text="💚 *Дякуємо за ваш фідбек!* Ваша думка допомагає нам ставати кращими.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id=message.from_user.id)
        )
