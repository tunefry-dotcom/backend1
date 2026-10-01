from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["artist_journey", "song_release", "informative", "success_story"]
AuthorType = Literal["artist", "tunefry"]
PostStatus = Literal["pending", "approved", "declined"]
CreditPack = Literal["single", "bundle_20"]

CATEGORY_LABELS: dict[str, str] = {
    "artist_journey": "Artist Journey",
    "song_release": "Song Release",
    "informative": "Informative",
    "success_story": "Success Story",
}


class CategoryOut(BaseModel):
    id: str
    label: str


# --- Publish credits -------------------------------------------------------

class CreditStatus(BaseModel):
    free_article_used: bool
    credits_remaining: int
    can_publish: bool


class CreditOrderRequest(BaseModel):
    pack: CreditPack


class CreditOrderResponse(BaseModel):
    order_id: str
    amount: int
    currency: str
    key_id: str
    pack: CreditPack
    credits: int


class CreditVerifyRequest(BaseModel):
    pack: CreditPack
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class CreditVerifyResponse(BaseModel):
    credited: bool
    free_article_used: Optional[bool] = None
    credits_remaining: Optional[int] = None
    support_note: Optional[str] = None


# --- Artist submission -------------------------------------------------------

class SubmitArticleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    category: Category
    cover_image_key: str = Field(min_length=1)


# --- Public-facing post shapes (never expose author_email/original text) ----

class PostSummary(BaseModel):
    id: str
    slug: str
    title: str
    category: Category
    author_type: AuthorType
    author_name: str
    cover_image_key: Optional[str] = None
    excerpt: Optional[str] = None
    is_featured: bool
    is_popular: bool
    published_at: Optional[str] = None


class PostDetail(PostSummary):
    body: str
    cover_image_keys: list[str] = Field(default_factory=list)


class PaginatedPosts(BaseModel):
    posts: list[PostSummary]
    total: int
    page: int
    per_page: int
    total_pages: int


# --- Admin ---------------------------------------------------------------

class RewriteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class RewriteResponse(BaseModel):
    title: str
    body: str


class AdminReviewRequest(BaseModel):
    status: Literal["approved", "declined"]
    admin_note: str = Field(default="", max_length=500)
    final_title: Optional[str] = Field(default=None, max_length=200)
    final_body: Optional[str] = Field(default=None, max_length=20000)


class AdminTunefryPublishRequest(BaseModel):
    final_title: str = Field(min_length=1, max_length=200)
    final_body: str = Field(min_length=1, max_length=20000)
    category: Category
    cover_image_keys: list[str] = Field(min_length=1, max_length=2)


class AdminFlagsRequest(BaseModel):
    is_featured: Optional[bool] = None
    is_popular: Optional[bool] = None


class AdminPost(BaseModel):
    id: str
    slug: str
    author_type: AuthorType
    author_email: Optional[str] = None
    author_name: str
    category: Category
    status: PostStatus
    original_title: str
    original_body: str
    final_title: Optional[str] = None
    final_body: Optional[str] = None
    cover_image_keys: list[str] = Field(default_factory=list)
    admin_note: str
    is_featured: bool
    is_popular: bool
    reviewed_at: Optional[str] = None
    published_at: Optional[str] = None
    created_at: Optional[str] = None
