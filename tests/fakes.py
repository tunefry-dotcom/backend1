"""Lightweight fakes for the supabase-py fluent query builder, used to unit-test
service-layer functions without hitting a real Supabase project.

Every chain method (select/eq/limit/order/range/insert/upsert/update) returns
``self`` so call chains of any shape work; only ``execute()`` produces a
result, taken from whatever was queued for that table.
"""

from __future__ import annotations

from typing import Any


class FakeResult:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeQuery:
    def __init__(self, data: Any = None) -> None:
        self._data = data if data is not None else []

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def maybe_single(self, *a, **k):
        # Real supabase-py unwraps .data to a single dict (or None) after this
        # call, instead of a list — mirror that so callers using
        # `.maybe_single().execute().data["field"]` see the same shape.
        self._data = self._data[0] if self._data else None
        return self

    def insert(self, row, *a, **k):
        self.last_insert = row
        return self

    def upsert(self, row, *a, **k):
        self.last_upsert = row
        return self

    def update(self, row, *a, **k):
        self.last_update = row
        return self

    def execute(self):
        return FakeResult(self._data)


class FakeClient:
    """``table(name)`` returns a pre-registered FakeQuery so tests can control
    exactly what each table returns, and inspect what was written to it."""

    def __init__(self, tables: dict[str, FakeQuery] | None = None) -> None:
        self._tables = tables or {}
        self.auth = FakeAuth()

    def table(self, name: str) -> FakeQuery:
        return self._tables.setdefault(name, FakeQuery())


class FakeGetUserResult:
    def __init__(self, email: str | None) -> None:
        self.user = FakeUser(email) if email is not None else None


class FakeUser:
    def __init__(self, email: str) -> None:
        self.email = email


class FakeAuth:
    def __init__(self) -> None:
        self.admin = FakeAdmin()


class FakeAdmin:
    def __init__(self) -> None:
        self._emails: dict[str, str] = {}

    def register_email(self, user_id: str, email: str) -> None:
        self._emails[user_id] = email

    def get_user_by_id(self, user_id: str) -> FakeGetUserResult:
        return FakeGetUserResult(self._emails.get(user_id))
