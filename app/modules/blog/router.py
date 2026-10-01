from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.core.r2_client import presign_get
from app.modules.auth.dependencies import CurrentUser, get_current_user
from app.modules.blog import credits as credits_service
from app.modules.blog import payment as blog_payment
from app.modules.blog import service as blog_service
from app.modules.blog.payment import PaymentError
from app.modules.blog.schemas import (
    CATEGORY_LABELS,
    CategoryOut,
    CreditOrderRequest,
    CreditOrderResponse,
    CreditStatus,
    CreditVerifyRequest,
    CreditVerifyResponse,
    PaginatedPosts,
    PostDetail,
    PostSummary,
    SubmitArticleRequest,
)

router = APIRouter(prefix="/blog", tags=["blog"])


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories() -> list[CategoryOut]:
    return [CategoryOut(id=k, label=v) for k, v in CATEGORY_LABELS.items()]


@router.get("/credits/me", response_model=CreditStatus)
async def my_credit_status(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CreditStatus:
    return CreditStatus(**credits_service.get_credit_status((current_user.email or "").lower()))


@router.post("/credits/order", response_model=CreditOrderResponse)
async def create_credit_order(
    body: CreditOrderRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CreditOrderResponse:
    try:
        order = blog_payment.create_order(current_user.id, body.pack)
    except PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return CreditOrderResponse(**order)


@router.post("/credits/verify", response_model=CreditVerifyResponse)
async def verify_credit_payment(
    body: CreditVerifyRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CreditVerifyResponse:
    try:
        blog_payment.verify_payment(
            user_id=current_user.id,
            pack=body.pack,
            razorpay_order_id=body.razorpay_order_id,
            razorpay_payment_id=body.razorpay_payment_id,
            razorpay_signature=body.razorpay_signature,
        )
    except PaymentError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    email = (current_user.email or "").lower()
    amount_inr = Decimal(blog_payment.amount_paise(body.pack)) / Decimal(100)
    credits = blog_payment.pack_credits(body.pack)
    result = credits_service.record_purchase_and_grant(
        email=email,
        pack=body.pack,
        razorpay_order_id=body.razorpay_order_id,
        razorpay_payment_id=body.razorpay_payment_id,
        amount_inr=amount_inr,
        credits=credits,
    )
    return CreditVerifyResponse(**result)


@router.post("/images")
async def upload_blog_image(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    file: UploadFile = File(...),
) -> dict[str, str]:
    key = await blog_service.save_blog_image(file)
    return {"key": key}


@router.get("/assets/{key:path}", include_in_schema=False)
async def proxy_blog_asset(key: str) -> RedirectResponse:
    if not key.startswith("blog/"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    if not settings.r2_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Storage not configured.")
    url = presign_get(key, expires_in=900)
    return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.post("/submit", response_model=PostDetail)
async def submit_article(
    body: SubmitArticleRequest,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> PostDetail:
    email = (current_user.email or "").lower()
    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Account has no email on file.")

    credits_service.consume_publish_credit(email)

    author_name = current_user.artist_name or current_user.full_name or email
    try:
        row = blog_service.create_pending_post(
            author_email=email,
            author_name=author_name,
            title=body.title.strip(),
            body=body.body.strip(),
            category=body.category,
            cover_image_key=body.cover_image_key,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not submit article: {exc}",
        ) from exc
    return PostDetail(**row)


@router.get("/mine", response_model=list[PostSummary])
async def my_posts(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    status_filter: Optional[str] = Query(default=None, alias="status"),
) -> list[dict]:
    email = (current_user.email or "").lower()
    return blog_service.list_my_posts(email, status_filter)


@router.get("/posts", response_model=PaginatedPosts)
async def list_posts(
    category: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=12, ge=1, le=15),
) -> PaginatedPosts:
    return PaginatedPosts(**blog_service.list_public_posts(category=category, page=page, per_page=per_page))


@router.get("/posts/{slug}", response_model=PostDetail)
async def get_post(slug: str) -> PostDetail:
    row = blog_service.get_post_by_slug(slug)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
    return PostDetail(**row)


@router.get("/posts/{slug}/related", response_model=list[PostSummary])
async def get_related_posts(slug: str, limit: int = Query(default=6, ge=1, le=12)) -> list[dict]:
    return blog_service.list_related_posts(slug, limit=limit)
