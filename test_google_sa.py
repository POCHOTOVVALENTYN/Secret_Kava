import asyncio
from app.integrations.google_sheets import GoogleSheetsClient
from app.core.config import settings

async def test_sa():
    print("🤖 Checking Google Service Account integration...")
    print(f"Spreadsheet ID: {settings.GOOGLE_SHEET_ID}")
    print(f"Service Account file: {settings.GOOGLE_SERVICE_ACCOUNT_FILE}")
    
    sheets = GoogleSheetsClient(
        spreadsheet_id=settings.GOOGLE_SHEET_ID,
        service_account_file=settings.GOOGLE_SERVICE_ACCOUNT_FILE
    )
    
    try:
        print("📖 Reading sheet headers from 'Бронювання спеціалісти'...")
        values = await sheets.read_sheet("Бронювання спеціалісти", "A1:H2")
        print(f"🎉 Successfully read rows: {values}")
        print("✅ Google Sheets connection via Service Account is 100% SUCCESSFUL!")
    except Exception as e:
        print(f"❌ Google Sheets access FAILED: {e}")
        print("\n💡 HINT: Please make sure you have opened your Google Sheet and shared it (Share button) with the service account email:")
        print("👉 sercet-kava@secret-kava-odesa.iam.gserviceaccount.com")
        print("   Give it 'Editor' (Редактор) permissions.")

if __name__ == "__main__":
    asyncio.run(test_sa())
