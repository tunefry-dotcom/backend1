"""Billing / subscription-plan bounded context.

Defines the canonical plan catalogue, the per-plan feature entitlement matrix,
and the machinery to read/write a user's plan and guard feature-gated routes.

The plan is the single source of truth for what services a signed-in artist can
access (single/album releases, catalogue transfer, playlist pitching, Instagram
linking, etc.). Everything downstream — the frontend route gates and any future
domain endpoints — derives access from the entitlements computed here.
"""
