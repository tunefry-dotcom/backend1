"""Tests for the admin Excel bulk-import feature (POST /admin/song-stats/import)
and its centrally-stored FX rate (GET/PUT /admin/fx-rate).

_parse_import_rows is a pure function (no Supabase/FastAPI/openpyxl coupling)
tested directly with plain tuples. The full endpoint is tested with
tests/fakes.py's FakeClient, patched into both app.modules.admin.router and
app.modules.earnings.service — recompute_balance() makes its own
get_service_client() call, so both need patching to share one fake client.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import openpyxl

from app.modules.admin import router as admin_router
from app.modules.earnings import service as earnings_service
from tests.fakes import FakeClient, FakeQuery


def _make_xlsx(header: tuple, rows: list[tuple]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


class _FakeUploadFile:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


class ParseImportRowsTests(unittest.TestCase):
    HEADER = ("Country", "ArtistName", "Song", "Streams", "Revenue", "Month", "Year", "Platform")

    def test_aggregates_across_country(self):
        rows = [
            ("India", "Lalit Sahu", "Tere Bina", 100, "1.50", "May", 2026, "Spotify"),
            ("USA", "Lalit Sahu", "Tere Bina", 50, "0.75", "May", 2026, "Spotify"),
        ]
        parsed = admin_router._parse_import_rows(self.HEADER, rows)
        groups = parsed["groups"]
        self.assertEqual(len(groups), 1)
        g = next(iter(groups.values()))
        self.assertEqual(g["streams"], 150)
        self.assertEqual(g["revenue_usd"], Decimal("2.25"))

    def test_distinct_platform_and_song_stay_separate_groups(self):
        rows = [
            ("India", "Lalit Sahu", "Tere Bina", 100, "1.00", "May", 2026, "Spotify"),
            ("India", "Lalit Sahu", "Tere Bina", 10, "0.10", "May", 2026, "YouTube"),
            ("India", "Lalit Sahu", "BAAP BOL", 20, "0.20", "May", 2026, "Spotify"),
        ]
        parsed = admin_router._parse_import_rows(self.HEADER, rows)
        self.assertEqual(len(parsed["groups"]), 3)

    def test_blank_rows_are_skipped(self):
        rows = [
            (None, None, None, None, None, None, None, None),
            ("India", "Lalit Sahu", "Tere Bina", 100, "1.00", "May", 2026, "Spotify"),
        ]
        parsed = admin_router._parse_import_rows(self.HEADER, rows)
        self.assertEqual(len(parsed["groups"]), 1)

    def test_trailing_subtotal_row_is_skipped(self):
        rows = [
            ("India", "Lalit Sahu", "Tere Bina", 100, "1.00", "May", 2026, "Spotify"),
            (None, None, None, 5714, "2.2492387125", None, None, None),
        ]
        parsed = admin_router._parse_import_rows(self.HEADER, rows)
        self.assertEqual(len(parsed["groups"]), 1)

    def test_platform_variants_normalizing_to_same_canonical_merge(self):
        # A multi-month DSP report can list the same platform under several
        # raw sub-labels (e.g. "YouTube" and "YouTube (PDL)") that both
        # normalize to canonical "YouTube". Grouping by the raw string would
        # leave these as two groups that later collide on the same
        # (song_title, platform, month, year) upsert key and crash Postgres
        # with "ON CONFLICT DO UPDATE command cannot affect row a second
        # time" — they must merge into one group instead.
        rows = [
            ("India", "Fearless", "Some Song", 100, "1.00", "March", 2026, "YouTube"),
            ("India", "Fearless", "Some Song", 50, "0.50", "March", 2026, "YouTube (PDL)"),
        ]
        parsed = admin_router._parse_import_rows(self.HEADER, rows)
        self.assertEqual(len(parsed["groups"]), 1)
        g = next(iter(parsed["groups"].values()))
        self.assertEqual(g["platform"], "YouTube")
        self.assertEqual(g["streams"], 150)
        self.assertEqual(g["revenue_usd"], Decimal("1.50"))

    def test_captures_artist_name_when_column_present(self):
        rows = [("India", "Lalit Sahu", "Tere Bina", 100, "1.00", "May", 2026, "Spotify")]
        parsed = admin_router._parse_import_rows(self.HEADER, rows)
        self.assertEqual(parsed["first_file_artist_name"], "Lalit Sahu")
        self.assertIn("lalit sahu", parsed["file_artist_names_norm"])

    def test_artist_name_column_optional(self):
        header = ("Song", "Streams", "Revenue", "Month", "Year", "Platform")
        rows = [("Tere Bina", 100, "1.00", "May", 2026, "Spotify")]
        parsed = admin_router._parse_import_rows(header, rows)
        self.assertEqual(parsed["first_file_artist_name"], "")
        self.assertEqual(parsed["file_artist_names_norm"], set())

    def test_missing_required_headers_raises(self):
        with self.assertRaises(admin_router.ImportRowError) as ctx:
            admin_router._parse_import_rows(("Song", "Streams"), [("x", 1)])
        self.assertIn("revenue", str(ctx.exception))
        self.assertIn("month", str(ctx.exception))

    def test_no_header_row_raises(self):
        with self.assertRaises(admin_router.ImportRowError):
            admin_router._parse_import_rows((), [])

    def test_missing_song_title_raises(self):
        rows = [("India", "Lalit Sahu", "", 100, "1.00", "May", 2026, "Spotify")]
        with self.assertRaises(admin_router.ImportRowError) as ctx:
            admin_router._parse_import_rows(self.HEADER, rows)
        self.assertIn("Song", str(ctx.exception))

    def test_invalid_month_raises(self):
        rows = [("India", "Lalit Sahu", "Tere Bina", 100, "1.00", "Maybe", 2026, "Spotify")]
        with self.assertRaises(admin_router.ImportRowError) as ctx:
            admin_router._parse_import_rows(self.HEADER, rows)
        self.assertIn("Month", str(ctx.exception))

    def test_non_numeric_streams_raises(self):
        rows = [("India", "Lalit Sahu", "Tere Bina", "lots", "1.00", "May", 2026, "Spotify")]
        with self.assertRaises(admin_router.ImportRowError) as ctx:
            admin_router._parse_import_rows(self.HEADER, rows)
        self.assertIn("numeric", str(ctx.exception))

    def test_negative_revenue_raises(self):
        rows = [("India", "Lalit Sahu", "Tere Bina", 100, "-1.00", "May", 2026, "Spotify")]
        with self.assertRaises(admin_router.ImportRowError) as ctx:
            admin_router._parse_import_rows(self.HEADER, rows)
        self.assertIn("non-negative", str(ctx.exception))

    def test_no_data_rows_raises(self):
        with self.assertRaises(admin_router.ImportRowError) as ctx:
            admin_router._parse_import_rows(self.HEADER, [(None,) * 8])
        self.assertIn("No data rows", str(ctx.exception))

    def test_row_count_cap_enforced(self):
        row = ("India", "Lalit Sahu", "Tere Bina", 1, "0.01", "May", 2026, "Spotify")
        rows = [row] * (admin_router._MAX_IMPORT_ROWS + 1)
        with self.assertRaises(admin_router.ImportRowError) as ctx:
            admin_router._parse_import_rows(self.HEADER, rows)
        self.assertIn("row limit", str(ctx.exception))


class AdminImportEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_422_when_rate_not_set(self):
        client = FakeClient({"fx_rate_settings": FakeQuery(data=[])})
        file = _FakeUploadFile("report.xlsx", b"unused")

        with patch.object(admin_router, "get_service_client", return_value=client):
            with self.assertRaises(admin_router.HTTPException) as ctx:
                await admin_router.admin_import_song_stats(file=file, email="artist@example.com")

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "usd_to_inr_not_set")

    async def test_rejects_non_xlsx_filename(self):
        client = FakeClient({"fx_rate_settings": FakeQuery(data=[{"usd_to_inr": "80.00"}])})
        file = _FakeUploadFile("report.csv", b"unused")

        with patch.object(admin_router, "get_service_client", return_value=client):
            with self.assertRaises(admin_router.HTTPException) as ctx:
                await admin_router.admin_import_song_stats(file=file, email="artist@example.com")

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_full_import_classifies_new_and_existing_songs_with_fx_conversion(self):
        header = ("Song", "Streams", "Revenue", "Month", "Year", "Platform")
        rows = [
            ("Tere Bina", 100, "1.00", "May", 2026, "Spotify"),  # already on file
            ("BAAP BOL", 20, "2.00", "May", 2026, "YouTube"),  # brand new
        ]
        file = _FakeUploadFile("report.xlsx", _make_xlsx(header, rows))

        client = FakeClient({
            "fx_rate_settings": FakeQuery(data=[{"usd_to_inr": "80.00"}]),
            "song_stats": FakeQuery(data=[
                {"song_title": "Tere Bina", "artist_name": "Lalit Sahu",
                 "submission_id": "sub-1", "revenue": "50.00"},
            ]),
            "submissions": FakeQuery(data=[]),
            "artist_balances": FakeQuery(data=[]),
            "withdrawal_requests": FakeQuery(data=[]),
        })

        with patch.object(admin_router, "get_service_client", return_value=client), \
                patch.object(earnings_service, "get_service_client", return_value=client):
            result = await admin_router.admin_import_song_stats(file=file, email="Artist@Example.com")

        self.assertEqual(result["imported"]["songs_new"], 1)
        self.assertEqual(result["imported"]["songs_updated"], 1)
        self.assertEqual(result["imported"]["rows_written"], 2)
        self.assertEqual(result["imported"]["total_streams"], 120)
        # (1.00 + 2.00) USD * 80.00 = 240.00 INR
        self.assertEqual(result["imported"]["total_revenue_inr"], 240.0)
        self.assertEqual(result["warnings"], [])

        upserted = client.table("song_stats").last_upsert
        self.assertEqual(len(upserted), 2)
        by_title = {r["song_title"]: r for r in upserted}
        self.assertEqual(by_title["Tere Bina"]["submission_id"], "sub-1")
        self.assertEqual(by_title["Tere Bina"]["revenue"], "80.0000")  # 1.00 * 80
        # new song has no submission match (empty submissions table) and
        # falls back to the email's existing artist_name on file
        self.assertIsNone(by_title["BAAP BOL"]["submission_id"])
        self.assertEqual(by_title["BAAP BOL"]["artist_name"], "Lalit Sahu")
        self.assertEqual(by_title["BAAP BOL"]["revenue"], "160.0000")  # 2.00 * 80

        # recompute_balance was actually invoked (against the pre-seeded
        # song_stats data — the fake upsert doesn't feed back into .execute()).
        self.assertEqual(result["balance"]["total_earned"], 50.0)

    async def test_artist_name_mismatch_produces_warning(self):
        header = ("ArtistName", "Song", "Streams", "Revenue", "Month", "Year", "Platform")
        rows = [("Someone Else", "New Song", 10, "1.00", "May", 2026, "Spotify")]
        file = _FakeUploadFile("report.xlsx", _make_xlsx(header, rows))

        client = FakeClient({
            "fx_rate_settings": FakeQuery(data=[{"usd_to_inr": "80.00"}]),
            "song_stats": FakeQuery(data=[
                {"song_title": "Tere Bina", "artist_name": "Lalit Sahu",
                 "submission_id": "sub-1", "revenue": "50.00"},
            ]),
            "submissions": FakeQuery(data=[]),
            "artist_balances": FakeQuery(data=[]),
            "withdrawal_requests": FakeQuery(data=[]),
        })

        with patch.object(admin_router, "get_service_client", return_value=client), \
                patch.object(earnings_service, "get_service_client", return_value=client):
            result = await admin_router.admin_import_song_stats(file=file, email="artist@example.com")

        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("does not match", result["warnings"][0])


if __name__ == "__main__":
    unittest.main()
