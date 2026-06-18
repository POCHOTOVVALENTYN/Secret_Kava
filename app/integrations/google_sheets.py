# app/integrations/google_sheets.py
from typing import Any
import os
import json
import asyncio
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from structlog import get_logger

logger = get_logger()

class GoogleSheetsClient:
    """Synchronizes booking orders, reviews, and client leads directly onto a central Google Sheets spreadsheet."""

    def __init__(self, spreadsheet_id: str, client_id: str = None, client_secret: str = None, refresh_token: str = None, service_account_file: str = None):
        self.spreadsheet_id = spreadsheet_id
        self.service_account_file = service_account_file
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        
        # 1. Try to load Service Account credentials if file path is provided and exists
        if service_account_file and os.path.exists(service_account_file):
            try:
                self.creds = service_account.Credentials.from_service_account_file(
                    service_account_file,
                    scopes=scopes
                )
                self.is_service_account = True
                logger.info("google_sheets_client_initialized_via_service_account", file=service_account_file)
            except Exception as e:
                logger.error("google_sheets_service_account_init_failed_falling_back", error=str(e))
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
            logger.info("google_sheets_client_initialized_via_oauth2")

        # Initialize Google Sheets v4 resource service
        self.service = build("sheets", "v4", credentials=self.creds)

    def _refresh_credentials(self) -> None:
        """Forces OAuth Access Token regeneration if expired (skipped for Service Account)."""
        if self.is_service_account:
            return
            
        try:
            if not self.creds.valid:
                self.creds.refresh(Request())
        except Exception as e:
            logger.critical("google_sheets_credentials_refresh_failed", error=str(e))
            raise RuntimeError("OAuth2 Credentials validation failed") from e

    async def append_row(self, sheet_name: str, row_values: list[Any]) -> None:
        """Appends a new list of columns at the end of the specified sheet tab range."""
        self._refresh_credentials()
        try:
            range_name = f"{sheet_name}!A:A"
            body = {
                "values": [row_values]
            }
            
            def _append():
                return self.service.spreadsheets().values().append(
                    spreadsheetId=self.spreadsheet_id,
                    range=range_name,
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body=body
                ).execute()
                
            await asyncio.to_thread(_append)
            
            logger.info("google_sheets_row_appended", sheet=sheet_name)
        except Exception as e:
            logger.error("google_sheets_append_row_failed", sheet=sheet_name, error=str(e))
            # Safe fallbacks: do not block main transaction thread if sheets log fails
            pass

    async def update_cell(self, sheet_name: str, row: int, col_letter: str, value: Any) -> None:
        """Updates a single cell by A1 notation (e.g. sheet, row=3, col_letter='E' → E3)."""
        self._refresh_credentials()
        cell_range = f"{sheet_name}!{col_letter}{row}"
        try:
            def _update():
                return self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=cell_range,
                    valueInputOption="RAW",
                    body={"values": [[value]]}
                ).execute()
            await asyncio.to_thread(_update)
            logger.info("google_sheets_cell_updated", cell=cell_range, value=value)
        except Exception as e:
            logger.error("google_sheets_update_cell_failed", cell=cell_range, error=str(e))
            raise

    async def read_sheet(self, sheet_name: str, range_name: str = "A:Z") -> list[list[Any]]:
        """Reads rows from the specified sheet tab range."""
        self._refresh_credentials()
        try:
            full_range = f"{sheet_name}!{range_name}"
            
            def _get():
                return self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=full_range
                ).execute()
                
            result = await asyncio.to_thread(_get)
            values = result.get('values', [])
            return values
        except Exception as e:
            logger.error("google_sheets_read_failed", sheet=sheet_name, error=str(e))
            return []
