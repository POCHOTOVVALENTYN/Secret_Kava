# app/bot/handlers/payment_retry.py
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton
from structlog import get_logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.services.booking import BookingService
from app.database.models.transaction import Payment
from app.bot.keyboards.inline import get_payment_retry_direct_keyboard, get_main_menu_keyboard

logger = get_logger()
router = Router(name="payment_retry_router")

@router.callback_query(F.data.startswith("pay_retry:"))
async def process_payment_retry(call: CallbackQuery, db_session: AsyncSession, booking_service: BookingService) -> None:
    """Handles the user clicking 'Try Again' on a failed payment, regenerating the invoice if the slot is still free."""
    payment_id = int(call.data.split(":")[1])
    
    # 1. Show loading state to prevent double clicks and let the user know we're working
    await call.answer("⏳ Оновлюємо посилання на оплату...", show_alert=False)
    
    try:
        # 2. Call service to verify slots and generate a new invoice
        res = await booking_service.retry_payment(payment_id=payment_id, telegram_id=call.from_user.id)
        
        if res:
            invoice_url, order_id = res
            
            # Fetch the payment amount to display on the keyboard
            pay_query = select(Payment).where(Payment.id == payment_id)
            pay_res = await db_session.execute(pay_query)
            payment = pay_res.scalar_one_or_none()
            amount = float(payment.amount) if payment else 1.0
            
            # 3. Update the message with the new payment link
            new_text = (
                f"💳 **Нове посилання для оплати згенеровано успішно!**\n\n"
                f"Ми продовжили бронювання слота ще на *10 хвилин*.\n"
                f"Будь ласка, здійсніть оплату за допомогою кнопки нижче:"
            )
            
            await call.message.edit_text(
                text=new_text,
                parse_mode="Markdown",
                reply_markup=get_payment_retry_direct_keyboard(payment_id, invoice_url, amount)
            )
            logger.info("payment_retry_link_generated", payment_id=payment_id, order_id=order_id)
        else:
            # Slot is already booked by someone else!
            failed_text = (
                f"❌ **На жаль, час бронювання вичерпано**\n\n"
                f"Вибраний вами слот вже зайнято іншим користувачем. Будь ласка, спробуйте забронювати інший час через головне меню."
            )
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="⬅️ Головне меню", callback_data="menu:home"))
            
            await call.message.edit_text(
                text=failed_text,
                parse_mode="Markdown",
                reply_markup=builder.as_markup()
            )
            logger.warning("payment_retry_failed_slot_unavailable", payment_id=payment_id)
            
    except Exception as e:
        logger.error("payment_retry_error", payment_id=payment_id, error=str(e))
        await call.answer("⚠️ Сталася помилка при оновленні платежу. Спробуйте пізніше.", show_alert=True)

@router.callback_query(F.data.startswith("pay_cancel:"))
async def process_payment_cancel(call: CallbackQuery, db_session: AsyncSession, booking_service: BookingService) -> None:
    """Handles cancelling a failed booking, freeing locks and transitioning statuses to cancelled."""
    payment_id = int(call.data.split(":")[1])
    await call.answer("Скасовуємо...", show_alert=False)
    
    try:
        pay_query = select(Payment).where(Payment.id == payment_id)
        pay_res = await db_session.execute(pay_query)
        payment = pay_res.scalar_one_or_none()
        
        if payment:
            payment.status = "failed"
            from datetime import timedelta
            from zoneinfo import ZoneInfo
            kyiv_tz = ZoneInfo("Europe/Kyiv")
            
            # Transition bookings to cancelled status and free Redis locks immediately
            if payment.consultation_id:
                from app.database.models.booking import ConsultationBooking
                b_q = select(ConsultationBooking).where(ConsultationBooking.id == payment.consultation_id)
                b_res = await db_session.execute(b_q)
                booking = b_res.scalar_one_or_none()
                if booking:
                    booking.status = "cancelled"
                    local_start = booking.start_time.astimezone(kyiv_tz)
                    slot_date = local_start.strftime("%Y-%m-%d")
                    slot_time = local_start.strftime("%H:%M")
                    lock_key = f"lock:slot:{booking.psychologist_id}:{slot_date}:{slot_time}"
                    await booking_service.redis.delete(lock_key)
            elif payment.room_booking_id:
                from app.database.models.booking import RoomBooking
                b_q = select(RoomBooking).where(RoomBooking.id == payment.room_booking_id)
                b_res = await db_session.execute(b_q)
                booking = b_res.scalar_one_or_none()
                if booking:
                    booking.status = "cancelled"
                    local_start = booking.start_time.astimezone(kyiv_tz)
                    rent_date = local_start.strftime("%Y-%m-%d")
                    duration_hours = max(1, int((booking.end_time - booking.start_time).total_seconds() // 3600))
                    for h in range(duration_hours):
                        slot_time_str = (local_start + timedelta(hours=h)).strftime("%H:%M")
                        lock_key = f"lock:room_slot:{booking.room_id}:{rent_date}:{slot_time_str}"
                        await booking_service.redis.delete(lock_key)
            elif payment.event_booking_id:
                from app.database.models.booking import EventBooking
                b_q = select(EventBooking).where(EventBooking.id == payment.event_booking_id)
                b_res = await db_session.execute(b_q)
                booking = b_res.scalar_one_or_none()
                if booking:
                    booking.status = "cancelled"
                    lock_key = f"lock:event_seat:{booking.event_id}:{call.from_user.id}"
                    await booking_service.redis.delete(lock_key)
                    
            await db_session.commit()
            logger.info("payment_retry_cancelled_successfully", payment_id=payment_id)
            
        # Notify user and return to home
        await call.message.edit_text(
            text="❌ Бронювання скасовано. Оберіть нову послугу в меню нижче:",
            reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
        )
    except Exception as e:
        logger.error("payment_cancel_error", payment_id=payment_id, error=str(e))
        await call.message.edit_text(
            text="🌿 Оберіть бажану опцію меню нижче:",
            reply_markup=get_main_menu_keyboard(user_id=call.from_user.id)
        )
