"""Cloudflare R2 client — S3-compatible file storage via boto3."""

from __future__ import annotations

import os
import re
from functools import lru_cache

import boto3
from botocore.config import Config

from app.core.config import settings

# Allowed MIME types and extensions (server-side guard; client validates too)
ALLOWED_AUDIO_TYPES: frozenset[str] = frozenset({
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/mpeg",         # MP3
    "audio/flac", "audio/x-flac",
    "audio/octet-stream", # some browsers report this for wav/flac
})
ALLOWED_AUDIO_EXTENSIONS: frozenset[str] = frozenset({".wav", ".mp3", ".flac"})

ALLOWED_COVER_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/jpg"})
ALLOWED_COVER_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})


@lru_cache(maxsize=1)
def _get_r2() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint_url,
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def get_r2_client() -> "boto3.client":
    return _get_r2()


def sanitize_key_part(name: str) -> str:
    """Lowercase, strip special chars, collapse whitespace/underscores → safe path segment."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_-]+", "_", name)
    name = name.strip("_")
    return (name or "unknown")[:80]


def build_key(
    artist_name: str,
    release_name: str,
    file_type: str,
    ext: str,
    track_number: int | None = None,
) -> str:
    """Build the R2 object key.

    Structure:
      {artist}/{release}/cover_art.{ext}
      {artist}/{release}/audio.{ext}            ← single song
      {artist}/{release}/track_NN.{ext}         ← album track
    """
    artist = sanitize_key_part(artist_name)
    release = sanitize_key_part(release_name)
    if file_type == "cover_art":
        filename = f"cover_art{ext}"
    elif file_type == "audio" and track_number is not None:
        filename = f"track_{track_number:02d}{ext}"
    else:
        filename = f"audio{ext}"
    return f"{artist}/{release}/{filename}"


def presign_put(key: str, content_type: str, expires_in: int = 3600) -> str:
    """Return a presigned PUT URL valid for `expires_in` seconds."""
    return _get_r2().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.r2_bucket_name,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
        HttpMethod="PUT",
    )


def presign_get(key: str, expires_in: int = 900) -> str:
    """Return a presigned GET URL valid for `expires_in` seconds (default 15 min)."""
    return _get_r2().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.r2_bucket_name, "Key": key},
        ExpiresIn=expires_in,
    )


def upload_bytes(key: str, content: bytes, content_type: str) -> None:
    """Upload raw bytes directly to R2 (used by the backend submissions handler)."""
    _get_r2().put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=content,
        ContentType=content_type,
    )
