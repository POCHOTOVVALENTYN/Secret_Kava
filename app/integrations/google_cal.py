# app/integrations/google_cal.py
from datetime import datetime, timedelta
import os
import asyncio
from typing import Any
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from structlog import get_logger

logger = get_logger()

class GoogleCalendarClient:
    """Manages active slot validations and events synchronization directly in therapist calendars."""
    
    def __init__(self, client_id: str = None, client_secret: str = None, refresh_token: str = None, service_account_file: str = None):
        self.service_account_file = service_account_file
        scopes = ["https://www.googleapis.com/auth/calendar"]
        
        # 1. Try to load Service Account credentials if file path is provided and exists
        if service_account_file and os.path.exists(service_account_file):
            try:
                self.creds = service_account.Credentials.from_service_account_file(
                    service_account_file,
                    scopes=scopes
                )
                self.is_service_account = True
                logger.info("google_calendar_client_initialized_via_service_account", file=service_account_file)
            except Exception as e:
                logger.error("google_calendar_service_account_init_failed_falling_back", error=str(e))
                self.is_service_account = False
        else:
            self.is_service_account = False

        # 2. Fallback to standard OAuth2 Credentials
        if not self.is_service_account:
            self.creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret
            )
            self._refresh_credentials()
            logger.info("google_calendar_client_initialized_via_oauth2")

        # Initialize Google Calendar v3 service resource
        self.service = build("calendar", "v3", credentials=self.creds)

    def _refresh_credentials(self) -> None:
        """Forces OAuth Access Token regeneration if expired (skipped for Service Account)."""
        if self.is_service_account:
            return
            
        try:
            if not self.creds.valid:
                self.creds.refresh(Request())
        except Exception as e:
            logger.critical("google_credentials_refresh_failed", error=str(e))
            raise RuntimeError("OAuth2 Credentials validation failed") from e

    async def get_busy_intervals(self, calendar_id: str, start_time: datetime, end_time: datetime) -> list[dict[str, datetime]]:
        """Queries Google Calendar FreeBusy API to determine busy intervals."""
        self._refresh_credentials()
        
        body = {
            "timeMin": start_time.isoformat(),
            "timeMax": end_time.isoformat(),
            "timeZone": "Europe/Kyiv",
            "items": [{"id": calendar_id}]
        }
        
        def _query():
            return self.service.freebusy().query(body=body).execute()
            
        response = await asyncio.to_thread(_query)
        busy_data = response.get("calendars", {}).get(calendar_id, {}).get("busy", [])
        
        parsed_intervals: list[dict[str, datetime]] = []
        for interval in busy_data:
            parsed_intervals.append({
                "start": datetime.fromisoformat(interval["start"].replace("Z", "+00:00")),
                "end": datetime.fromisoformat(interval["end"].replace("Z", "+00:00"))
            })
        return parsed_intervals

    async def create_booking_event(self, calendar_id: str, summary: str, start_time: datetime, duration_minutes: int) -> str:
        """Publishes a confirmed reservation directly inside Google Calendar, returning EventID."""
        self._refresh_credentials()
        
        end_time = start_time + timedelta(minutes=duration_minutes)
        event_body = {
            "summary": summary,
            "description": "Автоматичне бронювання через Telegram Bot психологічного простору",
            "start": {
                "dateTime": start_time.isoformat(),
                "timeZone": "Europe/Kyiv",
            },
            "end": {
                "dateTime": end_time.isoformat(),
                "timeZone": "Europe/Kyiv",
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 120}, # 2h reminder
                    {"method": "email", "minutes": 1440}, # 24h reminder
                ],
            },
        }
        
        def _insert():
            return self.service.events().insert(calendarId=calendar_id, body=event_body).execute()
            
        event = await asyncio.to_thread(_insert)
        return str(event.get("id"))

    async def delete_booking_event(self, calendar_id: str, event_id: str) -> None:
        """Cancels/removes a booking event from Google Calendar."""
        self._refresh_credentials()
        try:
            def _delete():
                self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            await asyncio.to_thread(_delete)
            logger.info("google_calendar_event_deleted", event_id=event_id)
        except Exception as e:
            logger.error("google_calendar_event_deletion_failed", event_id=event_id, error=str(e))
