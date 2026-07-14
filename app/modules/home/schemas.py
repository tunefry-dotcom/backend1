from __future__ import annotations

from pydantic import BaseModel


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


class HomeContent(BaseModel):
    artists: list[ArtistCard] = []
    yt_testimonials: list[YTTestimonial] = []
    trending_links: list[str] = []
    latest_release_link: str | None = None
    popular_artist_links: list[str] = []
    top_hits_links: list[str] = []
