import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { randomBytes, scryptSync, timingSafeEqual, createHash } from 'node:crypto';
import pg from 'pg';
import { projectedCents, addMonths, debtCentsAt, billStatus, daysBetween, todayInZone } from './finance.js';

const { Pool } = pg;
const pool = new Pool({ ...(process.env.DATABASE_URL ? { connectionString: process.env.DATABASE_URL } : { database: process.env.PGDATABASE || 'finances' }), max: 10 });
const sql = (text, values = []) => pool.query(text, values);
const row = async (text, values = []) => (await sql(text, values)).rows[0];
const today = () => todayInZone(process.env.APP_TIMEZONE || 'America/Araguaina');
const tokenHash = token => createHash('sha256').update(token).digest('hex');
const loginAttempts = new Map();
class ApiError extends Error { constructor(status, message) { super(message); this.status = status; } }

function reply(res, status, value) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(value));
}
async function readBody(req) {
  let raw = '';
  for await (const chunk of req) {
    raw += chunk;
    if (raw.length > 64_000) throw new ApiError(413, 'Dados muito grandes.');
  }
  try { return JSON.parse(raw || '{}'); } catch { throw new ApiError(400, 'JSON inválido.'); }
}
function string(v, label, max = 120) {
  if (typeof v !== 'string' || !v.trim() || v.trim().length > max) throw new ApiError(400, `${label} inválido.`);
  return v.trim();
}
function money(v, label, zero = false) {
  if (!Number.isSafeInteger(v) || v < (zero ? 0 : 1) || v > 100_000_000_000) throw new ApiError(400, `${label} inválido.`);
  return v;
}
function interestRate(v, period) {
  if (!['daily','monthly'].includes(period)) throw new ApiError(400, 'Periodicidade dos juros inválida.');
  const max = period === 'daily' ? 1000 : 10000;
  if (!Number.isSafeInteger(v) || v < 0 || v > max) throw new ApiError(400, `A taxa máxima é ${max/100}% ${period === 'daily' ? 'ao dia' : 'ao mês'}.`);
  return v;
}
function numberId(v) { const n = Number(v); if (!Number.isSafeInteger(n) || n < 1) throw new ApiError(400, 'Identificador inválido.'); return n; }
function date(v) {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v) || Number.isNaN(Date.parse(`${v}T12:00:00Z`)) || new Date(`${v}T12:00:00Z`).toISOString().slice(0, 10) !== v) throw new ApiError(400, 'Data inválida.');
  return v;
}
function email(v) {
  const result = string(v, 'Email', 254).toLowerCase();
  if (!/^\S+@\S+\.\S+$/.test(result)) throw new ApiError(400, 'Email inválido.');
  return result;
}
function hashPassword(v) {
  if (typeof v !== 'string' || v.length < 12 || v.length > 200) throw new ApiError(400, 'A senha deve ter entre 12 e 200 caracteres.');
  const salt = randomBytes(16).toString('hex');
  return `${salt}:${scryptSync(v, salt, 64).toString('hex')}`;
}
function verifyPassword(v, stored) {
  if (typeof v !== 'string' || v.length > 200) return false;
  const [salt, hex] = stored.split(':');
  const expected = Buffer.from(hex, 'hex');
  return expected.length === 64 && timingSafeEqual(scryptSync(v, salt, 64), expected);
}
function cookie(token, seconds) {
  return `session=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${seconds}${process.env.COOKIE_SECURE === 'true' ? '; Secure' : ''}`;
}
async function newSession(res, userId) {
  const token = randomBytes(32).toString('hex');
  await sql('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES($1,$2,now() + interval \'30 days\')', [tokenHash(token), userId]);
  res.setHeader('Set-Cookie', cookie(token, 30 * 86400));
}
async function currentUser(req) {
  const token = (req.headers.cookie || '').split(';').map(x => x.trim()).find(x => x.startsWith('session='))?.slice(8);
  if (!token || !/^[a-f0-9]{64}$/.test(token)) return null;
  return await row(`SELECT u.id,u.name,u.email,u.role FROM sessions s JOIN users u ON u.id=s.user_id
    WHERE s.token_hash=$1 AND s.expires_at>now() AND u.active=TRUE`, [tokenHash(token)]) || null;
}
async function requireUser(req) { const u = await currentUser(req); if (!u) throw new ApiError(401, 'Entre na sua conta.'); return u; }
async function requireAdmin(req) { const u = await requireUser(req); if (u.role !== 'admin') throw new ApiError(403, 'Somente o administrador pode fazer isso.'); return u; }
function checkOrigin(req) {
  if (req.headers.origin && new URL(req.headers.origin).host !== req.headers.host) throw new ApiError(403, 'Origem não permitida.');
}
function publicUser(u) { return { id: Number(u.id), name: u.name, email: u.email, role: u.role }; }
const rows = async (text, args = []) => (await sql(text, args)).rows;
const normalizeMoney = record => {
  for (const key of ['opening_cents','balance_cents','amount_cents','principal_cents']) if (record[key] !== undefined) record[key] = Number(record[key]);
  record.id = Number(record.id);
  if (record.account_id !== undefined) record.account_id = Number(record.account_id);
  if (record.bill_id !== undefined && record.bill_id !== null) record.bill_id = Number(record.bill_id);
  if (record.date instanceof Date) record.date = record.date.toISOString().slice(0, 10);
  if (record.start_date instanceof Date) record.start_date = record.start_date.toISOString().slice(0, 10);
  if (record.due_date instanceof Date) record.due_date = record.due_date.toISOString().slice(0, 10);
  return record;
};

async function readBills(asOf) {
  const bills = (await rows(`SELECT b.id,b.account_id,b.description,b.category,b.amount_cents,
    b.interest_rate_basis_points,b.interest_period,to_char(b.due_date,'YYYY-MM-DD') AS due_date,
    a.name AS account_name FROM bills b JOIN accounts a ON a.id=b.account_id ORDER BY b.due_date,b.id`)).map(normalizeMoney);
  const payments = (await rows(`SELECT t.id,t.bill_id,t.account_id,t.amount_cents,
    to_char(t.date,'YYYY-MM-DD') AS date,a.name AS account_name FROM transactions t
    JOIN accounts a ON a.id=t.account_id WHERE t.bill_id IS NOT NULL ORDER BY t.date,t.id`)).map(normalizeMoney);
  const byBill = new Map();
  for (const p of payments) { if (!byBill.has(p.bill_id)) byBill.set(p.bill_id, []); byBill.get(p.bill_id).push(p); }
  return bills.map(b => {
    const history = byBill.get(b.id) || [];
    const current = debtCentsAt(b, history, asOf);
    return { ...b, payments:history, current_cents:current, status:billStatus(b,history,asOf),
      days_overdue:b.due_date < asOf && current > 0 ? daysBetween(b.due_date,asOf) : 0,
      paid_cents:history.reduce((sum,p) => sum + p.amount_cents,0) };
  });
}

async function api(req, res, pathname) {
  const method = req.method;
  if (!['GET','POST','PATCH','DELETE'].includes(method)) throw new ApiError(405, 'Método não permitido.');
  if (method !== 'GET') checkOrigin(req);
  if (pathname === '/api/bootstrap' && method === 'GET') {
    return reply(res, 200, { setupRequired: !(await row('SELECT id FROM users LIMIT 1')), user: await currentUser(req) });
  }
  if (pathname === '/api/setup' && method === 'POST') {
    const v = await readBody(req), name = string(v.name, 'Nome', 80), mail = email(v.email), password = hashPassword(v.password);
    const client = await pool.connect();
    let created;
    try {
      await client.query('BEGIN');
      await client.query('LOCK TABLE users IN EXCLUSIVE MODE');
      if ((await client.query('SELECT id FROM users LIMIT 1')).rowCount) throw new ApiError(409, 'O administrador já foi criado.');
      created = (await client.query("INSERT INTO users(name,email,password_hash,role) VALUES($1,$2,$3,'admin') RETURNING id", [name, mail, password])).rows[0];
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    await newSession(res, created.id); return reply(res, 201, { ok: true });
  }
  if (pathname === '/api/login' && method === 'POST') {
    const v = await readBody(req), mail = email(v.email);
    if (loginAttempts.size > 1000) for (const [oldKey, oldAttempt] of loginAttempts) if (oldAttempt.until <= Date.now()) loginAttempts.delete(oldKey);
    const key = `${req.socket.remoteAddress}:${mail}`, attempt = loginAttempts.get(key);
    if (attempt && attempt.until > Date.now() && attempt.count >= 5) throw new ApiError(429, 'Muitas tentativas. Aguarde 15 minutos.');
    const u = await row('SELECT * FROM users WHERE email=$1 AND active=TRUE', [mail]);
    if (!u || !verifyPassword(v.password, u.password_hash)) {
      loginAttempts.set(key, { count: (attempt?.until > Date.now() ? attempt.count : 0) + 1, until: Date.now() + 15 * 60_000 });
      throw new ApiError(401, 'Email ou senha incorretos.');
    }
    loginAttempts.delete(key);
    await newSession(res, u.id); return reply(res, 200, { user: publicUser(u) });
  }
  if (pathname === '/api/logout' && method === 'POST') {
    const token = (req.headers.cookie || '').split(';').map(x => x.trim()).find(x => x.startsWith('session='))?.slice(8);
    if (token) await sql('DELETE FROM sessions WHERE token_hash=$1', [tokenHash(token)]);
    res.setHeader('Set-Cookie', cookie('', 0)); return reply(res, 200, { ok: true });
  }
  const user = await requireUser(req);
  if (pathname === '/api/profile/password' && method === 'POST') {
    const v = await readBody(req), stored = await row('SELECT password_hash FROM users WHERE id=$1', [user.id]);
    if (!verifyPassword(v.currentPassword, stored.password_hash)) throw new ApiError(400, 'Senha atual incorreta.');
    await sql('UPDATE users SET password_hash=$1 WHERE id=$2', [hashPassword(v.newPassword),user.id]);
    return reply(res, 200, { ok: true });
  }
  if (pathname === '/api/state' && method === 'GET') {
    const current = today(), future = addMonths(current, 12);
    const accounts = (await rows(`SELECT a.id,a.name,a.type,a.opening_cents,
      a.opening_cents + COALESCE(SUM(CASE WHEN t.date>$1::date THEN 0 WHEN t.kind='income' THEN t.amount_cents ELSE -t.amount_cents END),0) AS balance_cents
      FROM accounts a LEFT JOIN transactions t ON t.account_id=a.id GROUP BY a.id ORDER BY a.id DESC`,[current])).map(normalizeMoney);
    const transactions = (await rows(`SELECT t.*,to_char(t.date,'YYYY-MM-DD') AS date,a.name AS account_name FROM transactions t JOIN accounts a ON a.id=t.account_id
      ORDER BY t.date DESC,t.id DESC LIMIT 500`)).map(normalizeMoney);
    const investments = (await rows("SELECT id,name,type,principal_cents,rate_basis_points,period,to_char(start_date,'YYYY-MM-DD') AS start_date FROM investments ORDER BY id DESC")).map(normalizeMoney).map(x => ({ ...x,
      current_cents: projectedCents(x.principal_cents, x.rate_basis_points, x.start_date, current, x.period),
      future_cents: projectedCents(x.principal_cents, x.rate_basis_points, x.start_date, future, x.period)
    }));
    const bills = await readBills(current);
    const investmentSeries = Array.from({length:13},(_,i) => {
      const date = addMonths(current,i);
      return { date, value_cents:investments.reduce((sum,x) => sum + (projectedCents(x.principal_cents,x.rate_basis_points,x.start_date,date,x.period) || 0),0) };
    });
    const debtSeries = Array.from({length:13},(_,i) => {
      const date = addMonths(current,i);
      return { date, value_cents:bills.reduce((sum,b) => sum + (debtCentsAt(b,b.payments,date) || 0),0) };
    });
    const dueSeries = Array.from({length:6},(_,i) => {
      const date = addMonths(current,i);
      const month = date.slice(0,7);
      const value = bills.filter(b => b.status !== 'paid' &&
        (i === 0 ? b.due_date.slice(0,7) <= month : b.due_date.slice(0,7) === month))
        .reduce((sum,b) => sum + (debtCentsAt(b,b.payments,b.due_date <= current ? current : b.due_date) || 0),0);
      return { date, value_cents:value };
    });
    const users = user.role === 'admin' ? (await rows('SELECT id,name,email,role,active FROM users ORDER BY id')).map(x => ({ ...x, id: Number(x.id) })) : [];
    return reply(res, 200, { user: publicUser(user), today:current, accounts, transactions, investments, bills,
      investment_series:investmentSeries, debt_series:debtSeries, due_series:dueSeries, users });
  }
  if (pathname === '/api/accounts' && method === 'POST') {
    const v = await readBody(req);
    const result = await row('INSERT INTO accounts(name,type,opening_cents) VALUES($1,$2,$3) RETURNING id', [string(v.name,'Nome'),string(v.type,'Tipo',60),money(v.openingCents,'Saldo inicial',true)]);
    return reply(res, 201, { id: Number(result.id) });
  }
  const account = pathname.match(/^\/api\/accounts\/(\d+)$/);
  if (account && method === 'PATCH') {
    const v = await readBody(req), result = await row('UPDATE accounts SET name=$1,type=$2,opening_cents=$3 WHERE id=$4 RETURNING id', [string(v.name,'Nome'),string(v.type,'Tipo',60),money(v.openingCents,'Saldo inicial',true),numberId(account[1])]);
    if (!result) throw new ApiError(404, 'Conta não encontrada.'); return reply(res, 200, { ok: true });
  }
  if (account && method === 'DELETE') {
    if (await row('SELECT id FROM transactions WHERE account_id=$1 LIMIT 1', [numberId(account[1])])) throw new ApiError(409, 'Exclua os lançamentos desta conta primeiro.');
    if (await row('SELECT id FROM bills WHERE account_id=$1 LIMIT 1', [numberId(account[1])])) throw new ApiError(409, 'Exclua as despesas planejadas desta conta primeiro.');
    if (!(await row('DELETE FROM accounts WHERE id=$1 RETURNING id', [numberId(account[1])]))) throw new ApiError(404, 'Conta não encontrada.');
    return reply(res, 200, { ok: true });
  }
  if (pathname === '/api/bills' && method === 'POST') {
    const v = await readBody(req), accountId = numberId(v.accountId);
    if (!(await row('SELECT id FROM accounts WHERE id=$1', [accountId]))) throw new ApiError(400, 'Conta inválida.');
    interestRate(v.interestRateBasisPoints,v.interestPeriod);
    const created = await row(`INSERT INTO bills(account_id,description,category,amount_cents,due_date,interest_rate_basis_points,interest_period)
      VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id`, [accountId,string(v.description,'Descrição'),string(v.category,'Categoria',60),money(v.amountCents,'Valor'),date(v.dueDate),v.interestRateBasisPoints,v.interestPeriod]);
    return reply(res, 201, { id:Number(created.id) });
  }
  const bill = pathname.match(/^\/api\/bills\/(\d+)$/);
  if (bill && method === 'PATCH') {
    const billId = numberId(bill[1]), v = await readBody(req), accountId = numberId(v.accountId);
    if (await row('SELECT id FROM transactions WHERE bill_id=$1 LIMIT 1', [billId])) throw new ApiError(409, 'Uma despesa com pagamentos não pode ser editada.');
    if (!(await row('SELECT id FROM accounts WHERE id=$1', [accountId]))) throw new ApiError(400, 'Conta inválida.');
    interestRate(v.interestRateBasisPoints,v.interestPeriod);
    const updated = await row(`UPDATE bills SET account_id=$1,description=$2,category=$3,amount_cents=$4,
      due_date=$5,interest_rate_basis_points=$6,interest_period=$7 WHERE id=$8 RETURNING id`,
      [accountId,string(v.description,'Descrição'),string(v.category,'Categoria',60),money(v.amountCents,'Valor'),date(v.dueDate),v.interestRateBasisPoints,v.interestPeriod,billId]);
    if (!updated) throw new ApiError(404, 'Despesa não encontrada.');
    return reply(res, 200, { ok:true });
  }
  if (bill && method === 'DELETE') {
    const billId = numberId(bill[1]);
    if (await row('SELECT id FROM transactions WHERE bill_id=$1 LIMIT 1', [billId])) throw new ApiError(409, 'Uma despesa com pagamentos não pode ser excluída.');
    if (!(await row('DELETE FROM bills WHERE id=$1 RETURNING id', [billId]))) throw new ApiError(404, 'Despesa não encontrada.');
    return reply(res, 200, { ok:true });
  }
  const billInterest = pathname.match(/^\/api\/bills\/(\d+)\/interest$/);
  if (billInterest && method === 'PATCH') {
    const billId = numberId(billInterest[1]), v = await readBody(req);
    interestRate(v.interestRateBasisPoints,v.interestPeriod);
    const currentBill = (await readBills(today())).find(x => x.id === billId);
    if (!currentBill) throw new ApiError(404, 'Despesa não encontrada.');
    if (currentBill.status === 'paid') throw new ApiError(409, 'Uma despesa quitada não pode ter juros alterados.');
    await sql('UPDATE bills SET interest_rate_basis_points=$1,interest_period=$2 WHERE id=$3', [v.interestRateBasisPoints,v.interestPeriod,billId]);
    return reply(res, 200, { ok:true });
  }
  const billPayments = pathname.match(/^\/api\/bills\/(\d+)\/payments$/);
  if (billPayments && method === 'POST') {
    const billId = numberId(billPayments[1]), v = await readBody(req), paymentDate = date(v.date), accountId = numberId(v.accountId);
    if (paymentDate > today()) throw new ApiError(400, 'Registre o pagamento na data em que ele acontecer.');
    const amount = money(v.amountCents,'Valor do pagamento');
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      const billResult = await client.query(`SELECT id,description,category,amount_cents,interest_rate_basis_points,
        interest_period,to_char(due_date,'YYYY-MM-DD') AS due_date FROM bills WHERE id=$1 FOR UPDATE`, [billId]);
      if (!billResult.rowCount) throw new ApiError(404, 'Despesa não encontrada.');
      const accountResult = await client.query('SELECT id FROM accounts WHERE id=$1', [accountId]);
      if (!accountResult.rowCount) throw new ApiError(400, 'Conta inválida.');
      const payments = (await client.query(`SELECT id,amount_cents,to_char(date,'YYYY-MM-DD') AS date
        FROM transactions WHERE bill_id=$1 ORDER BY date,id`, [billId])).rows.map(normalizeMoney);
      if (payments.length && paymentDate < payments.at(-1).date) throw new ApiError(400, 'A data deve ser igual ou posterior ao último pagamento.');
      const billRecord = normalizeMoney(billResult.rows[0]);
      const outstanding = debtCentsAt(billRecord,payments,paymentDate);
      if (outstanding === null) throw new ApiError(400, 'A projeção excedeu o limite de cálculo.');
      if (outstanding === 0) throw new ApiError(409, 'Esta despesa já está paga.');
      if (amount > outstanding) throw new ApiError(400, `Pagamento acima do saldo devido: R$ ${(outstanding/100).toFixed(2)}.`);
      const created = await client.query(`INSERT INTO transactions(account_id,bill_id,kind,description,category,amount_cents,date)
        VALUES($1,$2,'expense',$3,$4,$5,$6) RETURNING id`, [accountId,billId,`Pagamento: ${billRecord.description}`,billRecord.category,amount,paymentDate]);
      await client.query('COMMIT');
      return reply(res, 201, { id:Number(created.rows[0].id) });
    } catch (error) { await client.query('ROLLBACK'); throw error; } finally { client.release(); }
  }
  const billPayment = pathname.match(/^\/api\/bills\/(\d+)\/payments\/(\d+)$/);
  if (billPayment && method === 'DELETE') {
    const result = await row('DELETE FROM transactions WHERE bill_id=$1 AND id=$2 RETURNING id', [numberId(billPayment[1]),numberId(billPayment[2])]);
    if (!result) throw new ApiError(404, 'Pagamento não encontrado.');
    return reply(res, 200, { ok:true });
  }
  if (pathname === '/api/transactions' && method === 'POST') {
    const v = await readBody(req), accountId = numberId(v.accountId);
    if (!(await row('SELECT id FROM accounts WHERE id=$1', [accountId]))) throw new ApiError(400, 'Conta inválida.');
    if (!['income','expense'].includes(v.kind)) throw new ApiError(400, 'Tipo inválido.');
    const transactionDate = date(v.date);
    if (transactionDate > today()) throw new ApiError(400, 'Lançamentos imediatos não podem ter data futura. Use Despesas para contas a vencer.');
    const result = await row(`INSERT INTO transactions(account_id,kind,description,category,amount_cents,date)
      VALUES($1,$2,$3,$4,$5,$6) RETURNING id`, [accountId,v.kind,string(v.description,'Descrição'),string(v.category,'Categoria',60),money(v.amountCents,'Valor'),transactionDate]);
    return reply(res, 201, { id: Number(result.id) });
  }
  const transaction = pathname.match(/^\/api\/transactions\/(\d+)$/);
  if (transaction && method === 'DELETE') {
    if (await row('SELECT id FROM transactions WHERE id=$1 AND bill_id IS NOT NULL', [numberId(transaction[1])])) throw new ApiError(409, 'Exclua o pagamento na despesa correspondente.');
    if (!(await row('DELETE FROM transactions WHERE id=$1 RETURNING id', [numberId(transaction[1])]))) throw new ApiError(404, 'Lançamento não encontrado.');
    return reply(res, 200, { ok: true });
  }
  if (pathname === '/api/investments' && method === 'POST') {
    const v = await readBody(req);
    if (!['daily','monthly','yearly'].includes(v.period)) throw new ApiError(400, 'Periodicidade inválida.');
    if (!Number.isSafeInteger(v.rateBasisPoints) || v.rateBasisPoints < -9999 || v.rateBasisPoints > 100_000) throw new ApiError(400, 'Taxa inválida.');
    const result = await row(`INSERT INTO investments(name,type,principal_cents,rate_basis_points,period,start_date)
      VALUES($1,$2,$3,$4,$5,$6) RETURNING id`, [string(v.name,'Nome'),string(v.type,'Tipo',60),money(v.principalCents,'Valor inicial'),v.rateBasisPoints,v.period,date(v.startDate)]);
    return reply(res, 201, { id: Number(result.id) });
  }
  const investment = pathname.match(/^\/api\/investments\/(\d+)$/);
  if (investment && method === 'DELETE') {
    if (!(await row('DELETE FROM investments WHERE id=$1 RETURNING id', [numberId(investment[1])]))) throw new ApiError(404, 'Investimento não encontrado.');
    return reply(res, 200, { ok: true });
  }
  if (pathname === '/api/users' && method === 'POST') {
    await requireAdmin(req);
    const v = await readBody(req), result = await row("INSERT INTO users(name,email,password_hash,role) VALUES($1,$2,$3,'member') RETURNING id", [string(v.name,'Nome',80),email(v.email),hashPassword(v.password)]);
    return reply(res, 201, { id: Number(result.id) });
  }
  const reset = pathname.match(/^\/api\/users\/(\d+)\/password$/);
  if (reset && method === 'POST') {
    await requireAdmin(req);
    const userId = numberId(reset[1]), v = await readBody(req);
    if (!(await row("UPDATE users SET password_hash=$1 WHERE id=$2 AND role='member' RETURNING id", [hashPassword(v.password),userId]))) throw new ApiError(404, 'Usuário não encontrado.');
    await sql('DELETE FROM sessions WHERE user_id=$1', [userId]);
    return reply(res, 200, { ok: true });
  }
  const member = pathname.match(/^\/api\/users\/(\d+)$/);
  if (member && method === 'PATCH') {
    await requireAdmin(req);
    const userId = numberId(member[1]), v = await readBody(req);
    if (userId === Number(user.id)) throw new ApiError(400, 'Você não pode desativar sua própria conta.');
    if (typeof v.active !== 'boolean') throw new ApiError(400, 'Estado inválido.');
    if (!(await row("UPDATE users SET active=$1 WHERE id=$2 AND role='member' RETURNING id", [v.active,userId]))) throw new ApiError(404, 'Usuário não encontrado.');
    if (!v.active) await sql('DELETE FROM sessions WHERE user_id=$1', [userId]);
    return reply(res, 200, { ok: true });
  }
  throw new ApiError(404, 'Recurso não encontrado.');
}

export const server = http.createServer(async (req, res) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'same-origin');
  res.setHeader('X-Frame-Options', 'DENY');
  try {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/health' && req.method === 'GET') { await sql('SELECT 1'); return reply(res, 200, { ok: true, version: 2 }); }
    if (!pathname.startsWith('/api/')) throw new ApiError(404, 'Recurso não encontrado.');
    await api(req, res, pathname);
  } catch (error) {
    if (error.code === '23505') return reply(res, 409, { error: 'Registro já existente.' });
    if (error.code === '23503') return reply(res, 409, { error: 'Há dados associados a este registro.' });
    if (!(error instanceof ApiError)) console.error(error);
    reply(res, error.status || 500, { error: error.status ? error.message : 'Erro interno do servidor.' });
  }
});

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const schema = await readFile(new URL('./schema.sql', import.meta.url), 'utf8');
    await pool.query(schema);
    const host = process.env.HOST || '127.0.0.1', port = Number(process.env.PORT || 3000);
    server.listen(port, host, () => console.log(`API em http://${host}:${port}`));
  } catch (error) {
    const detail = error.message || error.errors?.map(item => `${item.code || 'erro'}: ${item.message}`).join('; ') || String(error);
    console.error('Falha ao iniciar o banco:', detail);
    process.exitCode = 1;
    await pool.end();
  }
}
