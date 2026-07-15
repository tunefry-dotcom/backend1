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
    # HTTP timeout (seconds) for the Supabase auth (GoTrue) client. The library
    # defaults to httpx's 5s, which is too tight for a slow project — signup sends
    # the confirmation email synchronously and routinely exceeds 5s → ReadTimeout.
    supabase_http_timeout: float = Field(
        default=30.0, validation_alias="SUPABASE_HTTP_TIMEOUT"
    )

    # Extra CORS origins — comma-separated (e.g. "http://localhost:5173,http://localhost:3000")
    extra_cors_origin: str = Field(default="", validation_alias="EXTRA_CORS_ORIGIN")

    @property
    def extra_cors_origins(self) -> list[str]:
        """Parse comma-separated EXTRA_CORS_ORIGIN into a list, stripping blanks."""
        return [o.strip() for o in self.extra_cors_origin.split(",") if o.strip()]

    # Google OAuth
    google_oauth_enabled: bool = False
    oauth_callback_base_url: str = "http://localhost:8000"

    # Dev-only: enables /auth/dev/create-user (pre-confirmed users, bypasses email).
    # MUST stay false in production.
    dev_auth_enabled: bool = Field(default=False, validation_alias="DEV_AUTH_ENABLED")

    # Admin panel secret — sent as X-Admin-Secret header. Set a strong value in prod.
    admin_secret: str = Field(default="", validation_alias="ADMIN_SECRET")

    # Cloudflare R2 (file storage — S3-compatible). Set all four or storage is disabled.
    r2_account_id: str = Field(default="", validation_alias="R2_ACCOUNT_ID")
    r2_access_key_id: str = Field(default="", validation_alias="R2_ACCESS_KEY_ID")
    r2_secret_access_key: str = Field(default="", validation_alias="R2_SECRET_ACCESS_KEY")
    r2_bucket_name: str = Field(default="", validation_alias="R2_BUCKET_NAME")

    @property
    def r2_enabled(self) -> bool:
        return bool(
            self.r2_account_id
            and self.r2_access_key_id
            and self.r2_secret_access_key
            and self.r2_bucket_name
        )

    @property
    def r2_endpoint_url(self) -> str:
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    # Resend (transactional email). We send confirmation emails ourselves via the
    # Resend HTTP API to bypass Supabase's built-in SMTP sender, which hangs on this
    # project (sign_up blocks 30s+ on the SMTP connection). Set RESEND_API_KEY to
    # enable; the sender domain must be verified in Resend.
    resend_api_key: str = Field(default="", validation_alias="RESEND_API_KEY")
    resend_from_email: str = Field(
        default="noreply@tunefry.com", validation_alias="RESEND_FROM_EMAIL"
    )
    resend_from_name: str = Field(default="Tunefry", validation_alias="RESEND_FROM_NAME")

    @property
    def resend_enabled(self) -> bool:
        return bool(self.resend_api_key)

    @property
    def resend_from(self) -> str:
        """RFC 5322 From header, e.g. ``Tunefry <noreply@tunefry.com>``."""
        return f"{self.resend_from_name} <{self.resend_from_email}>"

    @property
    def confirm_email_url(self) -> str:
        """Backend endpoint the confirmation link points at."""
        return f"{self.oauth_callback_base_url}/auth/confirm"

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
