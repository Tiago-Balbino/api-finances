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
ALTER TABLE yield_rates ADD COLUMN IF NOT EXISTS source_code TEXT CHECK (source_code IN ('cdi', 'selic_target'));
ALTER TABLE yield_rates ADD COLUMN IF NOT EXISTS source_date DATE;
ALTER TABLE yield_rates ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
ALTER TABLE yield_rates ADD COLUMN IF NOT EXISTS sync_error TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS yield_rate_observations (
  source_code TEXT NOT NULL CHECK (source_code IN ('cdi', 'selic_target')),
  date DATE NOT NULL,
  annual_basis_points INTEGER NOT NULL CHECK (annual_basis_points BETWEEN 0 AND 10000),
  PRIMARY KEY (source_code,date)
);
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
ALTER TABLE bills DROP CONSTRAINT IF EXISTS bills_interest_period_check;
ALTER TABLE bills ADD CONSTRAINT bills_interest_period_check CHECK (interest_period IN ('daily', 'monthly', 'yearly'));
ALTER TABLE bills ADD COLUMN IF NOT EXISTS interest_rate_period TEXT CHECK (interest_rate_period IN ('daily', 'monthly', 'yearly'));
ALTER TABLE bills ADD COLUMN IF NOT EXISTS fine_basis_points INTEGER NOT NULL DEFAULT 0 CHECK (fine_basis_points BETWEEN 0 AND 10000);
ALTER TABLE bills ADD COLUMN IF NOT EXISTS fine_after TEXT NOT NULL DEFAULT 'day' CHECK (fine_after IN ('day', 'month', 'year'));
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
CREATE TABLE IF NOT EXISTS investment_movements (
  id BIGSERIAL PRIMARY KEY,
  investment_id BIGINT NOT NULL REFERENCES investments(id) ON DELETE CASCADE,
  direction TEXT NOT NULL CHECK (direction IN ('deposit', 'withdraw')),
  amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
  date DATE NOT NULL,
  account_id BIGINT REFERENCES accounts(id) ON DELETE RESTRICT,
  transaction_id BIGINT UNIQUE REFERENCES transactions(id) ON DELETE RESTRICT,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS investment_movements_investment_idx ON investment_movements(investment_id);
-- Older boxes only had an initial amount; it becomes their first deposit.
ALTER TABLE investments ADD COLUMN IF NOT EXISTS movements_seeded BOOLEAN NOT NULL DEFAULT FALSE;
INSERT INTO investment_movements(investment_id,direction,amount_cents,date)
SELECT id,'deposit',principal_cents,start_date FROM investments WHERE movements_seeded=FALSE;
UPDATE investments SET movements_seeded=TRUE WHERE movements_seeded=FALSE;
CREATE TABLE IF NOT EXISTS credit_cards (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  payment_account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  collateral_account_id BIGINT UNIQUE REFERENCES accounts(id) ON DELETE RESTRICT,
  base_limit_cents BIGINT NOT NULL DEFAULT 0 CHECK (base_limit_cents >= 0),
  closing_day INTEGER NOT NULL CHECK (closing_day BETWEEN 1 AND 31),
  due_day INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
  late_interest_basis_points INTEGER NOT NULL DEFAULT 0 CHECK (late_interest_basis_points BETWEEN 0 AND 10000),
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS card_purchases (
  id BIGSERIAL PRIMARY KEY,
  credit_card_id BIGINT NOT NULL REFERENCES credit_cards(id) ON DELETE RESTRICT,
  description TEXT NOT NULL,
  total_cents BIGINT NOT NULL CHECK (total_cents > 0),
  installments INTEGER NOT NULL CHECK (installments BETWEEN 1 AND 48),
  purchase_date DATE NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE bills ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT 'pix' CHECK (payment_method IN ('pix', 'debit', 'credit'));
ALTER TABLE bills ADD COLUMN IF NOT EXISTS credit_card_id BIGINT REFERENCES credit_cards(id) ON DELETE RESTRICT;
ALTER TABLE bills ADD COLUMN IF NOT EXISTS invoice_month DATE;
CREATE UNIQUE INDEX IF NOT EXISTS bills_card_invoice_idx ON bills(credit_card_id, invoice_month);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_method TEXT CHECK (payment_method IN ('pix', 'debit', 'credit'));
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS movement_type TEXT NOT NULL DEFAULT 'regular'
  CHECK (movement_type IN ('regular', 'card_purchase', 'invoice_payment', 'transfer'));
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS credit_card_id BIGINT REFERENCES credit_cards(id) ON DELETE RESTRICT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS card_purchase_id BIGINT REFERENCES card_purchases(id) ON DELETE RESTRICT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS invoice_bill_id BIGINT REFERENCES bills(id) ON DELETE RESTRICT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS installment_number INTEGER;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS transfer_group BIGINT;
CREATE SEQUENCE IF NOT EXISTS transfer_group_seq;
CREATE TABLE IF NOT EXISTS simulations (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
  summary JSONB NOT NULL,
  result JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO categories(kind,name)
SELECT 'expense', category FROM bills WHERE trim(category) <> ''
ON CONFLICT DO NOTHING;
INSERT INTO categories(kind,name)
SELECT kind, category FROM transactions WHERE trim(category) <> ''
ON CONFLICT DO NOTHING;
INSERT INTO categories(kind,name) VALUES
  ('expense','Moradia'),('expense','Alimentação'),('expense','Transporte'),
  ('expense','Saúde'),('expense','Educação'),('expense','Lazer'),('expense','Outros'),
  ('expense','Cartão de crédito'),
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
CREATE INDEX IF NOT EXISTS transactions_invoice_bill_idx ON transactions(invoice_bill_id);
CREATE INDEX IF NOT EXISTS transactions_card_purchase_idx ON transactions(card_purchase_id);
CREATE INDEX IF NOT EXISTS transactions_planned_income_idx ON transactions(planned_income_id);
CREATE INDEX IF NOT EXISTS transactions_credit_card_idx ON transactions(credit_card_id);
CREATE INDEX IF NOT EXISTS transactions_account_date_idx ON transactions(account_id,date);
CREATE INDEX IF NOT EXISTS bills_credit_card_idx ON bills(credit_card_id) WHERE credit_card_id IS NOT NULL;

-- Bloqueia acesso pela API REST do Supabase (anon/authenticated). A aplicação conecta como dona das tabelas e não é afetada.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE subcategories ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE bills ENABLE ROW LEVEL SECURITY;
ALTER TABLE planned_incomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE investments ENABLE ROW LEVEL SECURITY;
ALTER TABLE yield_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE yield_rate_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE passive_yields ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_cards ENABLE ROW LEVEL SECURITY;
ALTER TABLE card_purchases ENABLE ROW LEVEL SECURITY;
ALTER TABLE investment_movements ENABLE ROW LEVEL SECURITY;
ALTER TABLE simulations ENABLE ROW LEVEL SECURITY;
