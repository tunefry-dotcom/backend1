"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General
    env: Literal["development", "production", "test"] = "development"
    session_secret: str = "change-me-in-prod"
    frontend_url: str = "http://localhost:5173"

    # Cookies
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    access_cookie_name: str = "sb-access-token"
    refresh_cookie_name: str = "sb-refresh-token"
    pkce_cookie_name: str = "sb-pkce"

    # Supabase
    supabase_url: str = Field(default="", validation_alias="SUPABASE_URL")
    supabase_anon_key: str = Field(default="", validation_alias="SUPABASE_ANON_KEY")
    supabase_service_role_key: str = Field(
        default="", validation_alias="SUPABASE_SERVICE_ROLE_KEY"
    )
    # Only set if the project has NOT migrated to asymmetric JWKS signing keys.
    supabase_jwt_secret: str = Field(
        default="", validation_alias="SUPABASE_JWT_SECRET"
    )

    # Extra CORS origin (e.g. localhost dev while FRONTEND_URL is production)
    extra_cors_origin: str = Field(default="", validation_alias="EXTRA_CORS_ORIGIN")

    # Google OAuth
    google_oauth_enabled: bool = False
    oauth_callback_base_url: str = "http://localhost:8000"

    # Dev-only: enables /auth/dev/create-user (pre-confirmed users, bypasses email).
    # MUST stay false in production.
    dev_auth_enabled: bool = Field(default=False, validation_alias="DEV_AUTH_ENABLED")

    # Admin panel secret — sent as X-Admin-Secret header. Set a strong value in prod.
    admin_secret: str = Field(default="", validation_alias="ADMIN_SECRET")

    # Razorpay (plan payments). Keys are secrets — set via env only, never commit.
    razorpay_key_id: str = Field(default="", validation_alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: str = Field(default="", validation_alias="RAZORPAY_KEY_SECRET")
    # Divides the charged amount for testing: 100 → charge 1/100th (₹14.39 for a
    # ₹1439 plan); set to 1 in production to charge the real price.
    payment_amount_divisor: int = Field(
        default=100, validation_alias="PAYMENT_AMOUNT_DIVISOR"
    )

    @property
    def razorpay_enabled(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def jwks_url(self) -> str:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url}/auth/v1"

    @property
    def google_callback_url(self) -> str:
        return f"{self.oauth_callback_base_url}/auth/google/callback"

    @property
    def reset_password_url(self) -> str:
        return f"{self.oauth_callback_base_url}/auth/reset-password"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
