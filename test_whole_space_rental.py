import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select
import redis.asyncio as redis
import uuid

from app.database.session import async_session_factory
from app.database.repositories.user import UserRepository
from app.database.models.tenant import Tenant
from app.database.models.psychologist import Psychologist
from app.database.models.booking import ConsultationBooking, RoomBooking, RoomRentalSlot, Room
from app.database.models.transaction import Payment
from app.services.booking import BookingService
from app.core.config import settings

async def run_whole_space_test():
    print("🚀 Starting Whole Space Lease Integration & Overlap Tests...")
    
    async with async_session_factory() as db:
        # 1. Resolve tenant and user
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
            
        # Ensure default rooms (1 and 2) are present
        room1 = await db.get(Room, 1)
        if not room1:
            room1 = Room(id=1, tenant_id=user.tenant_id, name="Головний кабінет", description="Cabinet 1", hourly_rate=200.0)
            db.add(room1)
        room2 = await db.get(Room, 2)
        if not room2:
            room2 = Room(id=2, tenant_id=user.tenant_id, name="Весь простір", description="Whole Space", hourly_rate=500.0)
            db.add(room2)
        await db.flush()
        
        # Ensure default psychologist
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
            payment_client=None,
            sheets_client=None,
            scheduler=None
        )
        
        # Date selection (let's pick a Friday and a Wednesday)
        wednesday_str = "2026-06-17"  # Wednesday
        friday_str = "2026-06-19"     # Friday
        
        # Clean any existing mock slot records on these dates
        from sqlalchemy import delete
        await db.execute(delete(RoomRentalSlot).where(RoomRentalSlot.date.in_([wednesday_str, friday_str])))
        
        # Also clean up any overlapping consultations or room bookings on these dates
        day1_start = datetime.combine(datetime.strptime(wednesday_str, "%Y-%m-%d").date(), datetime.min.time())
        day2_end = datetime.combine(datetime.strptime(friday_str, "%Y-%m-%d").date(), datetime.max.time())
        
        await db.execute(delete(ConsultationBooking).where(ConsultationBooking.start_time >= day1_start, ConsultationBooking.start_time <= day2_end))
        await db.execute(delete(RoomBooking).where(RoomBooking.start_time >= day1_start, RoomBooking.start_time <= day2_end))
        
        # Seed Room 1 slots to test double-blocking later
        for h in range(8, 20):
            t_str = f"{h:02d}:00"
            for rid in (1,):
                slot = RoomRentalSlot(room_id=rid, date=wednesday_str, time=t_str, is_booked=False)
                db.add(slot)
                slot = RoomRentalSlot(room_id=rid, date=friday_str, time=t_str, is_booked=False)
                db.add(slot)
        await db.flush()
        
        # Clear Redis locks for test dates
        for h in range(8, 20):
            t_str = f"{h:02d}:00"
            await r_client.delete(f"lock:room_slot:1:{wednesday_str}:{t_str}")
            await r_client.delete(f"lock:room_slot:2:{wednesday_str}:{t_str}")
            await r_client.delete(f"lock:room_slot:1:{friday_str}:{t_str}")
            await r_client.delete(f"lock:room_slot:2:{friday_str}:{t_str}")
            
        # Test 1: Prepayment amount for Room ID = 2
        print("\n--- Test 1: Room 2 Prepayment Amount Verification (Expect 200 UAH) ---")
        invoice_url, invoice_id = await booking_service.create_rental_invoice(
            user_id=user.id,
            room_id=2,
            date_str=wednesday_str,
            time_str="12:00",
            hours=2,
            price=1000.0,
            client_name="Олексій Тестовий",
            client_phone="+380991234567"
        )
        
        # Verify pending payment is 200 UAH
        pay_query = select(Payment).where(Payment.invoice_id == invoice_id)
        pay_res = await db.execute(pay_query)
        payment = pay_res.scalar_one()
        assert float(payment.amount) == 200.0, f"Expected 200.0, got {payment.amount}"
        print(f"✅ Success: WayForPay invoice prepayment is exactly {payment.amount} UAH")
        
        # Test 2: Friday 17:00 - 20:00 reservation block for 'Жіноче коло'
        print("\n--- Test 2: Friday 17:00 - 20:00 Block Verification ---")
        friday_slots = await booking_service.get_available_room_slots(room_id=2, date_str=friday_str)
        blocked_hours = ["17:00", "18:00", "19:00"]
        for bh in blocked_hours:
            assert bh not in friday_slots, f"Friday slot {bh} should be blocked"
        print("✅ Success: Friday 17:00 - 20:00 slots are correctly blocked")
        
        # Test 3: Overlap check with ONLINE consultation (should NOT block Room 2)
        print("\n--- Test 3: Online Consultation Overlap Verification ---")
        online_consultation = ConsultationBooking(
            user_id=user.id,
            psychologist_id=psych.id,
            format="online",
            start_time=datetime.strptime(f"{wednesday_str} 10:00", "%Y-%m-%d %H:%M"),
            end_time=datetime.strptime(f"{wednesday_str} 11:00", "%Y-%m-%d %H:%M"),
            status="confirmed",
            price=1000.0
        )
        db.add(online_consultation)
        await db.flush()
        
        slots_after_online = await booking_service.get_available_room_slots(room_id=2, date_str=wednesday_str)
        print(f"Available slots after online consultation: {slots_after_online}")
        assert "10:00" in slots_after_online, "Online consultation should not block Whole Space"
        print("✅ Success: Online consultation does not block whole space slots")
        
        # Test 4: Overlap check with OFFLINE consultation (SHOULD block Room 2)
        print("\n--- Test 4: Offline Consultation Overlap Verification ---")
        offline_consultation = ConsultationBooking(
            user_id=user.id,
            psychologist_id=psych.id,
            format="offline",
            start_time=datetime.strptime(f"{wednesday_str} 14:00", "%Y-%m-%d %H:%M"),
            end_time=datetime.strptime(f"{wednesday_str} 15:00", "%Y-%m-%d %H:%M"),
            status="confirmed",
            price=1200.0
        )
        db.add(offline_consultation)
        await db.flush()
        
        slots_after_offline = await booking_service.get_available_room_slots(room_id=2, date_str=wednesday_str)
        assert "14:00" not in slots_after_offline, "Offline consultation should block Whole Space slot"
        print("✅ Success: Offline consultation correctly blocks whole space slots")
        
        # Test 5: Overlap check with Room ID = 1 booking (SHOULD block Room 2)
        print("\n--- Test 5: Room 1 Booking Overlap Verification ---")
        room1_booking = RoomBooking(
            user_id=user.id,
            room_id=1,
            start_time=datetime.strptime(f"{wednesday_str} 16:00", "%Y-%m-%d %H:%M"),
            end_time=datetime.strptime(f"{wednesday_str} 18:00", "%Y-%m-%d %H:%M"),
            status="confirmed",
            price=400.0
        )
        db.add(room1_booking)
        await db.flush()
        
        slots_after_room1 = await booking_service.get_available_room_slots(room_id=2, date_str=wednesday_str)
        assert "16:00" not in slots_after_room1, "Room 1 booking should block Whole Space slot"
        assert "17:00" not in slots_after_room1, "Room 1 booking should block consecutive Whole Space slot"
        print("✅ Success: Room 1 booking correctly blocks whole space slots")
        
        # Test 6: Room ID = 2 Confirmation blocks Room ID = 1 slots
        print("\n--- Test 6: Settle Room 2 Lease and Block Room 1 Slots Verification ---")
        # Settle the Room 2 booking we created in Test 1
        await booking_service.confirm_payment_and_booking(invoice_id)
        
        # Verify Room 1 slots for 12:00 and 13:00 are now marked booked
        r1_slots_query = select(RoomRentalSlot).where(
            RoomRentalSlot.room_id == 1,
            RoomRentalSlot.date == wednesday_str,
            RoomRentalSlot.time.in_(["12:00", "13:00"])
        )
        r1_slots_res = await db.execute(r1_slots_query)
        r1_slots = r1_slots_res.scalars().all()
        
        assert len(r1_slots) == 2
        for s in r1_slots:
            assert s.is_booked is True, f"Room 1 slot at {s.time} should be blocked after Room 2 is booked"
        print("✅ Success: Settle Whole Space lease blocks individual cabinet slots")
        
        await db.commit()
        await r_client.close()
        
    print("\n🎉 ALL WHOLE SPACE LEASE TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    asyncio.run(run_whole_space_test())
