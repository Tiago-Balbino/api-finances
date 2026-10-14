import hashlib
import hmac
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.rows import dict_row

from .finance import add_months, bill_status, debt_cents_at, projected_cents, today


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
    if period not in ("daily", "monthly"):
        fail(400, "Periodicidade dos juros inválida.")
    return integer(value, "Taxa de juros", 0, 1000 if period == "daily" else 10000)


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
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
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


def bill_rows(db, as_of: date):
    bills = all_rows(db, """SELECT b.*,c.name AS category_name,s.name AS subcategory_name,a.name AS account_name
        FROM bills b JOIN accounts a ON a.id=b.account_id
        LEFT JOIN categories c ON c.id=b.category_id LEFT JOIN subcategories s ON s.id=b.subcategory_id
        ORDER BY b.due_date,b.id""")
    payments = all_rows(db, """SELECT t.id,t.bill_id,t.account_id,t.amount_cents,t.date,t.note,a.name AS account_name
        FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.bill_id IS NOT NULL ORDER BY t.date,t.id""")
    by_bill: dict[int, list[dict]] = {}
    for payment in payments:
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
    accounts = all_rows(db, """SELECT a.*, a.opening_cents + COALESCE(SUM(CASE WHEN t.date>%s THEN 0
        WHEN t.kind='income' THEN t.amount_cents ELSE -t.amount_cents END),0) AS balance_cents
        FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id GROUP BY a.id ORDER BY a.id DESC""", (current,))
    transactions = all_rows(db, """SELECT t.*,a.name AS account_name,COALESCE(c.name,t.category) AS category_name,
        s.name AS subcategory_name FROM transactions t JOIN accounts a ON a.id=t.account_id
        LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN subcategories s ON s.id=t.subcategory_id
        ORDER BY t.date DESC,t.id DESC LIMIT 500""")
    for transaction in transactions:
        transaction["category"] = transaction["category_name"]
    investments = all_rows(db, "SELECT * FROM investments ORDER BY id DESC")
    for investment in investments:
        investment["current_cents"] = projected_cents(investment["principal_cents"], investment["rate_basis_points"], investment["start_date"], current, investment["period"])
        investment["future_cents"] = projected_cents(investment["principal_cents"], investment["rate_basis_points"], investment["start_date"], add_months(current, 12), investment["period"])
    bills = bill_rows(db, current)
    categories = all_rows(db, "SELECT id,kind,name FROM categories ORDER BY kind,name")
    subcategories = all_rows(db, "SELECT id,category_id,name FROM subcategories ORDER BY name")
    month_start = current.replace(day=1)
    month_end = add_months(month_start, 1)
    month_totals_rows = all_rows(db, """SELECT kind,SUM(amount_cents)::bigint AS amount_cents FROM transactions
        WHERE date >= %s AND date <= %s GROUP BY kind""", (month_start, current))
    month_totals = {"income_cents": 0, "expense_cents": 0}
    for item in month_totals_rows:
        month_totals[f"{item['kind']}_cents"] = item["amount_cents"]
    spending = all_rows(db, """SELECT t.category_id,COALESCE(c.name,t.category) AS category,
        t.subcategory_id,s.name AS subcategory,SUM(t.amount_cents)::bigint AS amount_cents
        FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
        LEFT JOIN subcategories s ON s.id=t.subcategory_id
        WHERE t.kind='expense' AND t.date >= %s AND t.date < %s AND t.date <= %s
        GROUP BY t.category_id,COALESCE(c.name,t.category),t.subcategory_id,s.name ORDER BY amount_cents DESC""", (month_start, month_end, current))
    investment_series = [{"date": add_months(current, i), "value_cents": sum(projected_cents(x["principal_cents"], x["rate_basis_points"], x["start_date"], add_months(current, i), x["period"]) or 0 for x in investments)} for i in range(13)]
    debt_series = [{"date": add_months(current, i), "value_cents": sum(debt_cents_at(x, x["payments"], add_months(current, i)) or 0 for x in bills)} for i in range(13)]
    due_series = []
    for i in range(6):
        month = add_months(current, i).strftime("%Y-%m")
        relevant = (b for b in bills if b["status"] != "paid" and (b["due_date"].strftime("%Y-%m") <= month if i == 0 else b["due_date"].strftime("%Y-%m") == month))
        due_series.append({"date": add_months(current, i), "value_cents": sum(debt_cents_at(b, b["payments"], max(current, b["due_date"])) or 0 for b in relevant)})
    users = all_rows(db, "SELECT id,name,email,role,active FROM users ORDER BY id") if who["role"] == "admin" else []
    return jsonable_encoder({"user": who, "today": current, "accounts": accounts, "transactions": transactions,
        "investments": investments, "bills": bills, "categories": categories, "subcategories": subcategories,
        "monthly_spending": spending, "monthly_totals": month_totals, "investment_series": investment_series,
        "debt_series": debt_series, "due_series": due_series, "users": users})


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
    if one(db, "SELECT id FROM bills WHERE category_id=%s LIMIT 1", (category_id,)) or one(db, "SELECT id FROM transactions WHERE category_id=%s LIMIT 1", (category_id,)) or one(db, "SELECT id FROM subcategories WHERE category_id=%s LIMIT 1", (category_id,)):
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
    if one(db, "SELECT id FROM bills WHERE subcategory_id=%s LIMIT 1", (subcategory_id,)) or one(db, "SELECT id FROM transactions WHERE subcategory_id=%s LIMIT 1", (subcategory_id,)):
        fail(409, "Subcategoria em uso. Reclassifique os registros primeiro.")
    if not one(db, "DELETE FROM subcategories WHERE id=%s RETURNING id", (subcategory_id,)):
        fail(404, "Subcategoria não encontrada.")
    return {"ok": True}


@app.post("/api/accounts", status_code=201)
def create_account(value: dict, who=Depends(mutation), db=Depends(db_session)):
    result = one(db, "INSERT INTO accounts(name,type,opening_cents,note) VALUES(%s,%s,%s,%s) RETURNING id",
        (text(value.get("name"), "Nome da conta"), text(value.get("type"), "Tipo", 60), integer(value.get("openingCents"), "Saldo inicial", 0), note(value.get("note"))))
    return {"id": result["id"]}


@app.patch("/api/accounts/{account_id}")
def edit_account(account_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "UPDATE accounts SET name=%s,type=%s,opening_cents=%s,note=%s WHERE id=%s RETURNING id",
        (text(value.get("name"), "Nome da conta"), text(value.get("type"), "Tipo", 60), integer(value.get("openingCents"), "Saldo inicial", 0), note(value.get("note")), account_id)):
        fail(404, "Conta não encontrada.")
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE account_id=%s LIMIT 1", (account_id,)) or one(db, "SELECT id FROM bills WHERE account_id=%s LIMIT 1", (account_id,)):
        fail(409, "Esta conta possui despesas ou lançamentos.")
    if not one(db, "DELETE FROM accounts WHERE id=%s RETURNING id", (account_id,)):
        fail(404, "Conta não encontrada.")
    return {"ok": True}


def bill_values(db, value: dict):
    account_id = integer(value.get("accountId"), "Conta")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    category_id, subcategory_id, category_name = category_choice(db, value, "expense")
    period = value.get("interestPeriod")
    return (account_id, text(value.get("description"), "Nome da despesa"), category_name,
        category_id, subcategory_id, note(value.get("note")), integer(value.get("amountCents"), "Valor"),
        parsed_date(value.get("dueDate")), interest(value.get("interestRateBasisPoints"), period), period)


@app.post("/api/bills", status_code=201)
def create_bill(value: dict, who=Depends(mutation), db=Depends(db_session)):
    result = one(db, """INSERT INTO bills(account_id,description,category,category_id,subcategory_id,note,
        amount_cents,due_date,interest_rate_basis_points,interest_period)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""", bill_values(db, value))
    return {"id": result["id"]}


@app.patch("/api/bills/{bill_id}")
def edit_bill(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE bill_id=%s LIMIT 1", (bill_id,)):
        fail(409, "Uma despesa com pagamentos não pode ser editada.")
    if not one(db, """UPDATE bills SET account_id=%s,description=%s,category=%s,category_id=%s,
        subcategory_id=%s,note=%s,amount_cents=%s,due_date=%s,interest_rate_basis_points=%s,
        interest_period=%s WHERE id=%s RETURNING id""", (*bill_values(db, value), bill_id)):
        fail(404, "Despesa não encontrada.")
    return {"ok": True}


@app.delete("/api/bills/{bill_id}")
def delete_bill(bill_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE bill_id=%s LIMIT 1", (bill_id,)):
        fail(409, "Uma despesa com pagamentos não pode ser excluída.")
    if not one(db, "DELETE FROM bills WHERE id=%s RETURNING id", (bill_id,)):
        fail(404, "Despesa não encontrada.")
    return {"ok": True}


@app.patch("/api/bills/{bill_id}/note")
def edit_bill_note(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "UPDATE bills SET note=%s WHERE id=%s RETURNING id", (note(value.get("note")), bill_id)):
        fail(404, "Despesa não encontrada.")
    return {"ok": True}


@app.patch("/api/bills/{bill_id}/classification")
def classify_bill(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    category_id, subcategory_id, category_name = category_choice(db, value, "expense")
    if not one(db, "UPDATE bills SET category=%s,category_id=%s,subcategory_id=%s WHERE id=%s RETURNING id",
        (category_name, category_id, subcategory_id, bill_id)):
        fail(404, "Despesa não encontrada.")
    db.execute("UPDATE transactions SET category=%s,category_id=%s,subcategory_id=%s WHERE bill_id=%s",
        (category_name, category_id, subcategory_id, bill_id))
    return {"ok": True}


@app.patch("/api/bills/{bill_id}/interest")
def edit_bill_interest(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    period = value.get("interestPeriod")
    rate = interest(value.get("interestRateBasisPoints"), period)
    bill = next((b for b in bill_rows(db, today(os.getenv("APP_TIMEZONE", "America/Araguaina"))) if b["id"] == bill_id), None)
    if not bill:
        fail(404, "Despesa não encontrada.")
    if bill["status"] == "paid":
        fail(409, "Uma despesa quitada não pode ter juros alterados.")
    db.execute("UPDATE bills SET interest_rate_basis_points=%s,interest_period=%s WHERE id=%s", (rate, period, bill_id))
    return {"ok": True}


@app.post("/api/bills/{bill_id}/payments", status_code=201)
def pay_bill(bill_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    when = parsed_date(value.get("date"))
    if when > today(os.getenv("APP_TIMEZONE", "America/Araguaina")):
        fail(400, "Registre o pagamento na data em que ele acontecer.")
    amount = integer(value.get("amountCents"), "Valor do pagamento")
    account_id = integer(value.get("accountId"), "Conta")
    bill = one(db, "SELECT * FROM bills WHERE id=%s FOR UPDATE", (bill_id,))
    if not bill:
        fail(404, "Despesa não encontrada.")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
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
    result = one(db, """INSERT INTO transactions(account_id,bill_id,kind,description,category,category_id,
        subcategory_id,note,amount_cents,date) VALUES(%s,%s,'expense',%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (account_id, bill_id, f"Pagamento: {bill['description']}", bill["category"], bill["category_id"], bill["subcategory_id"], note(value.get("note")), amount, when))
    return {"id": result["id"]}


@app.delete("/api/bills/{bill_id}/payments/{payment_id}")
def delete_payment(bill_id: int, payment_id: int, who=Depends(mutation), db=Depends(db_session)):
    if not one(db, "DELETE FROM transactions WHERE bill_id=%s AND id=%s RETURNING id", (bill_id, payment_id)):
        fail(404, "Pagamento não encontrado.")
    return {"ok": True}


@app.post("/api/transactions", status_code=201)
def create_transaction(value: dict, who=Depends(mutation), db=Depends(db_session)):
    account_id = integer(value.get("accountId"), "Conta")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    transaction_kind = kind(value.get("kind"))
    category_id, subcategory_id, category_name = category_choice(db, value, transaction_kind)
    when = parsed_date(value.get("date"))
    if when > today(os.getenv("APP_TIMEZONE", "America/Araguaina")):
        fail(400, "Lançamentos imediatos não podem ter data futura. Use Despesas para contas a vencer.")
    result = one(db, """INSERT INTO transactions(account_id,kind,description,category,category_id,
        subcategory_id,note,amount_cents,date) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (account_id, transaction_kind, text(value.get("description"), "Nome do lançamento"), category_name,
        category_id, subcategory_id, note(value.get("note")), integer(value.get("amountCents"), "Valor"), when))
    return {"id": result["id"]}


@app.patch("/api/transactions/{transaction_id}")
def edit_transaction(transaction_id: int, value: dict, who=Depends(mutation), db=Depends(db_session)):
    existing = one(db, "SELECT kind,bill_id FROM transactions WHERE id=%s", (transaction_id,))
    if not existing:
        fail(404, "Lançamento não encontrado.")
    if existing["bill_id"]:
        fail(409, "Edite o pagamento na despesa correspondente.")
    account_id = integer(value.get("accountId"), "Conta")
    if not one(db, "SELECT id FROM accounts WHERE id=%s", (account_id,)):
        fail(400, "Conta inválida.")
    transaction_kind = kind(value.get("kind"))
    category_id, subcategory_id, category_name = category_choice(db, value, transaction_kind)
    when = parsed_date(value.get("date"))
    if when > today(os.getenv("APP_TIMEZONE", "America/Araguaina")):
        fail(400, "Lançamentos imediatos não podem ter data futura.")
    db.execute("""UPDATE transactions SET account_id=%s,kind=%s,description=%s,category=%s,
        category_id=%s,subcategory_id=%s,note=%s,amount_cents=%s,date=%s WHERE id=%s""",
        (account_id, transaction_kind, text(value.get("description"), "Nome do lançamento"), category_name,
        category_id, subcategory_id, note(value.get("note")), integer(value.get("amountCents"), "Valor"), when, transaction_id))
    return {"ok": True}


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(transaction_id: int, who=Depends(mutation), db=Depends(db_session)):
    if one(db, "SELECT id FROM transactions WHERE id=%s AND bill_id IS NOT NULL", (transaction_id,)):
        fail(409, "Exclua o pagamento na despesa correspondente.")
    if not one(db, "DELETE FROM transactions WHERE id=%s RETURNING id", (transaction_id,)):
        fail(404, "Lançamento não encontrado.")
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
