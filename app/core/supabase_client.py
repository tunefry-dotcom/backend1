"""Supabase client factories.

- ``get_supabase()``      -> anon-key client for user-facing auth operations.
- ``get_service_client()`` -> service-role client (server-only, bypasses RLS).
- ``build_pkce_client()``  -> a client configured for the PKCE OAuth flow with a
                              caller-supplied storage so we can persist/restore the
                              code-verifier across the redirect (Phase 3).
"""

from __future__ import annotations

from typing import Optional

from supabase import Client, create_client
from supabase.lib.client_options import ClientOptions

from app.core.config import settings


class DictStorage:
    """In-memory key/value store implementing the supabase storage interface.

    supabase-py writes the PKCE code-verifier here during ``sign_in_with_oauth``.
    We serialize this dict into a short-lived cookie and rebuild it on callback so
    ``exchange_code_for_session`` can complete a stateless server-side PKCE flow.
    """

    def __init__(self, data: Optional[dict[str, str]] = None) -> None:
        self._data: dict[str, str] = dict(data or {})

    def get_item(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def set_item(self, key: str, value: str) -> None:
        self._data[key] = value

    def remove_item(self, key: str) -> None:
        self._data.pop(key, None)

    def dump(self) -> dict[str, str]:
        return dict(self._data)


def get_supabase() -> Client:
    """Anon client for password/OAuth/recovery auth calls."""
    return create_client(settings.supabase_url, settings.supabase_anon_key)


def get_service_client() -> Client:
    """Service-role client. Never expose this key or client to the browser."""
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def build_pkce_client(storage: DictStorage) -> Client:
    """Anon client wired for the PKCE flow using the provided storage."""
    options = ClientOptions(
        flow_type="pkce",
        storage=storage,
        auto_refresh_token=False,
        persist_session=True,
    )
    return create_client(settings.supabase_url, settings.supabase_anon_key, options)
