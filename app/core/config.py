# app/core/config.py
from typing import Literal
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Core
    ENVIRONMENT: Literal["development", "production", "testing"] = "development"
    PROJECT_NAME: str = "Psychological Space Bot"
    
    # Infrastructure
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/psychology_db"
    )
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0"
    )
    
    # Telegram Bot
    TELEGRAM_BOT_TOKEN: SecretStr
    TELEGRAM_WEBHOOK_SECRET: SecretStr = Field(default="change_me_to_a_long_random_string_123456!")
    TELEGRAM_WEBHOOK_URL: str | None = None
    
    # Integrations: Payments
    MONOBANK_API_TOKEN: SecretStr | None = None
    MONOBANK_WEBHOOK_URL: str | None = None
    WAYFORPAY_MERCHANT_ACCOUNT: str | None = None
    WAYFORPAY_MERCHANT_SECRET: SecretStr | None = None
    WAYFORPAY_MERCHANT_DOMAIN: str = "secretcava.com.ua"
    LIQPAY_PUBLIC_KEY: str | None = None
    LIQPAY_PRIVATE_KEY: SecretStr | None = None
    
    # Integrations: Google Workspace
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: SecretStr | None = None
    GOOGLE_REFRESH_TOKEN: SecretStr | None = None
    GOOGLE_CALENDAR_ID: str | None = None
    GOOGLE_SHEET_ID: str | None = None
    GOOGLE_SERVICE_ACCOUNT_FILE: str | None = "service_account.json"
    
    # Security & Admin Panel JWT
    JWT_SECRET_KEY: SecretStr = Field(default="super_secret_jwt_sign_key_do_not_leak_this_in_production!")
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    
    # GDPR: User Encryption Key (AES-256 GCM expects 32-byte key after decoding)
    PII_ENCRYPTION_KEY: SecretStr = Field(default="32_character_long_secret_key_123!")

settings = Settings() # type: ignore
