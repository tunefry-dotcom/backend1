"""Canonical plan catalogue + feature entitlement matrix.

This module is pure data + pure functions — no I/O, no framework imports — so it
can be imported anywhere (dependencies, router, tests) without side effects and
mirrored 1:1 by the frontend.

Terminology
-----------
- **Plan**: a subscription tier an artist is on (``free`` by default).
- **Feature**: a capability gated by plan (``release_album``, ``playlist_pitching`` …).
- **Entitlements**: the resolved ``{feature: bool}`` map for a given plan, plus
  numeric limits (max releases, max artists) and the royalty rate.

The matrix is derived directly from the public pricing page. Keep it in sync with
``frontend/src/lib/plans.js`` — the frontend mirrors these keys for route gating.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class Plan(str, Enum):
    """Subscription tiers, ordered lowest → highest privilege."""

    FREE = "free"
    SINGLE_SONG = "single-song"
    STARTER = "starter"
    SINGLE_ARTIST = "single-artist"
    DOUBLE_ARTIST = "double-artist"
    LABEL = "label"


class Feature(str, Enum):
    """Gated capabilities. Values map to frontend route-gate keys."""

    RELEASE_SINGLE = "release_single"        # /upload/new-song
    RELEASE_ALBUM = "release_album"          # /upload/new-album
    TRANSFER_SINGLE = "transfer_single"      # /upload/transfer-song
    TRANSFER_ALBUM = "transfer_album"        # /upload/transfer-album
    PLAYLIST_PITCHING = "playlist_pitching"  # /pitch-song
    INSTAGRAM_LINKING = "instagram_linking"  # /instalink
    CONTENT_ID = "content_id"
    CUSTOM_LABEL = "custom_label"


DEFAULT_PLAN = Plan.FREE

# Rank used for "is this an upgrade?" comparisons and to compute the cheapest
# plan that unlocks a given feature. Higher = more privileged.
PLAN_RANK: dict[Plan, int] = {
    Plan.FREE: 0,
    Plan.SINGLE_SONG: 1,
    Plan.STARTER: 2,
    Plan.SINGLE_ARTIST: 3,
    Plan.DOUBLE_ARTIST: 4,
    Plan.LABEL: 5,
}


class PlanSpec:
    """Immutable description of one plan: display metadata + entitlements."""

    __slots__ = (
        "plan",
        "name",
        "price_inr",
        "royalty_pct",
        "max_releases",
        "max_artists",
        "features",
    )

    def __init__(
        self,
        plan: Plan,
        name: str,
        price_inr: int,
        royalty_pct: int,
        max_releases: Optional[int],
        max_artists: int,
        features: frozenset[Feature],
    ) -> None:
        self.plan = plan
        self.name = name
        self.price_inr = price_inr
        self.royalty_pct = royalty_pct
        self.max_releases = max_releases  # None => unlimited
        self.max_artists = max_artists
        self.features = features


# --- Feature bundles (declared once, composed below to avoid drift) ----------

# Everyone who has any paid single-release capability can release singles;
# free tier can too (capped at 10). So release_single is universal.
_ALL_PLANS_FEATURE = {Feature.RELEASE_SINGLE}

# Premium bundle unlocked from Single Artist upward.
_PREMIUM = {
    Feature.RELEASE_ALBUM,
    Feature.TRANSFER_SINGLE,
    Feature.TRANSFER_ALBUM,
    Feature.PLAYLIST_PITCHING,
    Feature.CONTENT_ID,
    Feature.INSTAGRAM_LINKING,
}


PLAN_SPECS: dict[Plan, PlanSpec] = {
    Plan.FREE: PlanSpec(
        plan=Plan.FREE,
        name="Free",
        price_inr=0,
        royalty_pct=75,
        max_releases=10,
        max_artists=1,
        features=frozenset(_ALL_PLANS_FEATURE),
    ),
    Plan.SINGLE_SONG: PlanSpec(
        plan=Plan.SINGLE_SONG,
        name="Single Song",
        price_inr=299,
        royalty_pct=85,
        max_releases=1,
        max_artists=1,
        features=frozenset(_ALL_PLANS_FEATURE),
    ),
    Plan.STARTER: PlanSpec(
        plan=Plan.STARTER,
        name="Starter",
        price_inr=999,
        royalty_pct=90,
        max_releases=None,
        max_artists=1,
        # Starter adds Content ID + Instagram, but still singles-only (no albums,
        # transfer or pitching).
        features=frozenset(
            _ALL_PLANS_FEATURE | {Feature.CONTENT_ID, Feature.INSTAGRAM_LINKING}
        ),
    ),
    Plan.SINGLE_ARTIST: PlanSpec(
        plan=Plan.SINGLE_ARTIST,
        name="Single Artist",
        price_inr=1599,
        royalty_pct=100,
        max_releases=None,
        max_artists=1,
        features=frozenset(_ALL_PLANS_FEATURE | _PREMIUM),
    ),
    Plan.DOUBLE_ARTIST: PlanSpec(
        plan=Plan.DOUBLE_ARTIST,
        name="Double Artist",
        price_inr=2999,
        royalty_pct=100,
        max_releases=None,
        max_artists=2,
        features=frozenset(_ALL_PLANS_FEATURE | _PREMIUM | {Feature.CUSTOM_LABEL}),
    ),
    Plan.LABEL: PlanSpec(
        plan=Plan.LABEL,
        name="Label Plan",
        price_inr=6999,
        royalty_pct=100,
        max_releases=None,
        max_artists=5,
        features=frozenset(_ALL_PLANS_FEATURE | _PREMIUM | {Feature.CUSTOM_LABEL}),
    ),
}


def coerce_plan(value: object) -> Plan:
    """Best-effort parse of a stored/metadata value into a Plan.

    Unknown, missing or malformed values fall back to the default (free) rather
    than raising — a corrupt metadata field must never lock a user out or 500 a
    request; it degrades to the least-privileged tier.
    """
    if isinstance(value, Plan):
        return value
    if isinstance(value, str):
        try:
            return Plan(value.strip().lower())
        except ValueError:
            return DEFAULT_PLAN
    return DEFAULT_PLAN


def plan_from_claims(claims: dict[str, Any]) -> Plan:
    """Extract the plan from verified JWT claims (pure, no I/O).

    The plan lives in ``app_metadata.plan``. A missing or malformed value falls
    back to the default (free) tier via ``coerce_plan``.
    """
    app_metadata = claims.get("app_metadata") or {}
    return coerce_plan(app_metadata.get("plan"))


def get_spec(plan: Plan) -> PlanSpec:
    return PLAN_SPECS[plan]


def has_feature(plan: Plan, feature: Feature) -> bool:
    return feature in PLAN_SPECS[plan].features


def entitlements(plan: Plan) -> dict[Feature, bool]:
    """Resolve the full ``{feature: bool}`` map for a plan (every feature keyed)."""
    spec = PLAN_SPECS[plan]
    return {feature: (feature in spec.features) for feature in Feature}


def min_plan_for(feature: Feature) -> Optional[Plan]:
    """The cheapest plan (by rank) that unlocks ``feature``, or None if none do."""
    candidates = [
        p for p in Plan if feature in PLAN_SPECS[p].features
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: PLAN_RANK[p])


def is_upgrade(current: Plan, target: Plan) -> bool:
    return PLAN_RANK[target] > PLAN_RANK[current]
