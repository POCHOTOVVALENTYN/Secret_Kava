import asyncio
import os
import sys

# Add project root to python path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

async def main():
    from app.database.session import async_session_factory
    from app.services.booking import BookingService
    from app.integrations.google_sheets import GoogleSheetsClient
    from app.core.config import settings
    import redis.asyncio as redis

    sheets_client = GoogleSheetsClient(
        spreadsheet_id=settings.GOOGLE_SHEET_ID,
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else None,
        refresh_token=settings.GOOGLE_REFRESH_TOKEN.get_secret_value() if settings.GOOGLE_REFRESH_TOKEN else None,
        service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
    )

    print("Updating spreadsheet Row 2 Time to '10:00 - 15:00 (5 год.)'...")
    await sheets_client.update_cell("Заявки організаторів на заходи (Афіши)", row=3, col_letter="D", value="10:00 - 15:00 (5 год.)")
    
    redis_client = redis.from_url(settings.REDIS_URL)
    async with async_session_factory() as session:
        booking_service = BookingService(
            redis_client=redis_client,
            db_session=session,
            sheets_client=sheets_client
        )
        print("Clearing cache...")
        await redis_client.delete("cache:events_list")
        print("Fetching and caching events...")
        events = await booking_service.get_cached_events()
        print(f"Done! Cached {len(events)} events.")
        for e in events:
            print(f"- {e['title']} | ID {e['id']} | Status: {e['status']} | Date/Time: {e['date']}")
    await redis_client.close()

if __name__ == "__main__":
    asyncio.run(main())
