from calendar import monthrange
from datetime import date, datetime, timedelta
from functools import lru_cache
from math import floor, isfinite
from zoneinfo import ZoneInfo


def today(zone: str = "America/Araguaina") -> date:
    return datetime.now(ZoneInfo(zone)).date()


def add_months(value: date, count: int) -> date:
    month = value.month - 1 + count
    year = value.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


@lru_cache(maxsize=32)
def banking_holidays(year: int) -> frozenset[date]:
    """Brazilian national banking holidays, including movable dates."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    value = h + l - 7 * m + 114
    easter = date(year, value // 31, value % 31 + 1)
    fixed = {(1, 1), (4, 21), (5, 1), (9, 7), (10, 12),
             (11, 2), (11, 15), (11, 20), (12, 25)}
    return frozenset({date(year, month, day) for month, day in fixed} |
                     {easter - timedelta(days=48), easter - timedelta(days=47),
                      easter - timedelta(days=2), easter + timedelta(days=60)})


def is_business_day(value: date) -> bool:
    return value.weekday() < 5 and value not in banking_holidays(value.year)


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


PERIOD_DAYS = {"daily": 1, "monthly": 30, "yearly": 360}


def applied_rate(bill: dict) -> float:
    """Rate in basis points for each application period, converted pro rata from the informed rate period.

    Example: 2% a month applied daily is 2% / 30 per day."""
    period = bill["interest_period"]
    rate_period = bill.get("interest_rate_period") or period
    return bill["interest_rate_basis_points"] * PERIOD_DAYS[period] / PERIOD_DAYS[rate_period]


def fine_date(due: date, after: str) -> date:
    return due + timedelta(days=1) if after == "day" else add_months(due, 1 if after == "month" else 12)


def debt_cents_at(bill: dict, payments: list[dict], as_of: date) -> int | None:
    balance = bill["amount_cents"]
    due = bill["due_date"]
    rate = applied_rate(bill)
    period = bill["interest_period"]
    fine = bill.get("fine_basis_points") or 0
    fine_at = fine_date(due, bill.get("fine_after") or "day") if fine else None
    last = due

    def accrue(amount: int, until: date) -> int:
        # Interest is charged once per complete application period after the due date.
        if until <= last or amount <= 0 or not rate:
            return amount
        exponent = floor(periods(due, until, period)) - floor(periods(due, last, period))
        return floor(amount * (1 + rate / 10000) ** exponent + 0.5)

    for payment in sorted((p for p in payments if p["date"] <= as_of), key=lambda p: (p["date"], p["id"])):
        when = payment["date"]
        if fine_at and fine_at <= when:
            balance, last = accrue(balance, fine_at), max(last, fine_at)
            balance += floor(balance * fine / 10000 + 0.5) if balance > 0 else 0
            fine_at = None
        balance, last = accrue(balance, when), max(last, when)
        balance = max(0, balance - payment["amount_cents"])
    if fine_at and fine_at <= as_of:
        balance, last = accrue(balance, fine_at), max(last, fine_at)
        balance += floor(balance * fine / 10000 + 0.5) if balance > 0 else 0
    balance = accrue(balance, as_of)
    return balance if abs(balance) < 2**53 else None


def bill_status(bill: dict, payments: list[dict], as_of: date) -> str:
    balance = debt_cents_at(bill, payments, as_of)
    return "paid" if balance == 0 else "overdue" if bill["due_date"] < as_of else "pending"


def day_in_month(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))


def invoice_dates(purchase: date, closing_day: int, due_day: int, offset: int = 0) -> tuple[date, date]:
    """Closing and due dates of the invoice that receives a purchase installment.

    Purchases made on the closing day or later go to the next cycle."""
    month = purchase.replace(day=1)
    if purchase >= day_in_month(purchase.year, purchase.month, closing_day):
        month = add_months(month, 1)
    month = add_months(month, offset)
    closing = day_in_month(month.year, month.month, closing_day)
    due_month = month if due_day > closing_day else add_months(month, 1)
    return closing, day_in_month(due_month.year, due_month.month, due_day)


def split_installments(total_cents: int, count: int) -> list[int]:
    base = total_cents // count
    return [base + total_cents - base * count] + [base] * (count - 1)


def monthly_rate(bill: dict) -> float:
    """Interest of a bill expressed per 30 days, to rank which debt grows faster."""
    return applied_rate(bill) * 30 / PERIOD_DAYS[bill["interest_period"]]


def simulate_payoff(balance: int, bills: list[dict], incomes: list[dict], today: date, months: int = 60) -> dict:
    """Spend every incoming cent on debts, day by day, until everything is paid.

    bills: open bills with payments (real or projected). incomes: {"due_date", "amount_cents", "description"}
    still to be received; overdue ones are assumed to arrive today. Each date, money goes first to the
    bills already due with the highest interest (ties: oldest). Interest and fines keep running on late bills."""
    first_month = today.replace(day=1)
    horizon = add_months(first_month, months) - timedelta(days=1)
    debts = []
    for bill in bills:
        base = debt_cents_at(bill, bill["payments"], max(today, bill["due_date"])) or 0
        if base > 0 and bill["due_date"] <= horizon:
            debts.append({"bill": bill, "payments": list(bill["payments"]), "base": base, "paid": 0, "settled": None})
    arrivals: dict[date, list[dict]] = {}
    for income in incomes:
        if income["amount_cents"] > 0 and income["due_date"] <= horizon:
            arrivals.setdefault(max(today, income["due_date"]), []).append(income)
    events = sorted({today} | set(arrivals) | {debt["bill"]["due_date"] for debt in debts if debt["bill"]["due_date"] > today})
    rows: dict[date, dict] = {}
    cash, sequence = balance, 0

    def row(when: date) -> dict:
        return rows.setdefault(when.replace(day=1), {"income_cents": 0, "paid_cents": 0, "payments": []})

    for when in events:
        for income in arrivals.get(when, []):
            cash += income["amount_cents"]
            row(when)["income_cents"] += income["amount_cents"]
        due = [debt for debt in debts if debt["settled"] is None and debt["bill"]["due_date"] <= when]
        due.sort(key=lambda debt: (-monthly_rate(debt["bill"]), debt["bill"]["due_date"], str(debt["bill"]["id"])))
        for debt in due:
            if cash <= 0:
                break
            value = debt_cents_at(debt["bill"], debt["payments"], when) or 0
            amount = min(cash, value)
            sequence += 1
            debt["payments"].append({"id": 10**12 + sequence, "date": when, "amount_cents": amount})
            debt["paid"] += amount
            cash -= amount
            settled = amount == value
            if settled:
                debt["settled"] = when
            month = row(when)
            month["paid_cents"] += amount
            month["payments"].append({"date": when, "description": debt["bill"]["description"],
                "amount_cents": amount, "settled": settled, "late": when > debt["bill"]["due_date"],
                "due_date": debt["bill"]["due_date"]})

    late = [debt for debt in debts if debt["settled"] is None or debt["settled"] > debt["bill"]["due_date"]]
    unpaid = [debt for debt in debts if debt["settled"] is None]
    payoff = None if unpaid else max((debt["settled"] for debt in late), default=today)
    months_to_payoff = None if payoff is None else (payoff.year - today.year) * 12 + payoff.month - today.month
    last_month = horizon.replace(day=1) if payoff is None else max(payoff.replace(day=1), add_months(first_month, 11))
    timeline, cursor = [], first_month
    while cursor <= last_month:
        end = add_months(cursor, 1) - timedelta(days=1)
        open_debts = [debt for debt in debts if debt["bill"]["due_date"] <= end]
        month = rows.get(cursor, {"income_cents": 0, "paid_cents": 0, "payments": []})
        timeline.append({"month": cursor, **month,
            "debt_cents": sum(debt_cents_at(debt["bill"], debt["payments"], end) or 0 for debt in open_debts),
            "overdue_cents": sum(debt_cents_at(debt["bill"], debt["payments"], end) or 0 for debt in open_debts
                if debt["bill"]["due_date"] < end)})
        cursor = add_months(cursor, 1)
    leftover = sum(debt_cents_at(debt["bill"], debt["payments"], horizon) or 0 for debt in unpaid)
    return {"payoff_date": payoff, "months_to_payoff": months_to_payoff, "horizon_months": months,
        "total_paid_cents": sum(debt["paid"] for debt in debts),
        "interest_cents": sum(debt["paid"] for debt in debts) + leftover - sum(debt["base"] for debt in debts),
        "unpaid_at_horizon_cents": leftover, "unpaid_count": len(unpaid), "cash_left_cents": cash,
        "timeline": timeline}


def payments_fit(bill: dict, payments: list[dict]) -> bool:
    """True when, in date order, no payment is larger than what was owed on its day."""
    ordered = sorted(payments, key=lambda item: (item["date"], item["id"]))
    return all(item["amount_cents"] <= (debt_cents_at(bill, ordered[:index], item["date"]) or 0)
        for index, item in enumerate(ordered))
