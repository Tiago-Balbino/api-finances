import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.main import (app, category_choice, daily_yield_cents, materialize_fixed_bills,
                      monthly_payables, verify_password, yield_start)


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeCategories:
    def execute(self, query, params):
        if "FROM categories" in query:
            return FakeResult({"id": 1, "name": "Moradia"} if params == (1, "expense") else None)
        if "FROM subcategories" in query:
            return FakeResult({"id": 2} if params == (2, 1) else None)
        raise AssertionError(query)


class FakeRecurringBills:
    def __init__(self):
        self.due_dates = []

    def execute(self, query, params):
        if "SELECT * FROM bills WHERE expense_type='fixed'" in query:
            return type("Rows", (), {"fetchall": lambda _: [{
                "id": 7, "account_id": 1, "description": "Aluguel", "category": "Moradia",
                "category_id": 1, "subcategory_id": None, "note": "", "amount_cents": 10000,
                "due_date": date(2026, 1, 31), "interest_rate_basis_points": 0,
                "interest_period": "monthly",
            }]})()
        if "INSERT INTO bills" in query:
            self.due_dates.append(params[7])
            return FakeResult(None)
        raise AssertionError(query)


class ApiTests(unittest.TestCase):
    def test_legacy_node_password_hash_still_works(self):
        old_hash = "0123456789abcdef:62c5c6e51a9f43acf395e9620da3d8d6ab338be6dd7a8421564cc00e1d58c60e2097f8d1834005a29e4d433576ce0d20a8406424750df531d5a0bc4d29759a84"
        self.assertTrue(verify_password("senha-super-segura", old_hash))
        self.assertFalse(verify_password("senha-errada", old_hash))

    def test_subcategory_must_belong_to_category(self):
        self.assertEqual(category_choice(FakeCategories(), {"categoryId": 1, "subcategoryId": 2}, "expense"), (1, 2, "Moradia"))
        with self.assertRaises(HTTPException):
            category_choice(FakeCategories(), {"categoryId": 1, "subcategoryId": 3}, "expense")
        with self.assertRaises(HTTPException):
            category_choice(FakeCategories(), {"categoryId": 1}, "income")

    def test_expected_routes_exist(self):
        paths = {route.path for route in app.routes}
        self.assertTrue({"/health", "/api/state", "/api/categories", "/api/subcategories", "/api/bills/{bill_id}/classification"} <= paths)

    def test_fixed_expense_repeats_monthly_from_original_day(self):
        db = FakeRecurringBills()
        materialize_fixed_bills(db, date(2026, 2, 15))
        self.assertEqual(db.due_dates, [date(2026, 2, 28), date(2026, 3, 31),
            date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 31)])

    def test_unpaid_expense_carries_into_each_next_month(self):
        bill = {"id": 1, "amount_cents": 10000, "due_date": date(2026, 9, 10),
                "interest_rate_basis_points": 0, "interest_period": "monthly",
                "status": "overdue", "payments": []}
        series = monthly_payables([bill], date(2026, 10, 14))
        self.assertEqual([point["value_cents"] for point in series], [10000] * 6)
        self.assertTrue(all(point["items"][0]["carried"] for point in series))

    def test_passive_yield_uses_effective_annual_rate_and_starts_at_eight(self):
        self.assertEqual(daily_yield_cents(1000000, 1000, 10500), 396)
        zone = ZoneInfo("America/Sao_Paulo")
        self.assertEqual(yield_start(datetime(2026, 10, 14, 7, 59, tzinfo=zone)), date(2026, 10, 14))
        self.assertEqual(yield_start(datetime(2026, 10, 14, 8, 0, tzinfo=zone)), date(2026, 10, 15))


if __name__ == "__main__":
    unittest.main()
