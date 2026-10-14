import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.rows import dict_row

from .finance import (add_months, bill_status, debt_cents_at, invoice_dates, is_business_day, projected_cents,
                      split_installments, today)


def connect():
    return psycopg.connect(os.getenv("DATABASE_URL", "dbname=" + os.getenv("PGDATABASE", "finances")), row_factory=dict_row, prepare_threshold=None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with connect() as db:
        db.execute(Path(__file__).resolve().parents[1].joinpath("schema.sql").read_text())
    yield


app = FastAPI(title="Finances API", version="3.0", lifespan=lifespan)
login_attempts: dict[str, tuple[int, float]] = {}


@app.exception_handler(UniqueViolation)
async def duplicate_handler(_: Request, error: UniqueViolation):
    return JSONResponse(status_code=409, content={"error": "Registro já existente."})


@app.exception_handler(ForeignKeyViolation)
async def foreign_key_handler(_: Request, error: ForeignKeyViolation):
    return JSONResponse(status_code=409, content={"error": "Há dados associados a este registro."})


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, error: HTTPException):
    return JSONResponse(status_code=error.status_code, content={"error": error.detail})


def fail(status: int, message: str):
    raise HTTPException(status_code=status, detail=message)


def db_session():
    with connect() as db:
        yield db


def one(db, query: str, params: tuple = ()):
    return db.execute(query, params).fetchone()


def all_rows(db, query: str, params: tuple = ()):
    return db.execute(query, params).fetchall()


def text(value: Any, label: str, limit: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        fail(400, f"{label} inválido.")
    return value.strip()


def note(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 2000:
        fail(400, "Observações devem ter até 2000 caracteres.")
    return value.strip()


def integer(value: Any, label: str, minimum: int = 1, maximum: int = 100_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        fail(400, f"{label} inválido.")
    return value


def parsed_date(value: Any) -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return date.fromisoformat(value)
    except ValueError:
        fail(400, "Data inválida.")


def interest(value: Any, period: Any) -> int:
    if period not in ("daily", "monthly", "yearly"):
        fail(400, "Periodicidade dos juros inválida.")
    return integer(value, "Taxa de juros", 0, 1000 if period == "daily" else 10000)


def interest_period(value: Any) -> str:
    if value not in ("daily", "monthly", "yearly"):
        fail(400, "Aplicação dos juros inválida.")
    return value


def bill_extras(value: dict) -> tuple:
    """Payment method, period of the informed interest rate and the one-time late fine."""
    rate_period = value.get("interestRatePeriod") or value.get("interestPeriod")
    if rate_period not in ("daily", "monthly", "yearly"):
        fail(400, "Período da taxa de juros inválido.")
    fine_after = value.get("fineAfter", "day")
    if fine_after not in ("day", "month", "year"):
        fail(400, "Prazo da multa inválido.")
    return (payment_method(value.get("paymentMethod", "pix")), rate_period,
        integer(value.get("fineBasisPoints", 0), "Multa", 0, 10000), fine_after)


def kind(value: Any) -> str:
    if value not in ("income", "expense"):
        fail(400, "Tipo inválido.")
    return value


def category_choice(db, value: dict, expected_kind: str):
    category_id = integer(value.get("categoryId"), "Categoria")
    category = one(db, "SELECT id,name FROM categories WHERE id=%s AND kind=%s", (category_id, expected_kind))
    if not category:
        fail(400, "Selecione uma categoria válida.")
    sub_id = value.get("subcategoryId") or None
    if sub_id is not None:
        sub_id = integer(sub_id, "Subcategoria")
        if not one(db, "SELECT id FROM subcategories WHERE id=%s AND category_id=%s", (sub_id, category_id)):
            fail(400, "Selecione uma subcategoria dessa categoria.")
    return category_id, sub_id, category["name"]


def password_hash(password: Any) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 200:
        fail(400, "A senha deve ter entre 12 e 200 caracteres.")
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64)
    return f"{salt}:{digest.hex()}"


def verify_password(password: Any, stored: str) -> bool:
    if not isinstance(password, str) or len(password) > 200:
        return False
    try:
        salt, expected = stored.split(":", 1)
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def set_session(response: Response, db, user_id: int):
    token = secrets.token_hex(32)
    db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(%s,%s,now() + interval '30 days')", (hashlib.sha256(token.encode()).hexdigest(), user_id))
    response.set_cookie("session", token, max_age=30 * 86400, httponly=True, samesite="strict", secure=os.getenv("COOKIE_SECURE") == "true")


def maybe_user(request: Request, db):
    token = request.cookies.get("session", "")
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        return None
    return one(db, """SELECT u.id,u.name,u.email,u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=%s AND s.expires_at>now() AND u.active=TRUE""", (hashlib.sha256(token.encode()).hexdigest(),))


def user(request: Request, db=Depends(db_session)):
    result = maybe_user(request, db)
    if not result:
        fail(401, "Entre na sua conta.")
    return result


def check_origin(request: Request):
    origin = request.headers.get("origin")
    if not origin:
        return
    # ALLOWED_ORIGINS: domínios do front que repassam /api para cá (ex.: rewrite da Vercel), separados por vírgula.
    allowed = {request.headers.get("host")} | {urlsplit(o.strip()).netloc for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()}
    if urlsplit(origin).netloc not in allowed:
        fail(403, "Origem não permitida.")


def mutation(request: Request, db=Depends(db_session)):
    check_origin(request)
    result = maybe_user(request, db)
    if not result:
        fail(401, "Entre na sua conta.")
    return result


def admin(result):
    if result["role"] != "admin":
        fail(403, "Somente o administrador pode fazer isso.")


FORECAST_MONTHS = 12
PAYMENT_METHODS = ("pix", "debit", "credit")


def payment_method(value: Any) -> str:
    if value not in PAYMENT_METHODS:
        fail(400, "Forma de pagamento inválida.")
    return value


def bill_rows(db, as_of: date):
    bills = all_rows(db, """SELECT b.*,origin.recurrence_active AS series_active,
        c.name AS category_name,s.name AS subcategory_name,a.name AS account_name,cc.name AS card_name
        FROM bills b JOIN accounts a ON a.id=b.account_id LEFT JOIN credit_cards cc ON cc.id=b.credit_card_id
        LEFT JOIN bills origin ON origin.id=COALESCE(b.recurrence_origin_id,b.id)
        LEFT JOIN categories c ON c.id=b.category_id LEFT JOIN subcategories s ON s.id=b.subcategory_id
        WHERE b.archived=FALSE ORDER BY b.due_date,b.id""")
    payments = all_rows(db, """SELECT t.id,t.bill_id,t.account_id,t.amount_cents,t.date,t.note,t.payment_method,
        t.card_purchase_id,cp.installments,cc.name AS card_name,a.name AS account_name
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        LEFT JOIN card_purchases cp ON cp.id=t.card_purchase_id LEFT JOIN credit_cards cc ON cc.id=t.credit_card_id
        WHERE t.bill_id IS NOT NULL ORDER BY t.date,t.id""")
    by_bill: dict[int, list[dict]] = {}
    purchases: dict[int, dict] = {}
    for payment in payments:
        # A credit payment has one row per installment; show and settle it as a single payment.
        if payment["card_purchase_id"]:
            if payment["card_purchase_id"] in purchases:
                purchases[payment["card_purchase_id"]]["amount_cents"] += payment["amount_cents"]
                continue
            payment = purchases.setdefault(payment["card_purchase_id"], dict(payment))
        by_bill.setdefault(payment["bill_id"], []).append(payment)
    result = []
    for bill in bills:
        history = by_bill.get(bill["id"], [])
        current = debt_cents_at(bill, history, as_of)
        result.append({**bill, "category": bill["category_name"] or bill["category"], "payments": history,
            "current_cents": current, "status": bill_status(bill, history, as_of),
            "days_overdue": (as_of - bill["due_date"]).days if bill["due_date"] < as_of and current else 0,
            "paid_cents": sum(p["amount_cents"] for p in history)})
    return result


def materialize_fixed_bills(db, as_of: date):
    """Create one independent payable bill per month, without duplicating existing months."""
    last_month = add_months(as_of.replace(day=1), FORECAST_MONTHS - 1)
    horizon = add_months(last_month, 1)
    origins = all_rows(db, """SELECT * FROM bills WHERE expense_type='fixed'
        AND recurrence_origin_id IS NULL AND recurrence_active=TRUE AND archived=FALSE
        AND due_date < %s ORDER BY id FOR UPDATE""", (horizon,))
    for origin in origins:
        month_count = (last_month.year - origin["due_date"].year) * 12 + last_month.month - origin["due_date"].month
        for offset in range(1, month_count + 1):
            due = add_months(origin["due_date"], offset)
            month = due.replace(day=1)
            db.execute("""INSERT INTO bills(account_id,description,category,category_id,subcategory_id,note,
                amount_cents,due_date,interest_rate_basis_points,interest_period,expense_type,
                recurrence_origin_id,recurrence_month,payment_method,interest_rate_period,fine_basis_points,fine_after)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'fixed',%s,%s,%s,%s,%s,%s)
                ON CONFLICT (recurrence_origin_id,recurrence_month) DO NOTHING""",
                (origin["account_id"], origin["description"], origin["category"], origin["category_id"],
                 origin["subcategory_id"], origin["note"], origin["amount_cents"], due,
                 origin["interest_rate_basis_points"], origin["interest_period"], origin["id"], month,
                 origin.get("payment_method", "pix"), origin.get("interest_rate_period"),
                 origin.get("fine_basis_points", 0), origin.get("fine_after", "day")))


def materialize_fixed_incomes(db, as_of: date):
    last_month = add_months(as_of.replace(day=1), FORECAST_MONTHS - 1)
    horizon = add_months(last_month, 1)
    origins = all_rows(db, """SELECT * FROM planned_incomes WHERE income_type='fixed'
        AND recurrence_origin_id IS NULL AND recurrence_active=TRUE AND archived=FALSE
        AND due_date < %s ORDER BY id FOR UPDATE""", (horizon,))
    for origin in origins:
        month_count = (last_month.year - origin["due_date"].year) * 12 + last_month.month - origin["due_date"].month
        for offset in range(1, month_count + 1):
            due = add_months(origin["due_date"], offset)
            db.execute("""INSERT INTO planned_incomes(account_id,description,category,category_id,
                subcategory_id,note,amount_cents,due_date,income_type,recurrence_origin_id,recurrence_month)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'fixed',%s,%s)
                ON CONFLICT (recurrence_origin_id,recurrence_month) DO NOTHING""",
                (origin["account_id"], origin["description"], origin["category"], origin["category_id"],
                 origin["subcategory_id"], origin["note"], origin["amount_cents"], due,
                 origin["id"], due.replace(day=1)))


def income_rows(db, as_of: date):
    incomes = all_rows(db, """SELECT i.*,origin.recurrence_active AS series_active,
        c.name AS category_name,s.name AS subcategory_name,a.name AS account_name
        FROM planned_incomes i JOIN accounts a ON a.id=i.account_id
        LEFT JOIN planned_incomes origin ON origin.id=COALESCE(i.recurrence_origin_id,i.id)
        LEFT JOIN categories c ON c.id=i.category_id LEFT JOIN subcategories s ON s.id=i.subcategory_id
        WHERE i.archived=FALSE ORDER BY i.due_date,i.id""")
    receipts = all_rows(db, """SELECT t.id,t.planned_income_id,t.account_id,t.amount_cents,t.date,t.note,
        a.name AS account_name FROM transactions t JOIN accounts a ON a.id=t.account_id
        WHERE t.planned_income_id IS NOT NULL ORDER BY t.date,t.id""")
    grouped = {}
    for receipt in receipts:
        grouped.setdefault(receipt["planned_income_id"], []).append(receipt)
    result = []
    for income in incomes:
        history = grouped.get(income["id"], [])
        received = sum(item["amount_cents"] for item in history)
        remaining = max(0, income["amount_cents"] - received)
        result.append({**income, "category": income["category_name"] or income["category"],
            "receipts": history, "received_cents": received, "remaining_cents": remaining,
            "status": "received" if not remaining else "overdue" if income["due_date"] < as_of else "pending"})
    return result


def monthly_receivables(incomes: list[dict], current: date, months: int = 6) -> list[dict]:
    series = []
    for offset in range(months):
        start = add_months(current.replace(day=1), offset)
        end = add_months(start, 1) - timedelta(days=1)
        items = [{"id": item["id"], "value_cents": item["remaining_cents"],
            "carried": item["due_date"] < start} for item in incomes
            if item["remaining_cents"] and item["due_date"] <= end]
        series.append({"date": start, "value_cents": sum(item["value_cents"] for item in items), "items": items})
    return series


def month_bounds(value: str | None, current: date) -> tuple[date, date]:
    if value is None:
        start = current.replace(day=1)
    elif not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail(400, "Mês inválido.")
    else:
        start = parsed_date(f"{value}-01")
    return start, add_months(start, 1)


def fixed_rate_projection(balance: int, annual_points: int | None, multiplier: int,
                          current: date, months: int = 12) -> list[dict]:
    """Project the current balance with the present rate held constant and no cash flows."""
    series = [{"date": current, "value_cents": balance, "yield_cents": 0}]
    if annual_points is None:
        return series
    amount = balance
    cursor = current + timedelta(days=1)
    previous = balance
    for offset in range(1, months + 1):
        end = add_months(current, offset)
        while cursor <= end:
            if is_business_day(cursor):
                amount += daily_yield_cents(amount, annual_points, multiplier)
            cursor += timedelta(days=1)
        series.append({"date": end, "value_cents": amount, "yield_cents": amount - previous})
        previous = amount
    return series


YIELD_ZONE = ZoneInfo("America/Sao_Paulo")
BCB_SERIES = {"cdi": 4389, "selic_target": 432}


def yield_clock() -> datetime:
    return datetime.now(YIELD_ZONE)


def yield_start(now: datetime) -> date:
    return now.date() + timedelta(days=1 if now.time() >= clock_time(8) else 0)


def bcb_observations(source_code: str, as_of: date) -> list[tuple[date, int]]:
    if source_code not in BCB_SERIES:
        fail(400, "Indicador inválido.")
    params = urlencode({"formato": "json", "dataInicial": (as_of - timedelta(days=35)).strftime("%d/%m/%Y"),
        "dataFinal": as_of.strftime("%d/%m/%Y")})
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{BCB_SERIES[source_code]}/dados?{params}"
    with urlopen(url, timeout=8) as response:
        records = json.load(response)
    if not isinstance(records, list) or not records:
        raise ValueError("O Banco Central não retornou valores para o indicador.")
    result = []
    for item in records:
        when = datetime.strptime(item["data"], "%d/%m/%Y").date()
        points = int((Decimal(str(item["valor"]).replace(",", ".")) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if when <= as_of and 0 <= points <= 10000:
            result.append((when, points))
    if not result:
        raise ValueError("O Banco Central não retornou uma taxa válida até hoje.")
    return sorted(result)


def save_bcb_observations(db, source_code: str, records: list[tuple[date, int]]):
    for when, points in records:
        db.execute("""INSERT INTO yield_rate_observations(source_code,date,annual_basis_points)
            VALUES(%s,%s,%s) ON CONFLICT (source_code,date)
            DO UPDATE SET annual_basis_points=EXCLUDED.annual_basis_points""", (source_code, when, points))


def sync_dynamic_rates(db, as_of: date | None = None, force: bool = False) -> dict:
    as_of = as_of or yield_clock().date()
    rates = all_rows(db, "SELECT id,source_code,last_checked_at FROM yield_rates WHERE source_code IS NOT NULL FOR UPDATE")
    due = {item["source_code"] for item in rates
        if force or not item["last_checked_at"] or item["last_checked_at"].astimezone(YIELD_ZONE).date() < as_of}
    errors = {}
    for source_code in due:
        try:
            records = bcb_observations(source_code, as_of)
            save_bcb_observations(db, source_code, records)
            when, points = records[-1]
            db.execute("""UPDATE yield_rates SET annual_basis_points=%s,source_date=%s,
                last_checked_at=now(),sync_error='' WHERE source_code=%s""", (points, when, source_code))
        except (OSError, ValueError, ArithmeticError, KeyError, TypeError) as error:
            errors[source_code] = str(error)[:200]
            db.execute("UPDATE yield_rates SET last_checked_at=now(),sync_error=%s WHERE source_code=%s",
                ("Não foi possível consultar o Banco Central. O último valor conhecido foi mantido.", source_code))
    return {"updated": sorted(due - errors.keys()), "errors": errors}


def daily_yield_cents(balance_cents: int, annual_basis_points: int, multiplier_basis_points: int) -> int:
    # Convert the effective annual rate to one of 252 business-day periods.
    if balance_cents <= 0 or annual_basis_points <= 0 or multiplier_basis_points <= 0:
        return 0
    with localcontext() as context:
        context.prec = 32
        annual = Decimal(annual_basis_points) * Decimal(multiplier_basis_points) / Decimal(100000000)
        daily = (Decimal(1) + annual) ** (Decimal(1) / Decimal(252)) - Decimal(1)
        return int((Decimal(balance_cents) * daily).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def accrue_passive_yields(db, now: datetime | None = None) -> int:
    now = now or yield_clock()
    cutoff = now.date() if now.time() >= clock_time(8) else now.date() - timedelta(days=1)
    accounts = all_rows(db, """SELECT a.*,r.name AS rate_name,r.annual_basis_points,r.source_code
        FROM accounts a JOIN yield_rates r ON r.id=a.yield_rate_id
        WHERE a.yield_start_date IS NOT NULL AND a.yield_start_date <= %s
        ORDER BY a.id FOR UPDATE OF a""", (cutoff,))
    category = one(db, "SELECT id FROM categories WHERE kind='income' AND name='Investimentos'")
    created = 0
    for account in accounts:
        last = one(db, "SELECT max(date) AS last FROM passive_yields WHERE account_id=%s", (account["id"],))["last"]
        current = max(account["yield_start_date"], last + timedelta(days=1)) if last else account["yield_start_date"]
        while current <= cutoff:
            if is_business_day(current):
                at_eight = datetime.combine(current, clock_time(8), tzinfo=YIELD_ZONE)
                balance = one(db, """SELECT a.opening_cents + COALESCE(SUM(CASE WHEN t.kind='income'
                    THEN t.amount_cents ELSE -t.amount_cents END),0) AS cents
                    FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id AND t.movement_type<>'card_purchase'
                    AND (t.date < %s OR (t.date = %s AND t.created_at <= %s))
                    WHERE a.id=%s GROUP BY a.id""", (current, current, at_eight, account["id"]))["cents"]
                observation = one(db, """SELECT annual_basis_points FROM yield_rate_observations
                    WHERE source_code=%s AND date<=%s ORDER BY date DESC LIMIT 1""",
                    (account["source_code"], current)) if account["source_code"] else None
                annual_points = observation["annual_basis_points"] if observation else account["annual_basis_points"]
                amount = daily_yield_cents(balance, annual_points,
                    account["yield_multiplier_basis_points"])
                transaction_id = None
                if amount:
                    result = one(db, """INSERT INTO transactions(account_id,kind,description,category,category_id,
                        note,amount_cents,date) VALUES(%s,'income',%s,'Investimentos',%s,%s,%s,%s) RETURNING id""",
                        (account["id"], f"Rendimento: {account['name']}", category["id"] if category else None,
                         f"{account['yield_multiplier_basis_points'] / 100:g}% de {account['rate_name']}"
                         f" ({annual_points / 100:g}% a.a.)", amount, current))
                    transaction_id = result["id"]
                    created += 1
                db.execute("INSERT INTO passive_yields(account_id,date,amount_cents,transaction_id) VALUES(%s,%s,%s,%s)",
                    (account["id"], current, amount, transaction_id))
            current += timedelta(days=1)
    return created


def monthly_payables(bills: list[dict], current: date, months: int = 6) -> list[dict]:
    series = []
    for i in range(months):
        month_start = add_months(current.replace(day=1), i)
        month_end = add_months(month_start, 1) - timedelta(days=1)
        projection_date = current if i == 0 else month_end
        items = [{"id": b["id"], "value_cents": debt_cents_at(b, b["payments"], max(projection_date, b["due_date"])),
            "carried": b["due_date"] < month_start} for b in bills
            if b["status"] != "paid" and b["due_date"] <= month_end]
        series.append({"date": month_start, "value_cents": sum(item["value_cents"] or 0 for item in items),
            "items": items})
    return series


def forecast_series(balance_cents: int, bills: list[dict], incomes: list[dict], current: date,
                    months: int = FORECAST_MONTHS) -> list[dict]:
    """Project each month assuming no open bill is ever paid: unpaid bills keep accumulating
    (with interest) and never reduce the balance; planned incomes arrive on their due date."""
    payables = monthly_payables(bills, current, months + 1)
    receivables = monthly_receivables(incomes, current, months + 1)
    series = []
    for payable, receivable in zip(payables, receivables):
        carried = sum(item["value_cents"] or 0 for item in payable["items"] if item["carried"])
        balance = balance_cents + receivable["value_cents"]
        series.append({"date": payable["date"], "balance_cents": balance,
            "receivable_cents": receivable["value_cents"], "payable_cents": payable["value_cents"],
            "payable_month_cents": payable["value_cents"] - carried, "payable_carried_cents": carried,
            "gap_cents": balance - payable["value_cents"],
            "items": payable["items"], "income_items": receivable["items"]})
    return series


def card_for_account(db, card_id: Any, account_id: int) -> dict:
    card = one(db, "SELECT * FROM credit_cards WHERE id=%s FOR UPDATE",
        (integer(card_id, "Cartão"),))
    if not card:
        fail(400, "Cartão inválido.")
    if card["payment_account_id"] != account_id:
        fail(400, "Este cartão não pertence à conta escolhida.")
    return card


def card_limits(db, card: dict, as_of: date) -> dict:
    collateral = 0
    if card["collateral_account_id"]:
        collateral = one(db, """SELECT a.opening_cents + COALESCE(SUM(CASE WHEN t.kind='income'
            THEN t.amount_cents ELSE -t.amount_cents END),0) AS cents
            FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id AND t.date<=%s
            AND t.movement_type<>'card_purchase' WHERE a.id=%s GROUP BY a.id""",
            (as_of, card["collateral_account_id"]))["cents"]
    invoices = all_rows(db, "SELECT * FROM bills WHERE credit_card_id=%s", (card["id"],))
    paid = all_rows(db, """SELECT t.id,t.bill_id,t.amount_cents,t.date FROM transactions t
        JOIN bills b ON b.id=t.bill_id WHERE b.credit_card_id=%s ORDER BY t.date,t.id""", (card["id"],))
    used = 0
    for invoice in invoices:
        history = [item for item in paid if item["bill_id"] == invoice["id"]]
        used += debt_cents_at(invoice, history, max(as_of, invoice["due_date"])) or 0
    total = card["base_limit_cents"] + max(0, collateral)
    return {"collateral_cents": collateral, "limit_cents": total, "used_cents": used,
        "available_cents": total - used}


def card_rows(db, as_of: date) -> list[dict]:
    cards = all_rows(db, """SELECT c.*,pa.name AS payment_account_name,ca.name AS collateral_account_name,
        ca.yield_rate_id,ca.yield_multiplier_basis_points,r.name AS yield_rate_name,
        r.annual_basis_points AS yield_annual_basis_points
        FROM credit_cards c JOIN accounts pa ON pa.id=c.payment_account_id
        LEFT JOIN accounts ca ON ca.id=c.collateral_account_id
        LEFT JOIN yield_rates r ON r.id=ca.yield_rate_id ORDER BY c.id""")
    return [{**card, **card_limits(db, card, as_of)} for card in cards]


def ensure_invoice(db, card: dict, closing: date, due: date) -> int:
    month = due.replace(day=1)
    existing = one(db, "SELECT id FROM bills WHERE credit_card_id=%s AND invoice_month=%s", (card["id"], month))
    if existing:
        return existing["id"]
    category = one(db, "SELECT id,name FROM categories WHERE kind='expense' AND name='Cartão de crédito'")
    return one(db, """INSERT INTO bills(account_id,description,category,category_id,note,amount_cents,due_date,
        interest_rate_basis_points,interest_period,interest_rate_period,expense_type,payment_method,credit_card_id,
        invoice_month) VALUES(%s,%s,%s,%s,%s,1,%s,%s,'daily','monthly','eventual','pix',%s,%s) RETURNING id""",
        (card["payment_account_id"], f"Fatura {card['name']} · {due:%m/%Y}",
         category["name"] if category else "Cartão de crédito", category["id"] if category else None,
         f"Fecha em {closing:%d/%m/%Y}", due, card["late_interest_basis_points"], card["id"], month))["id"]


def refresh_invoice(db, bill_id: int):
    total = one(db, "SELECT COALESCE(SUM(amount_cents),0)::bigint AS cents FROM transactions WHERE invoice_bill_id=%s",
        (bill_id,))["cents"]
    if total:
        db.execute("UPDATE bills SET amount_cents=%s WHERE id=%s", (total, bill_id))
    elif not one(db, "SELECT id FROM transactions WHERE bill_id=%s LIMIT 1", (bill_id,)):
        db.execute("DELETE FROM bills WHERE id=%s", (bill_id,))


def create_card_purchase(db, card: dict, description: str, category: tuple, purchase_note: str,
                         total: int, when: date, installments: int, bill_id: int | None = None) -> int:
    limits = card_limits(db, card, when)
    if total > limits["available_cents"]:
        available = f"{max(0, limits['available_cents']) / 100:,.2f}".translate(str.maketrans(",.", ".,"))
        fail(400, f"Limite disponível insuficiente no cartão {card['name']} (R$ {available}).")
    category_name, category_id, subcategory_id = category
    purchase_id = one(db, """INSERT INTO card_purchases(credit_card_id,description,total_cents,installments,purchase_date)
        VALUES(%s,%s,%s,%s,%s) RETURNING id""", (card["id"], description, total, installments, when))["id"]
    for number, amount in enumerate(split_installments(total, installments), start=1):
        closing, due = invoice_dates(when, card["closing_day"], card["due_day"], number - 1)
        invoice_id = ensure_invoice(db, card, closing, due)
        db.execute("""INSERT INTO transactions(account_id,kind,description,category,category_id,subcategory_id,note,
            amount_cents,date,payment_method,movement_type,credit_card_id,card_purchase_id,invoice_bill_id,
            installment_number,bill_id) VALUES(%s,'expense',%s,%s,%s,%s,%s,%s,%s,'credit','card_purchase',%s,%s,%s,%s,%s)""",
            (card["payment_account_id"], f"{description} ({number}/{installments})" if installments > 1 else description,
             category_name, category_id, subcategory_id, purchase_note, amount, when, card["id"], purchase_id,
             invoice_id, number, bill_id))
        refresh_invoice(db, invoice_id)
    return purchase_id


def remove_card_purchase(db, purchase_id: int):
    rows = all_rows(db, "SELECT id,invoice_bill_id FROM transactions WHERE card_purchase_id=%s FOR UPDATE", (purchase_id,))
    if not rows:
        fail(404, "Compra não encontrada.")
    invoices = sorted({row["invoice_bill_id"] for row in rows})
    if one(db, "SELECT id FROM transactions WHERE bill_id = ANY(%s) LIMIT 1", (invoices,)):
        fail(409, "Esta compra está em uma fatura com pagamento. Exclua os pagamentos da fatura primeiro.")
    db.execute("DELETE FROM transactions WHERE card_purchase_id=%s", (purchase_id,))
    db.execute("DELETE FROM card_purchases WHERE id=%s", (purchase_id,))
    for invoice_id in invoices:
        refresh_invoice(db, invoice_id)


@app.get("/health")
def health(db=Depends(db_session)):
    one(db, "SELECT 1")
    return {"ok": True, "version": 3}


@app.get("/api/bootstrap")
def bootstrap(request: Request, db=Depends(db_session)):
    return {"setupRequired": not bool(one(db, "SELECT id FROM users LIMIT 1")), "user": maybe_user(request, db)}


@app.post("/api/setup", status_code=201)
def setup(value: dict, request: Request, response: Response, db=Depends(db_session)):
    check_origin(request)
    name = text(value.get("name"), "Nome", 80)
    email = text(value.get("email"), "Email", 254).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        fail(400, "Email inválido.")
    encoded = password_hash(value.get("password"))
    db.execute("LOCK TABLE users IN EXCLUSIVE MODE")
    if one(db, "SELECT id FROM users LIMIT 1"):
        fail(409, "O administrador já foi criado.")
    created = one(db, "INSERT INTO users(name,email,password_hash,role) VALUES(%s,%s,%s,'admin') RETURNING id", (name, email, encoded))
    set_session(response, db, created["id"])
    return {"ok": True}


@app.post("/api/login")
def login(value: dict, request: Request, response: Response, db=Depends(db_session)):
    check_origin(request)
    email = text(value.get("email"), "Email", 254).lower()
    key = f"{request.client.host if request.client else ''}:{email}"
    count, until = login_attempts.get(key, (0, 0))
    if until > time.time() and count >= 5:
        fail(429, "Muitas tentativas. Aguarde 15 minutos.")
    found = one(db, "SELECT * FROM users WHERE email=%s AND active=TRUE", (email,))
    if not found or not verify_password(value.get("password"), found["password_hash"]):
        login_attempts[key] = ((count if until > time.time() else 0) + 1, time.time() + 900)
        fail(401, "Email ou senha incorretos.")
    login_attempts.pop(key, None)
    set_session(response, db, found["id"])
    return {"user": {key: found[key] for key in ("id", "name", "email", "role")}}


@app.post("/api/logout")
def logout(request: Request, response: Response, db=Depends(db_session)):
    check_origin(request)
    token = request.cookies.get("session", "")
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=%s", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie("session")
    return {"ok": True}


@app.post("/api/profile/password")
def change_password(value: dict, who=Depends(mutation), db=Depends(db_session)):
    stored = one(db, "SELECT password_hash FROM users WHERE id=%s", (who["id"],))
    if not verify_password(value.get("currentPassword"), stored["password_hash"]):
        fail(400, "Senha atual incorreta.")
    db.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash(value.get("newPassword")), who["id"]))
    return {"ok": True}


@app.get("/api/state")
def state(who=Depends(user), db=Depends(db_session)):
    current = today(os.getenv("APP_TIMEZONE", "America/Araguaina"))
    materialize_fixed_bills(db, current)
    materialize_fixed_incomes(db, current)
    sync_dynamic_rates(db, current)
    accrue_passive_yields(db)
    accounts = all_rows(db, """SELECT a.*,r.name AS yield_rate_name,r.annual_basis_points AS yield_annual_basis_points,
        (SELECT cc.id FROM credit_cards cc WHERE cc.collateral_account_id=a.id) AS collateral_card_id,
        a.opening_cents + COALESCE(SUM(CASE WHEN t.date>%s THEN 0
        WHEN t.kind='income' THEN t.amount_cents ELSE -t.amount_cents END),0) AS balance_cents
        FROM accounts a LEFT JOIN yield_rates r ON r.id=a.yield_rate_id
        LEFT JOIN transactions t ON t.account_id=a.id AND t.movement_type<>'card_purchase'
        GROUP BY a.id,r.id ORDER BY a.id DESC""", (current,))
    transactions = all_rows(db, """SELECT t.*,py.transaction_id IS NOT NULL AS automatic_yield,
        a.name AS account_name,COALESCE(c.name,t.category) AS category_name,cc.name AS card_name,
        cp.installments,s.name AS subcategory_name FROM transactions t JOIN accounts a ON a.id=t.account_id
        LEFT JOIN passive_yields py ON py.transaction_id=t.id
        LEFT JOIN credit_cards cc ON cc.id=t.credit_card_id LEFT JOIN card_purchases cp ON cp.id=t.card_purchase_id
        LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN subcategories s ON s.id=t.subcategory_id
        ORDER BY t.date DESC,t.id DESC LIMIT 500""")
    for transaction in transactions:
        transaction["category"] = transaction["category_name"]
    investments = all_rows(db, "SELECT * FROM investments ORDER BY id DESC")
    for investment in investments:
        investment["current_cents"] = projected_cents(investment["principal_cents"], investment["rate_basis_points"], investment["start_date"], current, investment["period"])
        investment["future_cents"] = projected_cents(investment["principal_cents"], investment["rate_basis_points"], investment["start_date"], add_months(current, 12), investment["period"])
    bills = bill_rows(db, current)
    incomes = income_rows(db, current)
    categories = all_rows(db, "SELECT id,kind,name FROM categories ORDER BY kind,name")
    yield_rates = all_rows(db, """SELECT id,name,annual_basis_points,source_code,source_date,
        last_checked_at,sync_error FROM yield_rates ORDER BY name""")
    subcategories = all_rows(db, "SELECT id,category_id,name FROM subcategories ORDER BY name")
    month_start = current.replace(day=1)
    month_end = add_months(month_start, 1)
    month_totals_rows = all_rows(db, """SELECT kind,SUM(amount_cents)::bigint AS amount_cents FROM transactions
        WHERE date >= %s AND date <= %s AND movement_type IN ('regular','card_purchase')
        GROUP BY kind""", (month_start, current))
    month_totals = {"income_cents": 0, "expense_cents": 0}
    for item in month_totals_rows:
        month_totals[f"{item['kind']}_cents"] = item["amount_cents"]
    spending = all_rows(db, """SELECT t.category_id,COALESCE(c.name,t.category) AS category,
        t.subcategory_id,s.name AS subcategory,SUM(t.amount_cents)::bigint AS amount_cents
        FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
        LEFT JOIN subcategories s ON s.id=t.subcategory_id
        WHERE t.kind='expense' AND t.date >= %s AND t.date < %s AND t.date <= %s
        AND t.movement_type IN ('regular','card_purchase')
        GROUP BY t.category_id,COALESCE(c.name,t.category),t.subcategory_id,s.name ORDER BY amount_cents DESC""", (month_start, month_end, current))
    investment_series = [{"date": add_months(current, i), "value_cents": sum(projected_cents(x["principal_cents"], x["rate_basis_points"], x["start_date"], add_months(current, i), x["period"]) or 0 for x in investments)} for i in range(13)]
    debt_series = [{"date": add_months(current, i), "value_cents": sum(debt_cents_at(x, x["payments"], add_months(current, i)) or 0 for x in bills)} for i in range(13)]
    due_series = monthly_payables(bills, current)
    income_series = monthly_receivables(incomes, current)
    users = all_rows(db, "SELECT id,name,email,role,active FROM users ORDER BY id") if who["role"] == "admin" else []
    credit_cards = card_rows(db, current)
    card_purchases = all_rows(db, """SELECT p.*,cc.name AS card_name,
        MIN(t.category) AS category,MIN(t.category_id) AS category_id,MIN(t.subcategory_id) AS subcategory_id,
        MIN(t.note) AS note,MIN(t.bill_id) AS bill_id,
        COALESCE(SUM(t.amount_cents) FILTER (WHERE b.due_date > %s),0)::bigint AS remaining_cents
        FROM card_purchases p JOIN credit_cards cc ON cc.id=p.credit_card_id
        JOIN transactions t ON t.card_purchase_id=p.id JOIN bills b ON b.id=t.invoice_bill_id
        GROUP BY p.id,cc.name ORDER BY p.purchase_date DESC,p.id DESC LIMIT 300""", (current,))
    return jsonable_encoder({"user": who, "today": current, "accounts": accounts, "transactions": transactions,
        "credit_cards": credit_cards, "card_purchases": card_purchases,
        "investments": investments, "bills": bills, "incomes": incomes, "income_series": income_series,
        "categories": categories, "subcategories": subcategories,
        "yield_rates": yield_rates,
        "monthly_spending": spending, "monthly_totals": month_totals, "investment_series": investment_series,
        "debt_series": debt_series, "due_series": due_series, "users": users})


def account_forecast(db, current: date, account_id: int | None) -> list[dict]:
    # Collateral accounts back a card limit and are not money available to spend.
    balance = one(db, """SELECT COALESCE(SUM(a.opening_cents + COALESCE((SELECT SUM(CASE WHEN t.kind='income'
        THEN t.amount_cents ELSE -t.amount_cents END) FROM transactions t WHERE t.account_id=a.id
        AND t.date<=%s AND t.movement_type<>'card_purchase'),0)),0)::bigint AS cents
        FROM accounts a WHERE (%s::bigint IS NULL OR a.id=%s)
        AND NOT EXISTS (SELECT 1 FROM credit_cards cc WHERE cc.collateral_account_id=a.id)""",
        (current, account_id, account_id))["cents"]
    bills = [item for item in bill_rows(db, current) if account_id is None or item["account_id"] == account_id]
    incomes = [item for item in income_rows(db, current) if account_id is None or item["account_id"] == account_id]
    return forecast_series(balance, bills, incomes, current)


@app.get("/api/forecast")
def forecast(accountId: int | None = None, who=Depends(user), db=Depends(db_session)):
    current = today(os.getenv("APP_TIMEZONE", "America/Araguaina"))
    materialize_fixed_bills(db, current)
    materialize_fixed_incomes(db, current)
    if accountId is not None and not one(db, "SELECT id FROM accounts WHERE id=%s", (accountId,)):
        fail(404, "Conta não encontrada.")
    return jsonable_encoder({"account_id": accountId, "series": [{key: value for key, value in item.items()
        if not key.endswith("items")} for item in account_forecast(db, current, accountId)]})


@app.get("/api/dashboard")
def dashboard(month: str | None = None, accountId: int | None = None,
              who=Depends(user), db=Depends(db_session)):
    current = today(os.getenv("APP_TIMEZONE", "America/Araguaina"))
    materialize_fixed_bills(db, current)
    materialize_fixed_incomes(db, current)
    sync_dynamic_rates(db, current)
    accrue_passive_yields(db)
    start, end = month_bounds(month, current)
    cutoff = min(end - timedelta(days=1), current)
    if accountId is not None and not one(db, "SELECT id FROM accounts WHERE id=%s", (accountId,)):
        fail(404, "Conta não encontrada.")
    if start > current.replace(day=1):
        offset = (start.year - current.year) * 12 + start.month - current.month
        if offset > FORECAST_MONTHS:
            fail(400, f"A previsão alcança até {FORECAST_MONTHS} meses à frente.")
        series = account_forecast(db, current, accountId)
        point = series[offset]
        bills = {item["id"]: item for item in bill_rows(db, current)}
        incomes = {item["id"]: item for item in income_rows(db, current)}
        point["items"] = [{**item, "description": bills[item["id"]]["description"],
            "account_name": bills[item["id"]]["account_name"], "due_date": bills[item["id"]]["due_date"],
            "amount_cents": bills[item["id"]]["amount_cents"], "card_name": bills[item["id"]]["card_name"],
            "category": bills[item["id"]]["category"], "expense_type": bills[item["id"]]["expense_type"]}
            for item in point["items"]]
        point["income_items"] = [{**item, "description": incomes[item["id"]]["description"],
            "account_name": incomes[item["id"]]["account_name"], "due_date": incomes[item["id"]]["due_date"]}
            for item in point["income_items"]]
        return jsonable_encoder({"mode": "forecast", "month": start, "account_id": accountId, "forecast": point,
            "series": [{key: value for key, value in item.items() if not key.endswith("items")} for item in series]})
    totals = {"income_cents": 0, "expense_cents": 0, "yield_cents": 0, "transaction_count": 0}
    summary = one(db, """SELECT COALESCE(SUM(t.amount_cents) FILTER (WHERE t.kind='income'),0)::bigint AS income_cents,
        COALESCE(SUM(t.amount_cents) FILTER (WHERE t.kind='expense'),0)::bigint AS expense_cents,
        COALESCE(SUM(t.amount_cents) FILTER (WHERE py.transaction_id IS NOT NULL),0)::bigint AS yield_cents,
        COUNT(DISTINCT COALESCE(-t.card_purchase_id,t.id))::bigint AS transaction_count FROM transactions t
        LEFT JOIN passive_yields py ON py.transaction_id=t.id
        WHERE t.date >= %s AND t.date < %s AND t.date <= %s
        AND t.movement_type IN ('regular','card_purchase')
        AND (%s::bigint IS NULL OR t.account_id=%s)""", (start, end, current, accountId, accountId))
    totals.update(summary)
    categories = all_rows(db, """SELECT t.kind,t.category_id,COALESCE(c.name,t.category) AS category,
        t.subcategory_id,s.name AS subcategory,SUM(t.amount_cents)::bigint AS amount_cents
        FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
        LEFT JOIN subcategories s ON s.id=t.subcategory_id
        WHERE t.date >= %s AND t.date < %s AND t.date <= %s
        AND t.movement_type IN ('regular','card_purchase')
        AND (%s::bigint IS NULL OR t.account_id=%s)
        GROUP BY t.kind,t.category_id,COALESCE(c.name,t.category),t.subcategory_id,s.name
        ORDER BY amount_cents DESC""", (start, end, current, accountId, accountId))
    days = all_rows(db, """SELECT date,kind,SUM(amount_cents)::bigint AS amount_cents FROM transactions
        WHERE date >= %s AND date < %s AND date <= %s AND (%s::bigint IS NULL OR account_id=%s)
        AND movement_type IN ('regular','card_purchase')
        GROUP BY date,kind ORDER BY date""", (start, end, current, accountId, accountId))
    recent = all_rows(db, """SELECT t.id,t.account_id,t.kind,t.description,t.amount_cents,t.date,
        t.payment_method,t.movement_type,COALESCE(c.name,t.category) AS category,a.name AS account_name,
        py.transaction_id IS NOT NULL AS automatic_yield FROM transactions t
        JOIN accounts a ON a.id=t.account_id LEFT JOIN categories c ON c.id=t.category_id
        LEFT JOIN passive_yields py ON py.transaction_id=t.id
        WHERE t.date >= %s AND t.date < %s AND t.date <= %s
        AND t.movement_type IN ('regular','card_purchase')
        AND (%s::bigint IS NULL OR t.account_id=%s)
        ORDER BY t.date DESC,t.id DESC LIMIT 12""", (start, end, current, accountId, accountId))
    accounts = all_rows(db, """SELECT a.id,a.name,a.type,
        a.opening_cents + COALESCE(SUM(CASE WHEN t.kind='income' THEN t.amount_cents
        ELSE -t.amount_cents END),0) AS balance_cents
        FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id AND t.date <= %s
        AND t.movement_type<>'card_purchase'
        WHERE (%s::bigint IS NULL OR a.id=%s) GROUP BY a.id ORDER BY balance_cents DESC""",
        (cutoff, accountId, accountId))
    bills = [item for item in bill_rows(db, current) if start <= item["due_date"] < end
        and (accountId is None or item["account_id"] == accountId)]
    incomes = [item for item in income_rows(db, current) if start <= item["due_date"] < end
        and (accountId is None or item["account_id"] == accountId)]
    return jsonable_encoder({"mode": "actual", "month": start, "account_id": accountId, "totals": totals,
        "categories": categories, "days": days, "recent": recent, "accounts": accounts,
        "planned": {"bills_cents": sum(item["current_cents"] or 0 for item in bills if item["status"] != "paid"),
            "income_cents": sum(item["remaining_cents"] for item in incomes),
            "bills_count": sum(item["status"] != "paid" for item in bills),
            "income_count": sum(item["remaining_cents"] > 0 for item in incomes)}})


@app.get("/api/accounts/{account_id}/details")
def account_details(account_id: int, month: str | None = None, offset: int = 0,
                    who=Depends(user), db=Depends(db_session)):
    if offset < 0 or offset > 100000:
        fail(400, "Página inválida.")
    current = today(os.getenv("APP_TIMEZONE", "America/Araguaina"))
    sync_dynamic_rates(db, current)
    accrue_passive_yields(db)
    account = one(db, """SELECT a.*,r.name AS yield_rate_name,r.annual_basis_points,
        r.source_code,r.source_date,
        a.opening_cents + COALESCE(SUM(CASE WHEN t.kind='income' THEN t.amount_cents
        ELSE -t.amount_cents END),0) AS balance_cents
        FROM accounts a LEFT JOIN yield_rates r ON r.id=a.yield_rate_id
        LEFT JOIN transactions t ON t.account_id=a.id AND t.date<=%s AND t.movement_type<>'card_purchase'
        WHERE a.id=%s GROUP BY a.id,r.id""", (current, account_id))
    if not account:
        fail(404, "Conta não encontrada.")
    start, end = month_bounds(month, current) if month else (None, None)
    history = all_rows(db, """WITH ordered AS (
        SELECT t.*,a.opening_cents + SUM(CASE WHEN t.kind='income' THEN t.amount_cents
            ELSE -t.amount_cents END) OVER (ORDER BY t.date,t.id) AS balance_after_cents,
            py.transaction_id IS NOT NULL AS automatic_yield
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        LEFT JOIN passive_yields py ON py.transaction_id=t.id
        WHERE t.account_id=%s AND t.date<=%s AND t.movement_type<>'card_purchase')
        SELECT o.*,COALESCE(c.name,o.category) AS category_name,s.name AS subcategory_name
        FROM ordered o LEFT JOIN categories c ON c.id=o.category_id
        LEFT JOIN subcategories s ON s.id=o.subcategory_id
        WHERE (%s::date IS NULL OR o.date >= %s) AND (%s::date IS NULL OR o.date < %s)
        ORDER BY o.date DESC,o.id DESC LIMIT 50 OFFSET %s""",
        (account_id, current, start, start, end, end, offset))
    count = one(db, """SELECT COUNT(*)::bigint AS count FROM transactions WHERE account_id=%s AND date<=%s
        AND movement_type<>'card_purchase' AND (%s::date IS NULL OR date >= %s) AND (%s::date IS NULL OR date < %s)""",
        (account_id, current, start, start, end, end))["count"]
    summary = one(db, """SELECT COALESCE(SUM(t.amount_cents) FILTER (WHERE t.kind='income'),0)::bigint AS income_cents,
        COALESCE(SUM(t.amount_cents) FILTER (WHERE t.kind='expense'),0)::bigint AS expense_cents,
        COALESCE(SUM(t.amount_cents) FILTER (WHERE py.transaction_id IS NOT NULL),0)::bigint AS yield_cents
        FROM transactions t LEFT JOIN passive_yields py ON py.transaction_id=t.id
        WHERE t.account_id=%s AND t.date<=%s AND t.movement_type<>'card_purchase'""", (account_id, current))
    projection = fixed_rate_projection(account["balance_cents"], account["annual_basis_points"],
        account["yield_multiplier_basis_points"], current)
    cards = [card for card in card_rows(db, current) if card["payment_account_id"] == account_id]
    card_purchases = all_rows(db, """SELECT p.*,cc.name AS card_name FROM card_purchases p
        JOIN credit_cards cc ON cc.id=p.credit_card_id WHERE cc.payment_account_id=%s
        AND (%s::date IS NULL OR p.purchase_date >= %s) AND (%s::date IS NULL OR p.purchase_date < %s)
        ORDER BY p.purchase_date DESC,p.id DESC LIMIT 50""", (account_id, start, start, end, end))
    return jsonable_encoder({"account": account, "history": history, "history_count": count,
        "cards": cards, "card_purchases": card_purchases,
        "summary": summary, "projection": projection, "today": current, "month": start, "offset": offset})


@app.get("/api/cron/passive-yields")
def scheduled_passive_yields(request: Request, db=Depends(db_session)):
    secret = os.getenv("CRON_SECRET", "")
    authorization = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(authorization, f"Bearer {secret}"):
        fail(401, "Cron não autorizado.")
    sync = sync_dynamic_rates(db, yield_clock().date(), force=True)
    return {"created": accrue_passive_yields(db), "rates": sync}


@app.post("/api/categories", status_code=201)
def create_category(value: dict, who=Depends(mutation), db=Depends(db_session)):
    result = one(db, "INSERT INTO categories(kind,name) VALUES(%s,%s) RETURNING id", (kind(value.get("kind")), text(value.get("name"), "Nome da categoria", 60)))
    return {"id": result["id"]}


@app.patch("/api/categories/{category_id}")
def rename_category(category_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "UPDATE categories SET name=%s WHERE id=%s RETURNING id", (text(value.get("name"), "Nome da categoria", 60), category_id)):
        fail(404, "Categoria não encontrada.")
    return {"ok": True}


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM bills WHERE category_id=%s LIMIT 1", (category_id,)) or one(db, "SELECT id FROM planned_incomes WHERE category_id=%s LIMIT 1", (category_id,)) or one(db, "SELECT id FROM transactions WHERE category_id=%s LIMIT 1", (category_id,)) or one(db, "SELECT id FROM subcategories WHERE category_id=%s LIMIT 1", (category_id,)):
        fail(409, "Categoria em uso. Reclassifique os registros e exclua as subcategorias primeiro.")
    if not one(db, "DELETE FROM categories WHERE id=%s RETURNING id", (category_id,)):
        fail(404, "Categoria não encontrada.")
    return {"ok": True}


@app.post("/api/subcategories", status_code=201)
def create_subcategory(value: dict, who=Depends(mutation), db=Depends(db_session)):
    category_id = integer(value.get("categoryId"), "Categoria")
    if not one(db, "SELECT id FROM categories WHERE id=%s", (category_id,)):
        fail(400, "Categoria inválida.")
    result = one(db, "INSERT INTO subcategories(category_id,name) VALUES(%s,%s) RETURNING id", (category_id, text(value.get("name"), "Nome da subcategoria", 60)))
    return {"id": result["id"]}


@app.patch("/api/subcategories/{subcategory_id}")
def rename_subcategory(subcategory_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "UPDATE subcategories SET name=%s WHERE id=%s RETURNING id", (text(value.get("name"), "Nome da subcategoria", 60), subcategory_id)):
        fail(404, "Subcategoria não encontrada.")
    return {"ok": True}


@app.delete("/api/subcategories/{subcategory_id}")
def delete_subcategory(subcategory_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM bills WHERE subcategory_id=%s LIMIT 1", (subcategory_id,)) or one(db, "SELECT id FROM planned_incomes WHERE subcategory_id=%s LIMIT 1", (subcategory_id,)) or one(db, "SELECT id FROM transactions WHERE subcategory_id=%s LIMIT 1", (subcategory_id,)):
        fail(409, "Subcategoria em uso. Reclassifique os registros primeiro.")
    if not one(db, "DELETE FROM subcategories WHERE id=%s RETURNING id", (subcategory_id,)):
        fail(404, "Subcategoria não encontrada.")
    return {"ok": True}


@app.post("/api/accounts", status_code=201)
def create_account(value: dict, who=Depends(mutation), db=Depends(db_session)):
    rate_id, multiplier = account_yield_choice(db, value)
    result = one(db, """INSERT INTO accounts(name,type,opening_cents,note,yield_rate_id,
        yield_multiplier_basis_points,yield_start_date) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (text(value.get("name"), "Nome da conta"), text(value.get("type"), "Tipo", 60),
         integer(value.get("openingCents"), "Saldo inicial", 0), note(value.get("note")),
         rate_id, multiplier, yield_start(yield_clock()) if rate_id else None))
    return {"id": result["id"]}


@app.patch("/api/accounts/{account_id}")
def edit_account(account_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    sync_dynamic_rates(db, yield_clock().date())
    accrue_passive_yields(db)
    existing = one(db, "SELECT * FROM accounts WHERE id=%s FOR UPDATE", (account_id,))
    if not existing:
        fail(404, "Conta não encontrada.")
    rate_id, multiplier = account_yield_choice(db, value)
    changed = rate_id != existing["yield_rate_id"] or multiplier != existing["yield_multiplier_basis_points"]
    start = yield_start(yield_clock()) if rate_id and changed else existing["yield_start_date"] if rate_id else None
    db.execute("""UPDATE accounts SET name=%s,type=%s,opening_cents=%s,note=%s,yield_rate_id=%s,
        yield_multiplier_basis_points=%s,yield_start_date=%s WHERE id=%s""",
        (text(value.get("name"), "Nome da conta"), text(value.get("type"), "Tipo", 60),
         integer(value.get("openingCents"), "Saldo inicial", 0), note(value.get("note")),
         rate_id, multiplier, start, account_id))
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE account_id=%s LIMIT 1", (account_id,)) or one(db, "SELECT id FROM bills WHERE account_id=%s LIMIT 1", (account_id,)) or one(db, "SELECT id FROM planned_incomes WHERE account_id=%s LIMIT 1", (account_id,)) or one(db, "SELECT 1 FROM passive_yields WHERE account_id=%s LIMIT 1", (account_id,)):
        fail(409, "Esta conta possui despesas, entradas ou lançamentos.")
    if one(db, "SELECT id FROM credit_cards WHERE payment_account_id=%s OR collateral_account_id=%s LIMIT 1",
           (account_id, account_id)):
        fail(409, "Esta conta está ligada a um cartão de crédito.")
    if not one(db, "DELETE FROM accounts WHERE id=%s RETURNING id", (account_id,)):
        fail(404, "Conta não encontrada.")
    return {"ok": True}


def account_yield_choice(db, value: dict):
    rate_id = value.get("yieldRateId") or None
    multiplier = integer(value.get("yieldMultiplierBasisPoints", 10000), "Percentual da taxa", 0, 100000)
    if rate_id is not None:
        rate_id = integer(rate_id, "Taxa de referência")
        if not one(db, "SELECT id FROM yield_rates WHERE id=%s", (rate_id,)):
            fail(400, "Taxa de referência inválida.")
    return rate_id, multiplier


@app.post("/api/yield-rates", status_code=201)
def create_yield_rate(value: dict, who=Depends(mutation), db=Depends(db_session)):
    source_code = value.get("sourceCode") or None
    if source_code is not None and source_code not in BCB_SERIES:
        fail(400, "Indicador inválido.")
    if source_code:
        try:
            records = bcb_observations(source_code, yield_clock().date())
        except (OSError, ValueError, ArithmeticError, KeyError, TypeError) as error:
            fail(503, f"Não foi possível consultar o Banco Central: {error}")
        save_bcb_observations(db, source_code, records)
        source_date, points = records[-1]
    else:
        source_date = None
        points = integer(value.get("annualBasisPoints"), "Taxa anual", 0, 10000)
    result = one(db, """INSERT INTO yield_rates(name,annual_basis_points,source_code,source_date,last_checked_at)
        VALUES(%s,%s,%s,%s,%s) RETURNING id""",
        (text(value.get("name"), "Nome da taxa", 80), points, source_code, source_date,
         yield_clock() if source_code else None))
    return {"id": result["id"]}


@app.post("/api/yield-rates/sync")
def sync_yield_rates(who=Depends(mutation), db=Depends(db_session)):
    result = sync_dynamic_rates(db, yield_clock().date(), force=True)
    return result


@app.patch("/api/yield-rates/{rate_id}")
def edit_yield_rate(rate_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    sync_dynamic_rates(db, yield_clock().date())
    accrue_passive_yields(db)
    source_code = value.get("sourceCode") or None
    if source_code is not None and source_code not in BCB_SERIES:
        fail(400, "Indicador inválido.")
    if source_code:
        try:
            records = bcb_observations(source_code, yield_clock().date())
        except (OSError, ValueError, ArithmeticError, KeyError, TypeError) as error:
            fail(503, f"Não foi possível consultar o Banco Central: {error}")
        save_bcb_observations(db, source_code, records)
        source_date, points = records[-1]
    else:
        source_date = None
        points = integer(value.get("annualBasisPoints"), "Taxa anual", 0, 10000)
    if not one(db, """UPDATE yield_rates SET name=%s,annual_basis_points=%s,source_code=%s,
        source_date=%s,last_checked_at=%s,sync_error='' WHERE id=%s RETURNING id""",
        (text(value.get("name"), "Nome da taxa", 80), points, source_code, source_date,
         yield_clock() if source_code else None, rate_id)):
        fail(404, "Taxa não encontrada.")
    return {"ok": True}


@app.delete("/api/yield-rates/{rate_id}")
def delete_yield_rate(rate_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM accounts WHERE yield_rate_id=%s LIMIT 1", (rate_id,)):
        fail(409, "Esta taxa está em uso por uma conta.")
    if not one(db, "DELETE FROM yield_rates WHERE id=%s RETURNING id", (rate_id,)):
        fail(404, "Taxa não encontrada.")
    return {"ok": True}


def bill_values(db, value: dict):
    account_id = integer(value.get("accountId"), "Conta")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    category_id, subcategory_id, category_name = category_choice(db, value, "expense")
    period = value.get("interestPeriod")
    expense_type = value.get("expenseType", "eventual")
    if expense_type not in ("eventual", "fixed"):
        fail(400, "Tipo de despesa inválido.")
    return (account_id, text(value.get("description"), "Nome da despesa"), category_name,
        category_id, subcategory_id, note(value.get("note")), integer(value.get("amountCents"), "Valor"),
        parsed_date(value.get("dueDate")),
        interest(value.get("interestRateBasisPoints"), value.get("interestRatePeriod") or period),
        interest_period(period), expense_type)


def income_values(db, value: dict):
    account_id = integer(value.get("accountId"), "Conta")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    category_id, subcategory_id, category_name = category_choice(db, value, "income")
    income_type = value.get("incomeType", "eventual")
    if income_type not in ("eventual", "fixed"):
        fail(400, "Tipo de entrada inválido.")
    return (account_id, text(value.get("description"), "Nome da entrada"), category_name,
        category_id, subcategory_id, note(value.get("note")), integer(value.get("amountCents"), "Valor"),
        parsed_date(value.get("dueDate")), income_type)


@app.post("/api/incomes", status_code=201)
def create_income(value: dict, who=Depends(mutation), db=Depends(db_session)):
    fields = income_values(db, value)
    result = one(db, """INSERT INTO planned_incomes(account_id,description,category,category_id,
        subcategory_id,note,amount_cents,due_date,income_type,recurrence_active)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (*fields, fields[-1] == "fixed"))
    return {"id": result["id"]}


@app.patch("/api/incomes/{income_id}")
def edit_income(income_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    existing = one(db, "SELECT * FROM planned_incomes WHERE id=%s AND archived=FALSE FOR UPDATE", (income_id,))
    if not existing:
        fail(404, "Entrada não encontrada.")
    if one(db, "SELECT id FROM transactions WHERE planned_income_id=%s LIMIT 1", (income_id,)):
        fail(409, "Uma entrada com recebimentos não pode ser editada.")
    fields = income_values(db, value)
    if existing["recurrence_origin_id"] and fields[-1] != "fixed":
        fail(400, "Uma parcela mensal permanece fixa.")
    db.execute("""UPDATE planned_incomes SET account_id=%s,description=%s,category=%s,category_id=%s,
        subcategory_id=%s,note=%s,amount_cents=%s,due_date=%s,income_type=%s,recurrence_active=%s
        WHERE id=%s""", (*fields, fields[-1] == "fixed" and existing["recurrence_origin_id"] is None
        and (existing["income_type"] != "fixed" or existing["recurrence_active"]), income_id))
    if existing["recurrence_origin_id"] is None and existing["income_type"] == "fixed":
        if fields[-1] == "eventual":
            db.execute("""UPDATE planned_incomes SET archived=TRUE WHERE recurrence_origin_id=%s
                AND NOT EXISTS (SELECT 1 FROM transactions WHERE planned_income_id=planned_incomes.id)""", (income_id,))
        else:
            children = all_rows(db, """SELECT id,recurrence_month FROM planned_incomes
                WHERE recurrence_origin_id=%s AND archived=FALSE
                AND NOT EXISTS (SELECT 1 FROM transactions WHERE planned_income_id=planned_incomes.id)""", (income_id,))
            for child in children:
                offset = (child["recurrence_month"].year - fields[7].year) * 12 + child["recurrence_month"].month - fields[7].month
                if offset < 1:
                    db.execute("UPDATE planned_incomes SET archived=TRUE WHERE id=%s", (child["id"],))
                else:
                    db.execute("""UPDATE planned_incomes SET account_id=%s,description=%s,category=%s,
                        category_id=%s,subcategory_id=%s,note=%s,amount_cents=%s,due_date=%s
                        WHERE id=%s""", (*fields[:7], add_months(fields[7], offset), child["id"]))
    return {"ok": True}


@app.delete("/api/incomes/{income_id}")
def delete_income(income_id: int, who=Depends(mutation), db=Depends(db_session)):
    existing = one(db, "SELECT * FROM planned_incomes WHERE id=%s AND archived=FALSE FOR UPDATE", (income_id,))
    if not existing:
        fail(404, "Entrada não encontrada.")
    if one(db, "SELECT id FROM transactions WHERE planned_income_id=%s LIMIT 1", (income_id,)):
        fail(409, "Uma entrada com recebimentos não pode ser excluída.")
    if existing["income_type"] == "fixed":
        db.execute("UPDATE planned_incomes SET archived=TRUE,recurrence_active=FALSE WHERE id=%s", (income_id,))
        if existing["recurrence_origin_id"] is None:
            db.execute("""UPDATE planned_incomes SET archived=TRUE WHERE recurrence_origin_id=%s
                AND NOT EXISTS (SELECT 1 FROM transactions WHERE planned_income_id=planned_incomes.id)""", (income_id,))
    else:
        db.execute("DELETE FROM planned_incomes WHERE id=%s", (income_id,))
    return {"ok": True}


@app.patch("/api/incomes/{income_id}/stop-recurrence")
def stop_income_recurrence(income_id: int, who=Depends(mutation), db=Depends(db_session)):
    income = one(db, "SELECT * FROM planned_incomes WHERE id=%s AND archived=FALSE", (income_id,))
    if not income:
        fail(404, "Entrada não encontrada.")
    if income["income_type"] != "fixed":
        fail(400, "Esta entrada não é fixa.")
    origin_id = income["recurrence_origin_id"] or income_id
    db.execute("UPDATE planned_incomes SET recurrence_active=FALSE WHERE id=%s", (origin_id,))
    db.execute("""UPDATE planned_incomes SET archived=TRUE WHERE recurrence_origin_id=%s AND due_date >= %s
        AND NOT EXISTS (SELECT 1 FROM transactions WHERE planned_income_id=planned_incomes.id)""",
        (origin_id, today(os.getenv("APP_TIMEZONE", "America/Araguaina"))))
    return {"ok": True}


@app.post("/api/incomes/{income_id}/receipts", status_code=201)
def receive_income(income_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    when = parsed_date(value.get("date"))
    if when > today(os.getenv("APP_TIMEZONE", "America/Araguaina")):
        fail(400, "Registre o recebimento na data em que ele acontecer.")
    amount = integer(value.get("amountCents"), "Valor recebido")
    account_id = integer(value.get("accountId"), "Conta")
    income = one(db, "SELECT * FROM planned_incomes WHERE id=%s AND archived=FALSE FOR UPDATE", (income_id,))
    if not income:
        fail(404, "Entrada não encontrada.")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    received = one(db, "SELECT COALESCE(SUM(amount_cents),0) AS cents FROM transactions WHERE planned_income_id=%s", (income_id,))["cents"]
    if amount > income["amount_cents"] - received:
        fail(400, "Recebimento acima do valor restante.")
    result = one(db, """INSERT INTO transactions(account_id,planned_income_id,kind,description,category,
        category_id,subcategory_id,note,amount_cents,date)
        VALUES(%s,%s,'income',%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (account_id, income_id, f"Recebimento: {income['description']}", income["category"],
         income["category_id"], income["subcategory_id"], note(value.get("note")), amount, when))
    return {"id": result["id"]}


@app.delete("/api/incomes/{income_id}/receipts/{receipt_id}")
def delete_income_receipt(income_id: int, receipt_id: int, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "DELETE FROM transactions WHERE planned_income_id=%s AND id=%s RETURNING id", (income_id, receipt_id)):
        fail(404, "Recebimento não encontrado.")
    return {"ok": True}


@app.post("/api/bills", status_code=201)
def create_bill(value: dict, who=Depends(mutation), db=Depends(db_session)):
    fields = bill_values(db, value)
    extras = bill_extras(value)
    result = one(db, """INSERT INTO bills(account_id,description,category,category_id,subcategory_id,note,
        amount_cents,due_date,interest_rate_basis_points,interest_period,expense_type,recurrence_active,payment_method,
        interest_rate_period,fine_basis_points,fine_after)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (*fields, fields[-1] == "fixed", *extras))
    return {"id": result["id"]}


@app.patch("/api/bills/{bill_id}")
def edit_bill(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE bill_id=%s LIMIT 1", (bill_id,)):
        fail(409, "Uma despesa com pagamentos não pode ser editada.")
    existing = one(db, "SELECT * FROM bills WHERE id=%s AND archived=FALSE FOR UPDATE", (bill_id,))
    if not existing:
        fail(404, "Despesa não encontrada.")
    if existing["credit_card_id"]:
        fail(409, "A fatura é gerenciada pelo cartão.")
    fields = bill_values(db, value)
    extras = bill_extras(value)
    if existing["recurrence_origin_id"] and fields[-1] != "fixed":
        fail(400, "Uma parcela mensal permanece fixa. Exclua esta parcela se não for necessária.")
    if not one(db, """UPDATE bills SET account_id=%s,description=%s,category=%s,category_id=%s,
        subcategory_id=%s,note=%s,amount_cents=%s,due_date=%s,interest_rate_basis_points=%s,
        interest_period=%s,expense_type=%s,recurrence_active=%s,payment_method=%s,interest_rate_period=%s,
        fine_basis_points=%s,fine_after=%s WHERE id=%s RETURNING id""",
        (*fields, fields[-1] == "fixed" and existing["recurrence_origin_id"] is None
         and (existing["expense_type"] != "fixed" or existing["recurrence_active"]), *extras, bill_id)):
        fail(404, "Despesa não encontrada.")
    if existing["recurrence_origin_id"] is None and existing["expense_type"] == "fixed" and fields[-1] == "eventual":
        db.execute("""UPDATE bills SET archived=TRUE WHERE recurrence_origin_id=%s AND due_date >= %s
            AND NOT EXISTS (SELECT 1 FROM transactions WHERE bill_id=bills.id)""",
            (bill_id, today(os.getenv("APP_TIMEZONE", "America/Araguaina"))))
    elif existing["recurrence_origin_id"] is None and fields[-1] == "fixed":
        # Existing unpaid months follow changes to the template. Paid months keep their history.
        children = all_rows(db, """SELECT id,recurrence_month FROM bills WHERE recurrence_origin_id=%s AND archived=FALSE
            AND recurrence_month >= %s AND NOT EXISTS (SELECT 1 FROM transactions WHERE bill_id=bills.id)""",
            (bill_id, today(os.getenv("APP_TIMEZONE", "America/Araguaina")).replace(day=1)))
        for child in children:
            offset = (child["recurrence_month"].year - fields[7].year) * 12 + child["recurrence_month"].month - fields[7].month
            if offset < 1:
                db.execute("UPDATE bills SET archived=TRUE WHERE id=%s", (child["id"],))
                continue
            db.execute("""UPDATE bills SET account_id=%s,description=%s,category=%s,category_id=%s,
                subcategory_id=%s,note=%s,amount_cents=%s,due_date=%s,interest_rate_basis_points=%s,
                interest_period=%s,payment_method=%s,interest_rate_period=%s,fine_basis_points=%s,fine_after=%s,
                archived=FALSE WHERE id=%s""", (*fields[:7], add_months(fields[7], offset), *fields[8:10], *extras, child["id"]))
    return {"ok": True}


@app.delete("/api/bills/{bill_id}")
def delete_bill(bill_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE bill_id=%s LIMIT 1", (bill_id,)):
        fail(409, "Uma despesa com pagamentos não pode ser excluída.")
    existing = one(db, "SELECT * FROM bills WHERE id=%s AND archived=FALSE", (bill_id,))
    if not existing:
        fail(404, "Despesa não encontrada.")
    if existing["credit_card_id"]:
        fail(409, "A fatura é gerenciada pelo cartão.")
    if existing["expense_type"] == "fixed":
        db.execute("UPDATE bills SET archived=TRUE,recurrence_active=FALSE WHERE id=%s", (bill_id,))
        if existing["recurrence_origin_id"] is None:
            db.execute("""UPDATE bills SET archived=TRUE WHERE recurrence_origin_id=%s
                AND NOT EXISTS (SELECT 1 FROM transactions WHERE bill_id=bills.id)""", (bill_id,))
    else:
        db.execute("DELETE FROM bills WHERE id=%s", (bill_id,))
    return {"ok": True}


@app.patch("/api/bills/{bill_id}/stop-recurrence")
def stop_bill_recurrence(bill_id: int, who=Depends(mutation), db=Depends(db_session)):
    bill = one(db, "SELECT id,expense_type,recurrence_origin_id FROM bills WHERE id=%s AND archived=FALSE", (bill_id,))
    if not bill:
        fail(404, "Despesa não encontrada.")
    if bill["expense_type"] != "fixed":
        fail(400, "Esta despesa não é fixa.")
    origin_id = bill["recurrence_origin_id"] or bill_id
    db.execute("UPDATE bills SET recurrence_active=FALSE WHERE id=%s", (origin_id,))
    db.execute("""UPDATE bills SET archived=TRUE WHERE recurrence_origin_id=%s AND due_date >= %s
        AND NOT EXISTS (SELECT 1 FROM transactions WHERE bill_id=bills.id)""",
        (origin_id, today(os.getenv("APP_TIMEZONE", "America/Araguaina"))))
    return {"ok": True}


@app.patch("/api/bills/{bill_id}/note")
def edit_bill_note(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "UPDATE bills SET note=%s WHERE id=%s RETURNING id", (note(value.get("note")), bill_id)):
        fail(404, "Despesa não encontrada.")
    return {"ok": True}


@app.patch("/api/bills/{bill_id}/classification")
def classify_bill(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM bills WHERE id=%s AND credit_card_id IS NOT NULL", (bill_id,)):
        fail(409, "A fatura é gerenciada pelo cartão.")
    category_id, subcategory_id, category_name = category_choice(db, value, "expense")
    if not one(db, "UPDATE bills SET category=%s,category_id=%s,subcategory_id=%s WHERE id=%s RETURNING id",
        (category_name, category_id, subcategory_id, bill_id)):
        fail(404, "Despesa não encontrada.")
    db.execute("UPDATE transactions SET category=%s,category_id=%s,subcategory_id=%s WHERE bill_id=%s",
        (category_name, category_id, subcategory_id, bill_id))
    return {"ok": True}


@app.patch("/api/bills/{bill_id}/interest")
def edit_bill_interest(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    period = interest_period(value.get("interestPeriod"))
    _, rate_period, fine, fine_after = bill_extras(value)
    rate = interest(value.get("interestRateBasisPoints"), rate_period)
    bill = next((b for b in bill_rows(db, today(os.getenv("APP_TIMEZONE", "America/Araguaina"))) if b["id"] == bill_id), None)
    if not bill:
        fail(404, "Despesa não encontrada.")
    if bill["status"] == "paid":
        fail(409, "Uma despesa quitada não pode ter juros alterados.")
    db.execute("""UPDATE bills SET interest_rate_basis_points=%s,interest_period=%s,interest_rate_period=%s,
        fine_basis_points=%s,fine_after=%s WHERE id=%s""", (rate, period, rate_period, fine, fine_after, bill_id))
    return {"ok": True}


@app.post("/api/bills/{bill_id}/payments", status_code=201)
def pay_bill(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    when = parsed_date(value.get("date"))
    if when > today(os.getenv("APP_TIMEZONE", "America/Araguaina")):
        fail(400, "Registre o pagamento na data em que ele acontecer.")
    amount = integer(value.get("amountCents"), "Valor do pagamento")
    account_id = integer(value.get("accountId"), "Conta")
    method = payment_method(value.get("paymentMethod", "pix"))
    bill = one(db, "SELECT * FROM bills WHERE id=%s FOR UPDATE", (bill_id,))
    if not bill:
        fail(404, "Despesa não encontrada.")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    if bill["credit_card_id"] and method == "credit":
        fail(400, "Pague a fatura com Pix ou débito.")
    payments = all_rows(db, "SELECT id,amount_cents,date FROM transactions WHERE bill_id=%s ORDER BY date,id", (bill_id,))
    if payments and when < payments[-1]["date"]:
        fail(400, "A data deve ser igual ou posterior ao último pagamento.")
    outstanding = debt_cents_at(bill, payments, when)
    if outstanding is None:
        fail(400, "A projeção excedeu o limite de cálculo.")
    if outstanding == 0:
        fail(409, "Esta despesa já está paga.")
    if amount > outstanding:
        fail(400, "Pagamento acima do saldo devido.")
    if method == "credit":
        card = card_for_account(db, value.get("creditCardId"), account_id)
        installments = integer(value.get("installments", 1), "Parcelas", 1, 48)
        purchase_id = create_card_purchase(db, card, f"Pagamento: {bill['description']}",
            (bill["category"], bill["category_id"], bill["subcategory_id"]), note(value.get("note")),
            amount, when, installments, bill_id)
        return {"id": purchase_id}
    result = one(db, """INSERT INTO transactions(account_id,bill_id,kind,description,category,category_id,
        subcategory_id,note,amount_cents,date,payment_method,movement_type)
        VALUES(%s,%s,'expense',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (account_id, bill_id, f"Pagamento: {bill['description']}", bill["category"], bill["category_id"],
         bill["subcategory_id"], note(value.get("note")), amount, when, method,
         "invoice_payment" if bill["credit_card_id"] else "regular"))
    return {"id": result["id"]}


@app.delete("/api/bills/{bill_id}/payments/{payment_id}")
def delete_payment(bill_id: int, payment_id: int, who=Depends(mutation), db=Depends(db_session)):
    payment = one(db, "SELECT id,card_purchase_id FROM transactions WHERE bill_id=%s AND id=%s", (bill_id, payment_id))
    if not payment:
        fail(404, "Pagamento não encontrado.")
    if payment["card_purchase_id"]:
        remove_card_purchase(db, payment["card_purchase_id"])
    else:
        db.execute("DELETE FROM transactions WHERE id=%s", (payment_id,))
    return {"ok": True}


def expense_method(value: dict, transaction_kind: str) -> str | None:
    return payment_method(value.get("paymentMethod", "pix")) if transaction_kind == "expense" else None


@app.post("/api/transactions", status_code=201)
def create_transaction(value: dict, who=Depends(mutation), db=Depends(db_session)):
    account_id = integer(value.get("accountId"), "Conta")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    transaction_kind = kind(value.get("kind"))
    method = expense_method(value, transaction_kind)
    category_id, subcategory_id, category_name = category_choice(db, value, transaction_kind)
    when = parsed_date(value.get("date"))
    if when > today(os.getenv("APP_TIMEZONE", "America/Araguaina")):
        fail(400, "Lançamentos imediatos não podem ter data futura. Use Despesas para contas a vencer.")
    description = text(value.get("description"), "Nome do lançamento")
    amount = integer(value.get("amountCents"), "Valor")
    if method == "credit":
        card = card_for_account(db, value.get("creditCardId"), account_id)
        installments = integer(value.get("installments", 1), "Parcelas", 1, 48)
        return {"id": create_card_purchase(db, card, description, (category_name, category_id, subcategory_id),
            note(value.get("note")), amount, when, installments)}
    result = one(db, """INSERT INTO transactions(account_id,kind,description,category,category_id,
        subcategory_id,note,amount_cents,date,payment_method) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (account_id, transaction_kind, description, category_name, category_id, subcategory_id,
         note(value.get("note")), amount, when, method))
    return {"id": result["id"]}


@app.patch("/api/transactions/{transaction_id}")
def edit_transaction(transaction_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    existing = one(db, "SELECT kind,bill_id,planned_income_id,movement_type,card_purchase_id FROM transactions WHERE id=%s",
        (transaction_id,))
    if not existing:
        fail(404, "Lançamento não encontrado.")
    if existing["bill_id"]:
        fail(409, "Edite o pagamento na despesa correspondente.")
    if existing["planned_income_id"]:
        fail(409, "Exclua o recebimento na entrada correspondente.")
    if one(db, "SELECT 1 FROM passive_yields WHERE transaction_id=%s", (transaction_id,)):
        fail(409, "Rendimentos automáticos não podem ser editados.")
    if existing["movement_type"] == "transfer":
        fail(409, "Exclua o aporte ou resgate e registre novamente.")
    if existing["card_purchase_id"]:
        # Amount, date and card define the invoices; only the description and classification change here.
        category_id, subcategory_id, category_name = category_choice(db, value, "expense")
        description = text(value.get("description"), "Nome do lançamento")
        purchase = one(db, "UPDATE card_purchases SET description=%s WHERE id=%s RETURNING installments",
            (description, existing["card_purchase_id"]))
        db.execute("""UPDATE transactions SET description=CASE WHEN %s > 1
            THEN %s || ' (' || installment_number || '/' || %s || ')' ELSE %s END,
            category=%s,category_id=%s,subcategory_id=%s,note=%s WHERE card_purchase_id=%s""",
            (purchase["installments"], description, purchase["installments"], description, category_name,
             category_id, subcategory_id, note(value.get("note")), existing["card_purchase_id"]))
        return {"ok": True}
    account_id = integer(value.get("accountId"), "Conta")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    transaction_kind = kind(value.get("kind"))
    method = expense_method(value, transaction_kind)
    if method == "credit":
        fail(400, "Para lançar no crédito, exclua este lançamento e registre a compra no cartão.")
    category_id, subcategory_id, category_name = category_choice(db, value, transaction_kind)
    when = parsed_date(value.get("date"))
    if when > today(os.getenv("APP_TIMEZONE", "America/Araguaina")):
        fail(400, "Lançamentos imediatos não podem ter data futura.")
    db.execute("""UPDATE transactions SET account_id=%s,kind=%s,description=%s,category=%s,
        category_id=%s,subcategory_id=%s,note=%s,amount_cents=%s,date=%s,payment_method=%s WHERE id=%s""",
        (account_id, transaction_kind, text(value.get("description"), "Nome do lançamento"), category_name,
        category_id, subcategory_id, note(value.get("note")), integer(value.get("amountCents"), "Valor"), when,
        method, transaction_id))
    return {"ok": True}


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(transaction_id: int, who=Depends(mutation), db=Depends(db_session)):
    existing = one(db, "SELECT * FROM transactions WHERE id=%s", (transaction_id,))
    if not existing:
        fail(404, "Lançamento não encontrado.")
    if existing["bill_id"]:
        fail(409, "Exclua o pagamento na despesa correspondente.")
    if existing["planned_income_id"]:
        fail(409, "Exclua o recebimento na entrada correspondente.")
    if one(db, "SELECT 1 FROM passive_yields WHERE transaction_id=%s", (transaction_id,)):
        fail(409, "Rendimentos automáticos não podem ser excluídos.")
    if existing["card_purchase_id"]:
        remove_card_purchase(db, existing["card_purchase_id"])
        return {"ok": True}
    if existing["transfer_group"]:
        accounts = [row["account_id"] for row in all_rows(db,
            "DELETE FROM transactions WHERE transfer_group=%s RETURNING account_id", (existing["transfer_group"],))]
        for card in all_rows(db, "SELECT * FROM credit_cards WHERE collateral_account_id = ANY(%s) FOR UPDATE", (accounts,)):
            if card_limits(db, card, today(os.getenv("APP_TIMEZONE", "America/Araguaina")))["available_cents"] < 0:
                fail(409, "Excluir este aporte deixaria o limite do cartão abaixo do valor já usado.")
        return {"ok": True}
    db.execute("DELETE FROM transactions WHERE id=%s", (transaction_id,))
    return {"ok": True}


def card_values(db, value: dict) -> tuple:
    account_id = integer(value.get("paymentAccountId"), "Conta do cartão")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    if one(db, "SELECT id FROM credit_cards WHERE collateral_account_id=%s", (account_id,)):
        fail(400, "A conta de limite garantido não pode pagar um cartão.")
    return (text(value.get("name"), "Nome do cartão", 80), account_id,
        integer(value.get("baseLimitCents", 0), "Limite", 0),
        integer(value.get("closingDay"), "Dia de fechamento", 1, 31),
        integer(value.get("dueDay"), "Dia de vencimento", 1, 31),
        integer(value.get("lateInterestBasisPoints", 0), "Juros da fatura", 0, 10000), note(value.get("note")))


@app.post("/api/cards", status_code=201)
def create_card(value: dict, who=Depends(mutation), db=Depends(db_session)):
    name, account_id, base_limit, closing, due, late_interest, card_note = card_values(db, value)
    rate_id, multiplier = account_yield_choice(db, value)
    collateral = one(db, """INSERT INTO accounts(name,type,opening_cents,note,yield_rate_id,
        yield_multiplier_basis_points,yield_start_date) VALUES(%s,'Limite garantido',0,%s,%s,%s,%s) RETURNING id""",
        (f"Limite garantido · {name}"[:120], f"Garantia do cartão {name}", rate_id, multiplier,
         yield_start(yield_clock()) if rate_id else None))
    result = one(db, """INSERT INTO credit_cards(name,payment_account_id,collateral_account_id,base_limit_cents,
        closing_day,due_day,late_interest_basis_points,note) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (name, account_id, collateral["id"], base_limit, closing, due, late_interest, card_note))
    return {"id": result["id"]}


@app.patch("/api/cards/{card_id}")
def edit_card(card_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    sync_dynamic_rates(db, yield_clock().date())
    accrue_passive_yields(db)
    card = one(db, "SELECT * FROM credit_cards WHERE id=%s FOR UPDATE", (card_id,))
    if not card:
        fail(404, "Cartão não encontrado.")
    name, account_id, base_limit, closing, due, late_interest, card_note = card_values(db, value)
    has_purchases = one(db, "SELECT id FROM card_purchases WHERE credit_card_id=%s LIMIT 1", (card_id,))
    if has_purchases and (account_id, closing, due) != (card["payment_account_id"], card["closing_day"], card["due_day"]):
        fail(409, "Conta, fechamento e vencimento não podem mudar depois das primeiras compras.")
    db.execute("""UPDATE credit_cards SET name=%s,payment_account_id=%s,base_limit_cents=%s,closing_day=%s,
        due_day=%s,late_interest_basis_points=%s,note=%s WHERE id=%s""",
        (name, account_id, base_limit, closing, due, late_interest, card_note, card_id))
    db.execute("""UPDATE bills SET interest_rate_basis_points=%s WHERE credit_card_id=%s
        AND NOT EXISTS (SELECT 1 FROM transactions WHERE bill_id=bills.id)""", (late_interest, card_id))
    collateral = one(db, "SELECT * FROM accounts WHERE id=%s FOR UPDATE", (card["collateral_account_id"],))
    rate_id, multiplier = account_yield_choice(db, value)
    changed = rate_id != collateral["yield_rate_id"] or multiplier != collateral["yield_multiplier_basis_points"]
    start = yield_start(yield_clock()) if rate_id and changed else collateral["yield_start_date"] if rate_id else None
    db.execute("""UPDATE accounts SET name=%s,yield_rate_id=%s,yield_multiplier_basis_points=%s,yield_start_date=%s
        WHERE id=%s""", (f"Limite garantido · {name}"[:120], rate_id, multiplier, start, collateral["id"]))
    if card_limits(db, {**card, "base_limit_cents": base_limit}, today(os.getenv("APP_TIMEZONE", "America/Araguaina")))["available_cents"] < 0:
        fail(409, "O novo limite fica abaixo do valor já usado no cartão.")
    return {"ok": True}


@app.delete("/api/cards/{card_id}")
def delete_card(card_id: int, who=Depends(mutation), db=Depends(db_session)):
    card = one(db, "SELECT * FROM credit_cards WHERE id=%s FOR UPDATE", (card_id,))
    if not card:
        fail(404, "Cartão não encontrado.")
    if one(db, "SELECT id FROM card_purchases WHERE credit_card_id=%s LIMIT 1", (card_id,)):
        fail(409, "Este cartão possui compras.")
    if one(db, "SELECT id FROM transactions WHERE account_id=%s LIMIT 1", (card["collateral_account_id"],)):
        fail(409, "Resgate o limite garantido e exclua os aportes antes de excluir o cartão.")
    db.execute("DELETE FROM bills WHERE credit_card_id=%s", (card_id,))
    db.execute("DELETE FROM credit_cards WHERE id=%s", (card_id,))
    db.execute("DELETE FROM passive_yields WHERE account_id=%s", (card["collateral_account_id"],))
    db.execute("DELETE FROM accounts WHERE id=%s", (card["collateral_account_id"],))
    return {"ok": True}


@app.post("/api/cards/{card_id}/collateral", status_code=201)
def move_collateral(card_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    current = today(os.getenv("APP_TIMEZONE", "America/Araguaina"))
    card = one(db, "SELECT * FROM credit_cards WHERE id=%s FOR UPDATE", (card_id,))
    if not card:
        fail(404, "Cartão não encontrado.")
    direction = value.get("direction")
    if direction not in ("deposit", "withdraw"):
        fail(400, "Movimento inválido.")
    account_id = integer(value.get("accountId"), "Conta")
    if account_id == card["collateral_account_id"] or not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)) \
            or one(db, "SELECT id FROM credit_cards WHERE collateral_account_id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    amount = integer(value.get("amountCents"), "Valor")
    when = parsed_date(value.get("date"))
    if when > current:
        fail(400, "Registre o movimento na data em que ele acontecer.")
    limits = card_limits(db, card, current)
    if direction == "withdraw":
        if amount > limits["collateral_cents"]:
            fail(400, "Resgate acima do saldo do limite garantido.")
        if amount > limits["available_cents"]:
            fail(409, "O resgate deixaria o limite abaixo do valor já usado no cartão.")
    group = one(db, "SELECT nextval('transfer_group_seq') AS id")["id"]
    label = "Aporte no limite garantido" if direction == "deposit" else "Resgate do limite garantido"
    source, target = (account_id, card["collateral_account_id"]) if direction == "deposit" else (card["collateral_account_id"], account_id)
    for account, movement in ((source, "expense"), (target, "income")):
        db.execute("""INSERT INTO transactions(account_id,kind,description,category,note,amount_cents,date,
            movement_type,transfer_group) VALUES(%s,%s,%s,'Transferência',%s,%s,%s,'transfer',%s)""",
            (account, movement, f"{label}: {card['name']}", note(value.get("note")), amount, when, group))
    return {"ok": True}


@app.delete("/api/card-purchases/{purchase_id}")
def delete_card_purchase(purchase_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE card_purchase_id=%s AND bill_id IS NOT NULL LIMIT 1", (purchase_id,)):
        fail(409, "Exclua o pagamento na despesa correspondente.")
    remove_card_purchase(db, purchase_id)
    return {"ok": True}


@app.post("/api/investments", status_code=201)
def create_investment(value: dict, who=Depends(mutation), db=Depends(db_session)):
    period = value.get("period")
    if period not in ("daily", "monthly", "yearly"):
        fail(400, "Periodicidade inválida.")
    rate = integer(value.get("rateBasisPoints"), "Taxa", -9999, 100000)
    result = one(db, """INSERT INTO investments(name,type,principal_cents,rate_basis_points,period,start_date,note)
        VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""", (text(value.get("name"), "Nome do investimento"),
        text(value.get("type"), "Tipo", 60), integer(value.get("principalCents"), "Valor inicial"), rate,
        period, parsed_date(value.get("startDate")), note(value.get("note"))))
    return {"id": result["id"]}


@app.delete("/api/investments/{investment_id}")
def delete_investment(investment_id: int, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "DELETE FROM investments WHERE id=%s RETURNING id", (investment_id,)):
        fail(404, "Investimento não encontrado.")
    return {"ok": True}


@app.post("/api/users", status_code=201)
def create_user(value: dict, who=Depends(mutation), db=Depends(db_session)):
    admin(who)
    email = text(value.get("email"), "Email", 254).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        fail(400, "Email inválido.")
    result = one(db, "INSERT INTO users(name,email,password_hash,role) VALUES(%s,%s,%s,'member') RETURNING id",
        (text(value.get("name"), "Nome", 80), email, password_hash(value.get("password"))))
    return {"id": result["id"]}


@app.post("/api/users/{user_id}/password")
def reset_user_password(user_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    admin(who)
    if not one(db, "UPDATE users SET password_hash=%s WHERE id=%s AND role='member' RETURNING id", (password_hash(value.get("password")), user_id)):
        fail(404, "Usuário não encontrado.")
    db.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
    return {"ok": True}


@app.patch("/api/users/{user_id}")
def toggle_user(user_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    admin(who)
    if user_id == who["id"]:
        fail(400, "Você não pode desativar sua própria conta.")
    if not isinstance(value.get("active"), bool):
        fail(400, "Estado inválido.")
    if not one(db, "UPDATE users SET active=%s WHERE id=%s AND role='member' RETURNING id", (value["active"], user_id)):
        fail(404, "Usuário não encontrado.")
    if not value["active"]:
        db.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
    return {"ok": True}
