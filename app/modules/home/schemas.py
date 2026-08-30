from __future__ import annotations

import re

from pydantic import BaseModel, computed_field

_YT_ID_RE = re.compile(
    r'(?:youtu\.be/|youtube\.com/(?:watch\?.*?v=|shorts/|embed/))([A-Za-z0-9_-]{11})'
)


def _extract_yt_id(s: str) -> str:
    """Return the 11-char video ID from a full YouTube URL or a bare ID."""
    m = _YT_ID_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', s.strip()):
        return s.strip()
    return ""


class ArtistCard(BaseModel):
    name: str = ""
    image_url: str = ""
    genre: str = ""
    city: str = ""
    yt_video_id: str = ""


class YTTestimonial(BaseModel):
    video_id: str = ""
    title: str = ""
    channel: str = ""

    @computed_field
    @property
    def thumbnail_url(self) -> str:
        vid = _extract_yt_id(self.video_id)
        return f"https://img.youtube.com/vi/{vid}/mqdefault.jpg" if vid else ""


class HomeContent(BaseModel):
    artists: list[ArtistCard] = []
    yt_testimonials: list[YTTestimonial] = []
    trending_links: list[str] = []
    latest_release_link: str | None = None
    popular_artist_links: list[str] = []
    top_hits_links: list[str] = []
