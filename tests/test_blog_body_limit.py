from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.modules.blog.schemas import MAX_BODY_CHARS, AdminTunefryPublishRequest


def _req(body: str) -> AdminTunefryPublishRequest:
    return AdminTunefryPublishRequest(
        final_title="t", final_body=body, category="informative", cover_image_keys=["blog/a.png"]
    )


class BlogBodyLimitTests(unittest.TestCase):
    def test_long_admin_guide_is_accepted(self) -> None:
        # Regression: a ~30k-char pasted guide used to 422 against the old 20k cap.
        self.assertEqual(len(_req("x" * 30000).final_body), 30000)

    def test_over_cap_rejected_with_length_message(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            _req("x" * (MAX_BODY_CHARS + 1))
        self.assertIn("at most", ctx.exception.errors()[0]["msg"])


if __name__ == "__main__":
    unittest.main()
