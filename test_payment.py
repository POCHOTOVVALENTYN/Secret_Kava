import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.session import async_session_factory
from app.database.repositories.user import UserRepository
from app.database.models.tenant import Tenant
from app.database.models.psychologist import Psychologist
from app.services.booking import BookingService
from app.core.config import settings
import redis.asyncio as redis
import uuid
import httpx
from datetime import datetime

async def test_payment_chain():
    async with async_session_factory() as db:
        # Create user
        user_repo = UserRepository(db)
        user = await user_repo.get_by_telegram_id(999999)
        if not user:
            tenant = Tenant(name="Test Tenant", slug="test-tenant")
            db.add(tenant)
            await db.flush()
            
            user = await user_repo.create(
                telegram_id=999999,
                username="test_user",
                first_name="Тест",
                last_name="Тестовий",
                role="client",
                tenant_id=tenant.id
            )
            
            psych = Psychologist(
                tenant_id=tenant.id,
                name="Анна Зозуля",
                bio="Засновниця, психотерапевт",
                experience_years=10,
                specializations="Психолог",
                price_online=1000.0,
                price_offline=1200.0
            )
            db.add(psych)
            await db.flush()
            psych_id = psych.id
        else:
            # get psych
            psych_id = 1
            
        r_client = redis.from_url(settings.REDIS_URL)
        booking_service = BookingService(
            redis_client=r_client,
            db_session=db
        )
        
        # 1. Create Invoice
        invoice_url, invoice_id = await booking_service.create_consultation_invoice(
            user_id=user.id,
            psychologist_id=psych_id,
            format_type="online",
            date_str="2026-06-01",
            time_str="12:00",
            price=1000.0,
            client_name="Тест Тестовий"
        )
        
        print(f"Created Invoice: {invoice_id}")
        await db.commit()
        
    # 2. Trigger webhook
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/api/v1/payments/monobank/callback",
            json={
                "invoiceId": invoice_id,
                "status": "success",
                "amount": 100000,
                "modifiedDate": int(datetime.utcnow().timestamp())
            },
            headers={"x-sign": "test-signature"}
        )
        print(f"Webhook Response: {response.status_code} - {response.json()}")
        
if __name__ == "__main__":
    asyncio.run(test_payment_chain())
