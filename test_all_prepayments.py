import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select
import redis.asyncio as redis
import uuid

from app.database.session import async_session_factory
from app.database.repositories.user import UserRepository
from app.database.models.tenant import Tenant
from app.database.models.psychologist import Psychologist
from app.database.models.booking import ConsultationBooking, RoomBooking, EventBooking
from app.database.models.transaction import Payment
from app.services.booking import BookingService
from app.core.config import settings

async def run_prepayment_test():
    print("🚀 Starting Unified Prepayment & Cash removal Integration Tests...")
    
    async with async_session_factory() as db:
        # Resolve tenant and user
        user_repo = UserRepository(db)
        user = await user_repo.get_by_telegram_id(999999)
        
        if not user:
            tenant = Tenant(name="Test Tenant", slug="test-tenant")
            db.add(tenant)
            await db.flush()
            
            user = await user_repo.create(
                telegram_id=999999,
                username="test_prepay_user",
                first_name="Олексій",
                last_name="Тестовий",
                role="client",
                tenant_id=tenant.id
            )
            await db.flush()
            
        # Get or create psychologist for booking test
        psych_query = select(Psychologist).where(Psychologist.name == "Анна Зозуля")
        psych_res = await db.execute(psych_query)
        psych = psych_res.scalar_one_or_none()
        if not psych:
            psych = Psychologist(
                tenant_id=user.tenant_id,
                name="Анна Зозуля",
                bio="Засновниця, психотерапевт",
                experience_years=10,
                specializations="Психолог",
                price_online=1000.0,
                price_offline=1200.0
            )
            db.add(psych)
            await db.flush()
        
        r_client = redis.from_url(settings.REDIS_URL)
        
        booking_service = BookingService(
            redis_client=r_client,
            db_session=db,
            gcal_client=None,
            payment_client=None, # mock mode generates mock invoices
            sheets_client=None,
            scheduler=None
        )
        
        # ----------------------------------------------------
        # TEST 1: Consultation Prepayment Flow (50 UAH)
        # ----------------------------------------------------
        print("\n--- Test 1: Consultation Prepayment (Full price = 1000.0 UAH) ---")
        c_invoice_url, c_invoice_id = await booking_service.create_consultation_invoice(
            user_id=user.id,
            psychologist_id=psych.id,
            format_type="online",
            date_str="2026-06-20",
            time_str="14:00",
            price=1000.0,
            client_name="Олексій Тестовий",
            client_phone="+380991234567"
        )
        print(f"Generated Consultation Invoice URL: {c_invoice_url}")
        
        # ----------------------------------------------------
        # TEST 2: Room Rental Prepayment Flow (50 UAH)
        # ----------------------------------------------------
        print("\n--- Test 2: Room Rental Prepayment (Full price = 400.0 UAH) ---")
        r_invoice_url, r_invoice_id = await booking_service.create_rental_invoice(
            user_id=user.id,
            room_id=1,
            date_str="2026-06-21",
            time_str="12:00",
            hours=2,
            price=400.0,
            client_name="Олексій Тестовий",
            client_phone="+380991234567"
        )
        print(f"Generated Room Rental Invoice URL: {r_invoice_url}")
        
        # ----------------------------------------------------
        # TEST 3: Event Prepayment Flow (50 UAH)
        # ----------------------------------------------------
        print("\n--- Test 3: Event Prepayment (Full price = 300.0 UAH) ---")
        e_invoice_url, e_invoice_id = await booking_service.create_event_invoice(
            user_id=user.id,
            event_id=1,
            event_name="Воркшоп «Справитися зі стресом»",
            date_str="2026-06-22 18:00",
            price=300.0,
            client_name="Олексій Тестовий",
            client_phone="+380991234567"
        )
        print(f"Generated Event Invoice URL: {e_invoice_url}")
        
        await db.commit()
        await r_client.close()

    # ----------------------------------------------------
    # SETTLEMENT & DATABASE STATE VERIFICATION
    # ----------------------------------------------------
    print("\n--- Settling Invoices via Callback simulation ---")
    async with async_session_factory() as db:
        r_client = redis.from_url(settings.REDIS_URL)
        booking_service = BookingService(
            redis_client=r_client,
            db_session=db,
            gcal_client=None,
            payment_client=None,
            sheets_client=None,
            scheduler=None
        )
        
        print(f"Settling Consultation Invoice: {c_invoice_id}")
        await booking_service.confirm_payment_and_booking(c_invoice_id)
        
        print(f"Settling Room Rental Invoice: {r_invoice_id}")
        await booking_service.confirm_payment_and_booking(r_invoice_id)
        
        print(f"Settling Event Invoice: {e_invoice_id}")
        await booking_service.confirm_payment_and_booking(e_invoice_id)
        
        await db.commit()
        await r_client.close()

    # ----------------------------------------------------
    # DB VERIFICATION CHECKS
    # ----------------------------------------------------
    print("\n--- Verification of Database State ---")
    async with async_session_factory() as db:
        # 1. Verify Consultation
        c_query = select(ConsultationBooking).where(ConsultationBooking.user_id == user.id).order_by(ConsultationBooking.id.desc())
        c_res = await db.execute(c_query)
        c_booking = c_res.scalars().first()
        
        assert c_booking is not None, "Consultation booking should be present"
        print(f"Consultation Booking: ID={c_booking.id}, Price={c_booking.price} (Expected 1000.00), Status={c_booking.status} (Expected confirmed)")
        assert float(c_booking.price) == 1000.0, "Expected full price of 1000.0"
        assert c_booking.status == "confirmed", "Expected confirmed status"
        
        c_pay_query = select(Payment).where(Payment.consultation_id == c_booking.id)
        c_pay_res = await db.execute(c_pay_query)
        c_payment = c_pay_res.scalars().first()
        assert c_payment is not None
        print(f"Consultation Payment Amount: {c_payment.amount} (Expected 50.0), Status: {c_payment.status} (Expected success)")
        assert float(c_payment.amount) == 50.0
        assert c_payment.status == "success"

        # 2. Verify Room Rental
        r_query = select(RoomBooking).where(RoomBooking.user_id == user.id).order_by(RoomBooking.id.desc())
        r_res = await db.execute(r_query)
        r_booking = r_res.scalars().first()
        
        assert r_booking is not None
        print(f"Room Rental Booking: ID={r_booking.id}, Price={r_booking.price} (Expected 400.0), Status={r_booking.status} (Expected confirmed)")
        assert float(r_booking.price) == 400.0
        assert r_booking.status == "confirmed"
        
        r_pay_query = select(Payment).where(Payment.room_booking_id == r_booking.id)
        r_pay_res = await db.execute(r_pay_query)
        r_payment = r_pay_res.scalars().first()
        assert r_payment is not None
        print(f"Room Rental Payment Amount: {r_payment.amount} (Expected 50.0), Status: {r_payment.status} (Expected success)")
        assert float(r_payment.amount) == 50.0
        assert r_payment.status == "success"

        # 3. Verify Event
        e_query = select(EventBooking).where(EventBooking.user_id == user.id).order_by(EventBooking.id.desc())
        e_res = await db.execute(e_query)
        e_booking = e_res.scalars().first()
        
        assert e_booking is not None
        print(f"Event Booking: ID={e_booking.id}, Price={e_booking.price} (Expected 300.0), Status={e_booking.status} (Expected paid)")
        assert float(e_booking.price) == 300.0
        assert e_booking.status == "paid"
        
        e_pay_query = select(Payment).where(Payment.event_booking_id == e_booking.id)
        e_pay_res = await db.execute(e_pay_query)
        e_payment = e_pay_res.scalars().first()
        assert e_payment is not None
        print(f"Event Payment Amount: {e_payment.amount} (Expected 50.0), Status: {e_payment.status} (Expected success)")
        assert float(e_payment.amount) == 50.0
        assert e_payment.status == "success"
        
        print("\n🎉 ALL TESTS PASSED SUCCESSFULLY! Invoices are exactly 50 UAH and bookings retain full values.")

if __name__ == "__main__":
    asyncio.run(run_prepayment_test())
