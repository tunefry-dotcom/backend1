"""Shared platform normalization for royalty ingestion scripts.

Collapses the messy case/spacing variants coming from either the legacy
SQL Server dump (`dbo.MusicStreams.Platform`) OR the DSP-consolidated Excel
report (`source_platform` on Combined_All sheet) into:

  * a canonical ``platform`` string (stored on ``song_stats.platform``)
  * a ``platform_group`` (stored on ``song_stats.platform_group``, used by
    the frontend's Stats > Platform chart to club low-stream platforms
    into ``Other``).

The frontend's Overview UGC toggle subtracts streams whose group is
``Facebook`` or ``TikTok`` — those two strings must appear here verbatim.
"""

from __future__ import annotations

import re


_PLATFORM_MAP: dict[str, tuple[str, str]] = {
    # ---- Legacy dump values (ingest_streams.py) --------------------------
    "spotify": ("Spotify", "Spotify"),
    "applemusic": ("Apple Music", "Apple Music"),
    "apple music": ("Apple Music", "Apple Music"),
    "yt-pdl": ("YouTube", "YouTube"),
    "youtube": ("YouTube", "YouTube"),
    "youtube music": ("YouTube", "YouTube"),
    "youtube ad supported": ("YouTube", "YouTube"),
    "youtube art track": ("YouTube", "YouTube"),
    "youtube ugc": ("YouTube", "YouTube"),
    "facebook": ("Facebook", "Facebook"),
    "facebook audio library": ("Facebook", "Facebook"),
    "facebook fingerprinting": ("Facebook", "Facebook"),
    "instagram": ("Instagram", "Facebook"),
    "amazon": ("Amazon", "Amazon"),
    "amazon prime": ("Amazon", "Amazon"),
    "jiosaavn": ("JioSaavn", "JioSaavn"),
    "gaana": ("Gaana", "Gaana"),
    "tiktok": ("TikTok", "TikTok"),
    "tiktok inc.": ("TikTok", "TikTok"),
    "snap": ("Snap", "Other"),
    "soundcloud": ("SoundCloud", "Other"),
    # ---- New DSP-consolidated Excel report values ------------------------
    # source_platform column on Combined_All uses these exact strings.
    "youtube (pdl)": ("YouTube", "YouTube"),
    "youtube (mango)": ("YouTube", "YouTube"),
    "facebook / meta": ("Facebook", "Facebook"),
    "facebook/meta": ("Facebook", "Facebook"),
    "meta": ("Facebook", "Facebook"),
}


def normalize_platform(raw: str | None) -> tuple[str, str]:
    """(canonical_name, group) for a raw platform string. Unknown -> Other."""
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    if key in _PLATFORM_MAP:
        return _PLATFORM_MAP[key]
    pretty = re.sub(r"\s+", " ", (raw or "").strip()) or "Unknown"
    return (pretty.title(), "Other")
