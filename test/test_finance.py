import unittest
from datetime import date, timedelta

from app.finance import (add_months, bill_status, debt_cents_at, invoice_dates, is_business_day, payments_fit,
                         projected_cents, simulate_payoff, split_installments)


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

    def test_brazilian_banking_holidays(self):
        self.assertFalse(is_business_day(date(2026, 2, 16)))  # Carnaval
        self.assertFalse(is_business_day(date(2026, 2, 17)))
        self.assertFalse(is_business_day(date(2026, 4, 3)))   # Sexta-Feira Santa
        self.assertFalse(is_business_day(date(2026, 6, 4)))   # Corpus Christi
        self.assertFalse(is_business_day(date(2026, 11, 20)))
        self.assertTrue(is_business_day(date(2026, 10, 14)))

    def test_monthly_rate_applied_daily_is_split_by_thirty_days(self):
        bill = {"amount_cents": 300000, "due_date": date(2026, 10, 10), "interest_rate_basis_points": 300,
                "interest_period": "daily", "interest_rate_period": "monthly"}
        self.assertEqual(debt_cents_at(bill, [], date(2026, 10, 11)), 300300)  # 3% / 30 = 0.1% per day

    def test_monthly_application_only_charges_complete_months(self):
        bill = {"amount_cents": 10000, "due_date": date(2026, 10, 10), "interest_rate_basis_points": 1200,
                "interest_period": "monthly", "interest_rate_period": "yearly"}
        self.assertEqual(debt_cents_at(bill, [], date(2026, 11, 9)), 10000)
        self.assertEqual(debt_cents_at(bill, [], date(2026, 11, 10)), 10100)  # 12% a.a. = 1% a.m.

    def test_fine_is_charged_once_after_the_chosen_delay(self):
        bill = {"amount_cents": 10000, "due_date": date(2026, 10, 10), "interest_rate_basis_points": 0,
                "interest_period": "monthly", "fine_basis_points": 200, "fine_after": "month"}
        self.assertEqual(debt_cents_at(bill, [], date(2026, 11, 9)), 10000)
        self.assertEqual(debt_cents_at(bill, [], date(2026, 11, 10)), 10200)
        self.assertEqual(debt_cents_at(bill, [], date(2027, 5, 10)), 10200)
        paid = [{"id": 1, "date": date(2026, 10, 20), "amount_cents": 5000}]
        self.assertEqual(debt_cents_at(bill, paid, date(2026, 12, 1)), 5100)
        on_time = [{"id": 1, "date": date(2026, 10, 10), "amount_cents": 10000}]
        self.assertEqual(debt_cents_at({**bill, "fine_after": "day"}, on_time, date(2026, 12, 1)), 0)

    def test_purchase_on_or_after_closing_day_goes_to_next_invoice(self):
        self.assertEqual(invoice_dates(date(2026, 10, 4), 5, 12), (date(2026, 10, 5), date(2026, 10, 12)))
        self.assertEqual(invoice_dates(date(2026, 10, 5), 5, 12), (date(2026, 11, 5), date(2026, 11, 12)))
        self.assertEqual(invoice_dates(date(2026, 10, 20), 5, 12), (date(2026, 11, 5), date(2026, 11, 12)))

    def test_due_day_before_closing_day_falls_in_next_month(self):
        self.assertEqual(invoice_dates(date(2026, 10, 1), 25, 5), (date(2026, 10, 25), date(2026, 11, 5)))

    def test_invoice_days_respect_month_end_and_installments_cross_year(self):
        self.assertEqual(invoice_dates(date(2026, 2, 1), 31, 10), (date(2026, 2, 28), date(2026, 3, 10)))
        self.assertEqual(invoice_dates(date(2026, 11, 20), 5, 12, 2), (date(2027, 2, 5), date(2027, 2, 12)))

    def test_installments_sum_exactly_with_remainder_first(self):
        self.assertEqual(split_installments(1000, 3), [334, 333, 333])
        self.assertEqual(sum(split_installments(99999, 7)), 99999)

    def debt(self, id, due, amount, rate=0):
        return {"id": id, "description": f"D{id}", "due_date": due, "amount_cents": amount,
                "interest_rate_basis_points": rate, "interest_period": "monthly", "payments": []}

    def test_simulation_pays_highest_interest_first_and_reports_payoff(self):
        today = date(2026, 10, 14)
        bills = [self.debt(1, date(2026, 9, 10), 50000), self.debt(2, date(2026, 9, 20), 50000, rate=500)]
        incomes = [{"due_date": date(2026, 11, 5), "amount_cents": 60000, "description": "Salário"},
                   {"due_date": date(2026, 12, 5), "amount_cents": 60000, "description": "Salário"}]
        result = simulate_payoff(10000, bills, incomes, today, months=12)
        october = result["timeline"][0]
        self.assertEqual(october["payments"][0]["description"], "D2")  # 5% a.m. antes da sem juros
        self.assertEqual(result["payoff_date"], date(2026, 12, 5))
        self.assertEqual(result["months_to_payoff"], 2)
        self.assertGreater(result["interest_cents"], 0)
        self.assertEqual(result["total_paid_cents"], 100000 + result["interest_cents"])

    def test_simulation_without_enough_income_never_pays_off(self):
        today = date(2026, 10, 14)
        bills = [self.debt(1, date(2026, 9, 10), 500000, rate=300)]
        incomes = [{"due_date": date(2026, 11, 5), "amount_cents": 1000, "description": "Bico"}]
        result = simulate_payoff(0, bills, incomes, today, months=6)
        self.assertIsNone(result["payoff_date"])
        self.assertGreater(result["unpaid_at_horizon_cents"], 500000)
        self.assertEqual(len(result["timeline"]), 6)

    def test_future_bill_paid_on_due_date_is_not_late(self):
        today = date(2026, 10, 14)
        bills = [self.debt(1, date(2026, 11, 10), 30000, rate=1000)]
        incomes = [{"due_date": date(2026, 11, 5), "amount_cents": 30000, "description": "Salário"}]
        result = simulate_payoff(0, bills, incomes, today, months=12)
        self.assertEqual(result["months_to_payoff"], 0)
        self.assertEqual(result["interest_cents"], 0)

    def test_payments_fit_checks_each_payment_against_the_balance_of_its_day(self):
        bill = {"amount_cents": 10000, "due_date": date(2026, 10, 10), "interest_rate_basis_points": 0,
                "interest_period": "monthly"}
        early = {"id": 1, "date": date(2026, 10, 1), "amount_cents": 4000}
        later = {"id": 2, "date": date(2026, 10, 5), "amount_cents": 6000}
        self.assertTrue(payments_fit(bill, [later, early]))  # dates out of order are fine
        self.assertFalse(payments_fit(bill, [early, {**later, "amount_cents": 6001}]))
        self.assertFalse(payments_fit({**bill, "amount_cents": 5000}, [early, later]))  # invoice reduced below what was paid

    def assert_reconciles(self, balance, bills, today, result):
        """Cash and debt must close every month, to the cent."""
        opening = balance
        previous_debt = 0
        for month in result["timeline"]:
            start = month["month"]
            end = add_months(start, 1) - timedelta(days=1)
            self.assertEqual(month["opening_cash_cents"], opening)
            self.assertEqual(month["closing_cash_cents"], opening + month["income_cents"] - month["paid_cents"])
            self.assertGreaterEqual(month["closing_cash_cents"], 0)
            self.assertEqual(month["paid_cents"], sum(item["amount_cents"] for item in month["payments"]))
            new = sum(debt_cents_at(bill, bill["payments"], max(today, bill["due_date"]))
                for bill in bills if (bill["due_date"] <= end if start <= today else start <= bill["due_date"] <= end))
            interest = month["debt_cents"] - previous_debt - new + month["paid_cents"]
            self.assertGreaterEqual(interest, 0, f"juros negativos em {start}")
            previous_debt, opening = month["debt_cents"], month["closing_cash_cents"]
        self.assertEqual(result["total_paid_cents"] + result["unpaid_at_horizon_cents"],
            result["debt_today_cents"] + result["interest_cents"])

    def test_simulation_reconciles_cash_and_debt_every_month(self):
        today = date(2026, 10, 14)
        bills = [self.debt(1, date(2026, 8, 10), 1500000, rate=400), self.debt(2, date(2026, 10, 14), 30000, rate=200),
                 self.debt(3, date(2026, 11, 12), 10000, rate=1000), self.debt(4, date(2027, 1, 20), 45000)]
        bills[0].update(interest_period="daily", interest_rate_period="monthly", fine_basis_points=200, fine_after="day")
        incomes = [{"due_date": add_months(date(2026, 11, 5), i), "amount_cents": 250000, "description": "Salário"} for i in range(24)]
        result = simulate_payoff(395000, bills, incomes, today, months=24)
        self.assert_reconciles(395000, bills, today, result)
        self.assertEqual(result["total_income_cents"], 23 * 250000)  # out/2028 fica fora do horizonte de 24 meses
        self.assertEqual(result["cash_left_cents"], 395000 + 23 * 250000 - result["total_paid_cents"])

    def test_bills_due_before_next_income_are_paid_on_time(self):
        today = date(2026, 10, 14)
        loan = self.debt(1, date(2026, 8, 10), 1000000, rate=400)
        invoice = self.debt(2, date(2026, 11, 12), 10000, rate=1000)
        incomes = [{"due_date": date(2026, 11, 5), "amount_cents": 250000, "description": "Salário"},
                   {"due_date": date(2026, 12, 5), "amount_cents": 250000, "description": "Salário"}]
        result = simulate_payoff(0, [loan, invoice], incomes, today, months=12)
        november = result["timeline"][1]["payments"]
        self.assertEqual([(item["description"], item["amount_cents"]) for item in november],
                         [("D1", 240000), ("D2", 10000)])  # R$ 100 guardados para a fatura de 12/11
        self.assertFalse(november[1]["late"])
        self.assert_reconciles(0, [loan, invoice], today, result)


if __name__ == "__main__":
    unittest.main()
