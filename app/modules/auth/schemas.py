from pydantic import BaseModel, EmailStr, field_validator


class SignUpRequest(BaseModel):
    full_name: str
    artist_name: str
    phone: str
    email: EmailStr
    password: str

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Full name must be at least 2 characters")
        return v

    @field_validator("artist_name")
    @classmethod
    def artist_name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Artist name must be at least 2 characters")
        return v

    @field_validator("phone")
    @classmethod
    def phone_not_blank(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 7:
            raise ValueError("Please enter a valid phone number")
        return v

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v
