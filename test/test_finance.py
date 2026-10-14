import unittest
from datetime import date

from app.finance import add_months, bill_status, debt_cents_at, projected_cents


class FinanceTests(unittest.TestCase):
    def test_investment_changes_daily_and_keeps_monthly_rate(self):
        start = date(2026, 1, 10)
        self.assertGreater(projected_cents(10000, 100, start, date(2026, 1, 11), "monthly"), 10000)
        self.assertEqual(projected_cents(10000, 100, start, date(2026, 2, 10), "monthly"), 10100)

    def test_debt_interest_and_partial_payment(self):
        bill = {"amount_cents": 10000, "due_date": date(2026, 10, 10),
                "interest_rate_basis_points": 100, "interest_period": "daily"}
        self.assertEqual(bill_status(bill, [], date(2026, 10, 10)), "pending")
        self.assertEqual(debt_cents_at(bill, [], date(2026, 10, 11)), 10100)
        payments = [{"id": 1, "date": date(2026, 10, 11), "amount_cents": 4000}]
        self.assertEqual(debt_cents_at(bill, payments, date(2026, 10, 11)), 6100)
        self.assertEqual(bill_status(bill, payments, date(2026, 10, 11)), "overdue")
        self.assertEqual(debt_cents_at(bill, payments, date(2026, 10, 12)), 6161)

    def test_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))


if __name__ == "__main__":
    unittest.main()
