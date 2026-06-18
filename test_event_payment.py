import asyncio
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import redis.asyncio as redis
import uuid

from app.database.session import async_session_factory
from app.database.repositories.user import UserRepository
from app.database.models.tenant import Tenant
from app.database.models.booking import EventBooking
from app.database.models.transaction import Payment
from app.services.booking import BookingService
from app.core.config import settings

async def test_event_payment_chain():
    print("🚀 Starting Event Booking Integration Test...")
    
    async with async_session_factory() as db:
        # 1. Resolve tenant and user
        user_repo = UserRepository(db)
        user = await user_repo.get_by_telegram_id(999999)
        
        if not user:
            tenant = Tenant(name="Test Studio Tenant", slug="test-studio-tenant")
            db.add(tenant)
            await db.flush()
            
            user = await user_repo.create(
                telegram_id=999999,
                username="test_event_user",
                first_name="Олексій",
                last_name="Тестовий",
                role="client",
                tenant_id=tenant.id
            )
            await db.flush()
            
        r_client = redis.from_url(settings.REDIS_URL)
        
        # Instantiate BookingService without real Google integrations to avoid expired token crashes
        booking_service = BookingService(
            redis_client=r_client,
            db_session=db,
            gcal_client=None,
            payment_client=None, # mock mode triggers automatically
            sheets_client=None,  # skip real Google Sheets sync for test
            scheduler=None
        )
        
        # 2. Create Event Invoice
        print("🎟️ Generating event invoice...")
        invoice_url, invoice_id = await booking_service.create_event_invoice(
            user_id=user.id,
            event_id=1,
            event_name="Воркшоп «Справитися зі стресом»",
            date_str="2026-05-30 18:00",
            price=400.0,
            client_name="Олексій Тестовий",
            client_phone="+380991234567"
        )
        
        print(f"✅ Pending Event Booking & Payment created! Invoice URL: {invoice_url}")
        await db.commit()
        await r_client.close()

    # 3. Simulate payment webhook callback via BookingService
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
        
        print(f"💳 Settle invoice {invoice_id}...")
        await booking_service.confirm_payment_and_booking(invoice_id)
        await db.commit()
        await r_client.close()

    # 4. Verify DB State
    async with async_session_factory() as db:
        # Check event booking
        eb_query = select(EventBooking).where(EventBooking.user_id == user.id)
        eb_res = await db.execute(eb_query)
        bookings = eb_res.scalars().all()
        
        print(f"\n📊 --- VERIFICATION RESULT ---")
        print(f"Number of bookings for test user: {len(bookings)}")
        for idx, booking in enumerate(bookings, 1):
            print(f"Booking #{idx}: ID={booking.id}, Event='{booking.event_name}', Price={booking.price}, Status={booking.status}")
            assert booking.status == "paid", f"Expected paid status, got {booking.status}"
            
        # Check payment record
        pay_query = select(Payment).where(Payment.event_booking_id.isnot(None))
        pay_res = await db.execute(pay_query)
        payments = pay_res.scalars().all()
        
        print(f"Number of event payments: {len(payments)}")
        for idx, payment in enumerate(payments, 1):
            print(f"Payment #{idx}: ID={payment.id}, Amount={payment.amount}, Status={payment.status}, InvoiceID={payment.invoice_id}")
            assert payment.status == "success", f"Expected success status, got {payment.status}"
            
        print("✅ SUCCESS: All database integrity and state tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(test_event_payment_chain())
