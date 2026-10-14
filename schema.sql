CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  opening_cents BIGINT NOT NULL CHECK (opening_cents >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS yield_rates (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  annual_basis_points INTEGER NOT NULL CHECK (annual_basis_points >= 0 AND annual_basis_points <= 10000)
);
CREATE UNIQUE INDEX IF NOT EXISTS yield_rates_name_idx ON yield_rates(lower(name));
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS yield_rate_id BIGINT REFERENCES yield_rates(id) ON DELETE RESTRICT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS yield_multiplier_basis_points INTEGER NOT NULL DEFAULT 10000
  CHECK (yield_multiplier_basis_points BETWEEN 0 AND 100000);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS yield_start_date DATE;
CREATE TABLE IF NOT EXISTS categories (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS categories_kind_name_idx ON categories(kind, lower(name));
CREATE TABLE IF NOT EXISTS subcategories (
  id BIGSERIAL PRIMARY KEY,
  category_id BIGINT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS subcategories_category_name_idx ON subcategories(category_id, lower(name));
CREATE TABLE IF NOT EXISTS bills (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  description TEXT NOT NULL,
  category TEXT NOT NULL,
  amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
  due_date DATE NOT NULL,
  interest_rate_basis_points INTEGER NOT NULL DEFAULT 0 CHECK (interest_rate_basis_points BETWEEN 0 AND 10000),
  interest_period TEXT NOT NULL DEFAULT 'monthly' CHECK (interest_period IN ('daily', 'monthly')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE bills DROP CONSTRAINT IF EXISTS bills_interest_rate_basis_points_check;
ALTER TABLE bills ADD CONSTRAINT bills_interest_rate_basis_points_check CHECK (interest_rate_basis_points BETWEEN 0 AND 10000);
ALTER TABLE bills ADD COLUMN IF NOT EXISTS category_id BIGINT REFERENCES categories(id) ON DELETE RESTRICT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS subcategory_id BIGINT REFERENCES subcategories(id) ON DELETE RESTRICT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
ALTER TABLE bills ADD COLUMN IF NOT EXISTS expense_type TEXT NOT NULL DEFAULT 'eventual' CHECK (expense_type IN ('eventual', 'fixed'));
ALTER TABLE bills ADD COLUMN IF NOT EXISTS recurrence_origin_id BIGINT REFERENCES bills(id) ON DELETE RESTRICT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS recurrence_month DATE;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS recurrence_active BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;
CREATE UNIQUE INDEX IF NOT EXISTS bills_recurrence_month_idx ON bills(recurrence_origin_id, recurrence_month);
CREATE TABLE IF NOT EXISTS planned_incomes (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  description TEXT NOT NULL,
  category TEXT NOT NULL,
  category_id BIGINT REFERENCES categories(id) ON DELETE RESTRICT,
  subcategory_id BIGINT REFERENCES subcategories(id) ON DELETE RESTRICT,
  note TEXT NOT NULL DEFAULT '',
  amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
  due_date DATE NOT NULL,
  income_type TEXT NOT NULL CHECK (income_type IN ('eventual', 'fixed')),
  recurrence_origin_id BIGINT REFERENCES planned_incomes(id) ON DELETE RESTRICT,
  recurrence_month DATE,
  recurrence_active BOOLEAN NOT NULL DEFAULT FALSE,
  archived BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS planned_incomes_recurrence_month_idx ON planned_incomes(recurrence_origin_id, recurrence_month);
CREATE TABLE IF NOT EXISTS transactions (
  id BIGSERIAL PRIMARY KEY,
  account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK (kind IN ('income', 'expense')),
  description TEXT NOT NULL,
  category TEXT NOT NULL,
  amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
  date DATE NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS bill_id BIGINT REFERENCES bills(id) ON DELETE RESTRICT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS planned_income_id BIGINT REFERENCES planned_incomes(id) ON DELETE RESTRICT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS category_id BIGINT REFERENCES categories(id) ON DELETE RESTRICT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS subcategory_id BIGINT REFERENCES subcategories(id) ON DELETE RESTRICT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS passive_yields (
  account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  date DATE NOT NULL,
  amount_cents BIGINT NOT NULL CHECK (amount_cents >= 0),
  transaction_id BIGINT UNIQUE REFERENCES transactions(id) ON DELETE RESTRICT,
  PRIMARY KEY (account_id, date)
);
CREATE TABLE IF NOT EXISTS investments (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  principal_cents BIGINT NOT NULL CHECK (principal_cents > 0),
  rate_basis_points INTEGER NOT NULL,
  period TEXT NOT NULL CHECK (period IN ('daily', 'monthly', 'yearly')),
  start_date DATE NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE investments ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
INSERT INTO categories(kind,name)
SELECT 'expense', category FROM bills WHERE trim(category) <> ''
ON CONFLICT DO NOTHING;
INSERT INTO categories(kind,name)
SELECT kind, category FROM transactions WHERE trim(category) <> ''
ON CONFLICT DO NOTHING;
INSERT INTO categories(kind,name) VALUES
  ('expense','Moradia'),('expense','Alimentação'),('expense','Transporte'),
  ('expense','Saúde'),('expense','Educação'),('expense','Lazer'),('expense','Outros'),
  ('income','Salário'),('income','Serviços'),('income','Investimentos'),('income','Outros')
ON CONFLICT DO NOTHING;
UPDATE bills b SET category_id=c.id FROM categories c
WHERE b.category_id IS NULL AND c.kind='expense' AND lower(c.name)=lower(b.category);
UPDATE transactions t SET category_id=c.id FROM categories c
WHERE t.category_id IS NULL AND c.kind=t.kind AND lower(c.name)=lower(t.category);
CREATE INDEX IF NOT EXISTS transactions_account_idx ON transactions(account_id);
CREATE INDEX IF NOT EXISTS transactions_date_idx ON transactions(date DESC);
CREATE INDEX IF NOT EXISTS transactions_bill_idx ON transactions(bill_id);
CREATE INDEX IF NOT EXISTS bills_due_idx ON bills(due_date);
CREATE INDEX IF NOT EXISTS transactions_category_date_idx ON transactions(category_id,date DESC);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);

-- Bloqueia acesso pela API REST do Supabase (anon/authenticated). A aplicação conecta como dona das tabelas e não é afetada.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE subcategories ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bills ENABLE ROW LEVEL SECURITY;
ALTER TABLE investments ENABLE ROW LEVEL SECURITY;
ALTER TABLE yield_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE passive_yields ENABLE ROW LEVEL SECURITY;
