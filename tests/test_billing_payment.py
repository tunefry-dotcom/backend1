"""Diwali offer: server-side price, Razorpay amount and verify-payment amount check."""

from __future__ import annotations

import hashlib
import hmac
import unittest
from unittest import mock

from app.modules.billing import payment, plans
from app.modules.billing.plans import Plan

_EXPECTED_RUPEES = {
    Plan.FREE: 0,
    Plan.SINGLE_SONG: 269,  # 299 * 0.9 = 269.1 -> floor
    Plan.STARTER: 899,
    Plan.SINGLE_ARTIST: 1439,
    Plan.DOUBLE_ARTIST: 2699,
    Plan.LABEL: 6299,
}


class OfferPriceTests(unittest.TestCase):
    def test_offer_prices(self):
        for plan, rupees in _EXPECTED_RUPEES.items():
            self.assertEqual(plans.offer_price_inr(plan), rupees, plan)

    def test_free_has_no_offer(self):
        self.assertEqual(plans.offer_pct(Plan.FREE), 0)

    def test_offer_off_restores_list_price(self):
        with mock.patch.object(plans, "DIWALI_OFFER_ACTIVE", False):
            for plan in Plan:
                self.assertEqual(plans.offer_price_inr(plan), plans.get_spec(plan).price_inr)
                self.assertEqual(plans.offer_pct(plan), 0)


class AmountPaiseTests(unittest.TestCase):
    def test_amount_paise_uses_offer_price(self):
        with mock.patch.object(payment.settings, "payment_amount_divisor", 1):
            self.assertEqual(payment.amount_paise(Plan.SINGLE_ARTIST), 143900)
            self.assertEqual(payment.amount_paise(Plan.LABEL), 629900)

    def test_divisor_still_applies(self):
        with mock.patch.object(payment.settings, "payment_amount_divisor", 100):
            self.assertEqual(payment.amount_paise(Plan.SINGLE_ARTIST), 1439)


class VerifyAmountTests(unittest.TestCase):
    SECRET = "secret"

    def _verify(self, order_amount: int):
        order_id, pay_id = "order_1", "pay_1"
        sig = hmac.new(
            self.SECRET.encode(), f"{order_id}|{pay_id}".encode(), hashlib.sha256
        ).hexdigest()
        resp = mock.Mock()
        resp.json.return_value = {
            "status": "paid",
            "amount": order_amount,
            "notes": {"user_id": "u1", "plan": Plan.SINGLE_ARTIST.value},
        }
        with mock.patch.object(payment, "_check_enabled", return_value=("key", self.SECRET)), \
             mock.patch.object(payment.settings, "payment_amount_divisor", 1), \
             mock.patch.object(payment.httpx, "get", return_value=resp):
            payment.verify_payment("u1", Plan.SINGLE_ARTIST, order_id, pay_id, sig)

    def test_accepts_discounted_amount(self):
        self._verify(143900)

    def test_rejects_list_price_order(self):
        with self.assertRaises(payment.PaymentError):
            self._verify(159900)


if __name__ == "__main__":
    unittest.main()
