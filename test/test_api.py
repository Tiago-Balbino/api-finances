import unittest
from io import BytesIO
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.main import (app, bcb_observations, bill_month_summary, income_month_summary, investment_value, category_choice, daily_yield_cents, fixed_rate_projection, forecast_series,
                      missing_recurrences,
                      monthly_payables, monthly_receivables,
                      verify_password, yield_start)


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
        self.assertTrue({"/health", "/api/state", "/api/dashboard", "/api/accounts/{account_id}/details",
            "/api/categories", "/api/subcategories", "/api/bills/{bill_id}/classification", "/api/forecast",
            "/api/cards", "/api/cards/{card_id}/collateral", "/api/card-purchases/{purchase_id}"} <= paths)

    def test_fixed_expense_repeats_monthly_from_original_day(self):
        origin = {"id": 7, "due_date": date(2026, 1, 31)}
        missing = missing_recurrences([origin], set(), date(2026, 2, 15))
        self.assertEqual([due for _, due in missing], [date(2026, 2, 28), date(2026, 3, 31),
            date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31),
            date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31)])

    def test_existing_or_archived_months_are_not_recreated(self):
        origin = {"id": 7, "due_date": date(2026, 1, 31)}
        existing = {(7, date(2026, month, 1)) for month in range(2, 13)}
        self.assertEqual([due for _, due in missing_recurrences([origin], existing, date(2026, 2, 15))],
            [date(2027, 1, 31)])

    def test_unpaid_expense_carries_into_each_next_month(self):
        bill = {"id": 1, "amount_cents": 10000, "due_date": date(2026, 9, 10),
                "interest_rate_basis_points": 0, "interest_period": "monthly",
                "status": "overdue", "payments": []}
        series = monthly_payables([bill], date(2026, 10, 14))
        self.assertEqual([point["value_cents"] for point in series], [10000] * 6)
        self.assertTrue(all(point["items"][0]["carried"] for point in series))

    def test_unreceived_amount_carries(self):
        income = {"id": 1, "due_date": date(2026, 9, 10), "remaining_cents": 300000}
        series = monthly_receivables([income], date(2026, 10, 14))
        self.assertEqual([point["value_cents"] for point in series], [300000] * 6)
        self.assertTrue(all(point["items"][0]["carried"] for point in series))

    def test_passive_yield_uses_effective_annual_rate_and_starts_at_eight(self):
        self.assertEqual(daily_yield_cents(1000000, 1000, 10500), 396)
        zone = ZoneInfo("America/Sao_Paulo")
        self.assertEqual(yield_start(datetime(2026, 10, 14, 7, 59, tzinfo=zone)), date(2026, 10, 14))
        self.assertEqual(yield_start(datetime(2026, 10, 14, 8, 0, tzinfo=zone)), date(2026, 10, 15))

    def test_bcb_rate_ignores_future_values_and_preserves_observation_date(self):
        payload = b'[{"data":"13/10/2026","valor":"13.65"},{"data":"15/10/2026","valor":"14.00"}]'
        with patch("app.main.urlopen", return_value=BytesIO(payload)) as request:
            self.assertEqual(bcb_observations("cdi", date(2026, 10, 14)), [(date(2026, 10, 13), 1365)])
        self.assertIn("bcdata.sgs.4389", request.call_args.args[0])
        self.assertIn("dataFinal=14%2F10%2F2026", request.call_args.args[0])

    def test_account_projection_holds_rate_and_respects_original_month_end(self):
        points = fixed_rate_projection(100000, 1200, 10000, date(2026, 1, 31), 2)
        self.assertEqual([point["date"] for point in points],
            [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)])
        self.assertGreater(points[2]["value_cents"], points[1]["value_cents"])
        self.assertEqual(fixed_rate_projection(100000, None, 10000, date(2026, 1, 31)),
            [{"date": date(2026, 1, 31), "value_cents": 100000, "yield_cents": 0}])

    def test_forecast_never_assumes_payment_and_accumulates_interest(self):
        overdue = {"id": 1, "amount_cents": 10000, "due_date": date(2026, 9, 10),
                   "interest_rate_basis_points": 100, "interest_period": "monthly",
                   "status": "overdue", "payments": []}
        upcoming = {"id": 2, "amount_cents": 5000, "due_date": date(2026, 11, 20),
                    "interest_rate_basis_points": 0, "interest_period": "monthly",
                    "status": "pending", "payments": []}
        salary = {"id": 3, "due_date": date(2026, 11, 5), "remaining_cents": 300000}
        series = forecast_series(20000, [overdue, upcoming], [salary], date(2026, 10, 14), 3)
        self.assertEqual(len(series), 4)
        october, november, december = series[0], series[1], series[2]
        self.assertEqual(october["balance_cents"], 20000)
        self.assertEqual(november["balance_cents"], 320000)
        self.assertEqual(november["payable_month_cents"], 5000)
        self.assertEqual(december["payable_month_cents"], 0)
        self.assertGreater(december["payable_carried_cents"], november["payable_carried_cents"] - 5000)
        self.assertEqual(december["balance_cents"], 320000)
        self.assertEqual(december["gap_cents"], december["balance_cents"] - december["payable_cents"])

    def bill(self, id, due, amount=10000, kind="eventual", payments=(), card=None, rate=0):
        return {"id": id, "due_date": due, "amount_cents": amount, "expense_type": kind, "credit_card_id": card,
                "interest_rate_basis_points": rate, "interest_period": "monthly", "payments": list(payments)}

    def test_bill_month_splits_overdue_upcoming_paid_and_living_cost(self):
        today = date(2026, 10, 14)
        bills = [
            self.bill(1, date(2026, 9, 10)),                                  # atrasada de setembro
            self.bill(2, date(2026, 10, 5), 20000, "fixed",                   # fixa vencida e parcialmente paga
                      [{"id": 9, "date": date(2026, 10, 6), "amount_cents": 5000}]),
            self.bill(3, date(2026, 10, 20), 30000, "fixed"),                  # fixa a vencer
            self.bill(4, date(2026, 10, 25), 7000),                            # eventual a vencer
            self.bill(5, date(2026, 10, 12), 40000, card=1),                   # fatura
            self.bill(6, date(2026, 11, 5), 99999, "fixed"),                   # mês seguinte
        ]
        incomes = [{"id": 1, "income_type": "fixed", "due_date": date(2026, 10, 5), "amount_cents": 500000}]
        result = bill_month_summary(bills, incomes, date(2026, 10, 1), today)
        self.assertEqual(result["overdue_cents"], 10000 + 15000 + 40000)
        self.assertEqual(result["upcoming_cents"], 30000 + 7000)
        self.assertEqual(result["remaining_cents"], result["overdue_cents"] + result["upcoming_cents"])
        self.assertEqual(result["paid_cents"], 5000)
        self.assertEqual(result["fixed"], {"total_cents": 50000, "paid_cents": 5000, "remaining_cents": 45000, "count": 2})
        self.assertEqual(result["eventual"]["total_cents"], 7000)
        self.assertEqual(result["invoice"]["total_cents"], 40000)
        self.assertEqual(result["living_cost_share_bp"], 1000)
        self.assertEqual({item["id"] for item in result["items"]}, {1, 2, 3, 4, 5})

    def test_future_month_only_counts_earlier_debts_as_overdue(self):
        bills = [self.bill(1, date(2026, 9, 10), rate=100), self.bill(2, date(2026, 11, 20))]
        result = bill_month_summary(bills, [], date(2026, 11, 1), date(2026, 10, 14))
        self.assertGreater(result["overdue_cents"], 10000)   # juros acumulados até o fim de novembro
        self.assertEqual(result["upcoming_cents"], 10000)
        self.assertEqual(result["paid_cents"], 0)

    def test_past_month_shows_payments_made_in_that_month(self):
        bills = [self.bill(1, date(2026, 8, 10), payments=[{"id": 1, "date": date(2026, 8, 9), "amount_cents": 10000}])]
        result = bill_month_summary(bills, [], date(2026, 8, 1), date(2026, 10, 14))
        self.assertEqual((result["paid_cents"], result["remaining_cents"]), (10000, 0))

    def test_income_month_separates_yield_and_what_is_still_coming(self):
        incomes = [{"id": 1, "income_type": "fixed", "due_date": date(2026, 10, 5), "amount_cents": 500000,
                    "receipts": [{"date": date(2026, 10, 5), "amount_cents": 300000}]},
                   {"id": 2, "income_type": "eventual", "due_date": date(2026, 10, 30), "amount_cents": 80000, "receipts": []}]
        received = [{"amount_cents": 300000, "category": "Salário", "automatic_yield": False},
                    {"amount_cents": 1200, "category": "Investimentos", "automatic_yield": True}]
        result = income_month_summary(incomes, received, date(2026, 10, 1), date(2026, 10, 14))
        self.assertEqual((result["received_cents"], result["yield_cents"]), (301200, 1200))
        self.assertEqual((result["overdue_cents"], result["upcoming_cents"]), (200000, 80000))
        self.assertEqual(result["fixed_expected_cents"], 500000)
        self.assertEqual(result["categories"][0]["category"], "Salário")

    def test_investment_value_compounds_each_movement_from_its_date(self):
        box = {"rate_basis_points": 100, "period": "monthly"}
        movements = [{"direction": "deposit", "amount_cents": 10000, "date": date(2026, 1, 10)},
                     {"direction": "deposit", "amount_cents": 10000, "date": date(2026, 2, 10)},
                     {"direction": "withdraw", "amount_cents": 5000, "date": date(2026, 3, 10)}]
        self.assertEqual(investment_value(box, movements, date(2026, 2, 10)), 10100 + 10000)
        self.assertEqual(investment_value(box, movements, date(2026, 3, 10)), 10201 + 10100 - 5000)


if __name__ == "__main__":
    unittest.main()
