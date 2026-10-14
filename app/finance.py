from calendar import monthrange
from datetime import date, datetime
from math import floor, isfinite
from zoneinfo import ZoneInfo


def today(zone: str = "America/Araguaina") -> date:
    return datetime.now(ZoneInfo(zone)).date()


def add_months(value: date, count: int) -> date:
    month = value.month - 1 + count
    year = value.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def periods(start: date, end: date, period: str) -> float:
    if end <= start:
        return 0
    if period == "daily":
        return (end - start).days
    step = 12 if period == "yearly" else 1
    full = max(0, ((end.year - start.year) * 12 + end.month - start.month) // step)
    if add_months(start, full * step) > end:
        full -= 1
    last = add_months(start, full * step)
    next_date = add_months(start, (full + 1) * step)
    return full + (end - last).days / (next_date - last).days


def projected_cents(principal: int, basis_points: int, start: date, end: date, period: str) -> int | None:
    amount = principal * (1 + basis_points / 10000) ** periods(start, end, period)
    return floor(amount + 0.5) if isfinite(amount) and abs(amount) < 2**53 else None


def debt_cents_at(bill: dict, payments: list[dict], as_of: date) -> int | None:
    balance = bill["amount_cents"]
    last = bill["due_date"]
    due = bill["due_date"]
    rate = bill["interest_rate_basis_points"]
    period = bill["interest_period"]
    for payment in sorted((p for p in payments if p["date"] <= as_of), key=lambda p: (p["date"], p["id"])):
        when = payment["date"]
        if when > due and when > last and balance > 0:
            exponent = periods(due, when, period) - periods(due, last, period)
            balance = floor(balance * (1 + rate / 10000) ** exponent + 0.5)
        balance = max(0, balance - payment["amount_cents"])
        last = max(last, when)
    if as_of > due and as_of > last and balance > 0:
        exponent = periods(due, as_of, period) - periods(due, last, period)
        balance = floor(balance * (1 + rate / 10000) ** exponent + 0.5)
    return balance if abs(balance) < 2**53 else None


def bill_status(bill: dict, payments: list[dict], as_of: date) -> str:
    balance = debt_cents_at(bill, payments, as_of)
    return "paid" if balance == 0 else "overdue" if bill["due_date"] < as_of else "pending"
