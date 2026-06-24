import asyncio
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import redis.asyncio as redis

from app.database.session import async_session_factory
from app.database.repositories.user import UserRepository
from app.database.models.tenant import Tenant
from app.database.models.booking import EventBooking
from app.database.models.transaction import Payment
from app.services.booking import BookingService
from app.core.config import settings

async def test_womens_circle_logic():
    print("🚀 Starting Women's Circle Integration Test...")
    
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
            
        # Clean up any lingering test bookings
        from sqlalchemy import delete
        await db.execute(delete(EventBooking).where(EventBooking.event_id == 99))
        await db.execute(delete(Payment).where(Payment.event_booking_id.isnot(None)))
        await db.flush()
            
        r_client = redis.from_url(settings.REDIS_URL)
        
        booking_service = BookingService(
            redis_client=r_client,
            db_session=db,
            gcal_client=None,
            payment_client=None, # mock mode triggers automatically
            sheets_client=None,  # skip real Google Sheets sync for test
            scheduler=None
        )
        
        # 2. Get upcoming fridays
        print("📅 Retrieving upcoming Fridays...")
        fridays = await booking_service.get_upcoming_fridays()
        assert len(fridays) == 4, f"Expected 4 upcoming Fridays, got {len(fridays)}"
        print(f"Upcoming Fridays: {fridays}")
        
        # Let's select the first friday date
        target_date = fridays[0]["date_str"]
        event_start = datetime.combine(datetime.strptime(target_date, "%Y-%m-%d").date(), datetime.strptime("17:00", "%H:%M").time())
        
        # 3. Create Event Invoice for Women's Circle (event_id = 99)
        print("🎟& Generating event invoice for Women's Circle...")
        invoice_url, invoice_id = await booking_service.create_event_invoice(
            user_id=user.id,
            event_id=99,
            event_name="Жіноче коло",
            date_str=f"{target_date} 17:00",
            price=400.0,
            client_name="Олексій Тестовий",
            client_phone="+380991234567"
        )
        
        print(f"✅ Pending Women's Circle Booking & Payment created! Invoice URL: {invoice_url}")
        
        # Check prepay amount is 200 UAH
        payment_query = select(Payment).where(Payment.invoice_id == invoice_id)
        res = await db.execute(payment_query)
        pay_record = res.scalar_one()
        print(f"Payment record amount: {pay_record.amount} (Expected: 200.00)")
        assert float(pay_record.amount) == 200.0, f"Expected prepay amount 200.0 UAH, got {pay_record.amount}"
        
        await db.commit()
        await r_client.close()

    # 4. Simulate payment webhook callback via BookingService
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

    # 5. Verify DB State and Availability with 17 seats limit
    async with async_session_factory() as db:
        eb_query = select(EventBooking).where(EventBooking.user_id == user.id, EventBooking.event_id == 99)
        eb_res = await db.execute(eb_query)
        bookings = eb_res.scalars().all()
        
        print(f"\n📊 --- VERIFICATION RESULT ---")
        print(f"Number of bookings for test user: {len(bookings)}")
        for idx, booking in enumerate(bookings, 1):
            print(f"Booking #{idx}: ID={booking.id}, Event='{booking.event_name}', Price={booking.price}, Status={booking.status}")
            assert booking.status == "paid", f"Expected paid status, got {booking.status}"
            
        r_client = redis.from_url(settings.REDIS_URL)
        booking_service = BookingService(
            redis_client=r_client,
            db_session=db,
            gcal_client=None,
            payment_client=None,
            sheets_client=None,
            scheduler=None
        )
        fridays = await booking_service.get_upcoming_fridays()
        print(f"Updated Fridays status: {fridays}")
        assert fridays[0]["booked_count"] == 1, f"Expected 1 booked slot, got {fridays[0]['booked_count']}"
        assert fridays[0]["available"] is True, "First Friday should still be available"
        
        # Create 16 more mock bookings for the same Friday to hit the 17 seats limit
        print("Creating 16 more mock bookings to test capacity limit...")
        for j in range(16):
            mock_booking = EventBooking(
                user_id=user.id,
                event_id=99,
                event_name="Жіноче коло",
                start_time=event_start,
                price=400.0,
                status="paid",
                client_name=f"Mock User {j}",
                client_phone=f"+3809900000{j:02d}"
            )
            db.add(mock_booking)
        await db.commit()
        
        fridays = await booking_service.get_upcoming_fridays()
        print(f"Capacity test Friday status: {fridays[0]}")
        assert fridays[0]["booked_count"] == 17, f"Expected 17 booked slots, got {fridays[0]['booked_count']}"
        assert fridays[0]["available"] is False, "First Friday should be unavailable after 17 bookings"
        print("✅ Capacity limit test passed!")
        
        # Cleanup mock bookings
        print("Cleaning up database records...")
        cleanup_query = select(EventBooking).where(EventBooking.event_id == 99)
        res = await db.execute(cleanup_query)
        for b in res.scalars().all():
            await db.delete(b)
            
        cleanup_payments = select(Payment).where(Payment.event_booking_id.isnot(None))
        res_p = await db.execute(cleanup_payments)
        for p in res_p.scalars().all():
            await db.delete(p)
            
        await db.commit()
        await r_client.close()
        print("✅ SUCCESS: All tests passed!")

if __name__ == "__main__":
    asyncio.run(test_womens_circle_logic())
