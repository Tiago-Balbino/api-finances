const DAY = 86_400_000;
const utcDate = value => new Date(`${value}T12:00:00Z`);
export const daysBetween = (startDate, endDate) => Math.round((utcDate(endDate) - utcDate(startDate)) / DAY);

export function addMonths(date, count) {
  const d = utcDate(date);
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + count, 1, 12));
  target.setUTCDate(Math.min(d.getUTCDate(), new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate()));
  return target.toISOString().slice(0, 10);
}

export function fullPeriods(startDate, endDate, period) {
  if (endDate <= startDate) return 0;
  if (period === 'daily') return daysBetween(startDate, endDate);
  const step = period === 'yearly' ? 12 : 1;
  const a = utcDate(startDate), b = utcDate(endDate);
  let periods = Math.floor(((b.getUTCFullYear() - a.getUTCFullYear()) * 12 + b.getUTCMonth() - a.getUTCMonth()) / step);
  if (addMonths(startDate, periods * step) > endDate) periods--;
  return Math.max(0, periods);
}

export function fractionalPeriods(startDate, endDate, period) {
  if (endDate <= startDate) return 0;
  if (period === 'daily') return daysBetween(startDate, endDate);
  const full = fullPeriods(startDate, endDate, period);
  const step = period === 'yearly' ? 12 : 1;
  const last = addMonths(startDate, full * step);
  const next = addMonths(startDate, (full + 1) * step);
  return full + daysBetween(last, endDate) / daysBetween(last, next);
}

export function projectedCents(principalCents, rateBasisPoints, startDate, endDate, period) {
  const exponent = fractionalPeriods(startDate, endDate, period);
  const value = Math.round(principalCents * Math.pow(1 + rateBasisPoints / 10_000, exponent));
  return Number.isSafeInteger(value) ? value : null;
}

export function debtCentsAt(bill, payments, asOfDate) {
  let balance = bill.amount_cents;
  let last = bill.due_date;
  const ordered = [...payments].filter(x => x.date <= asOfDate).sort((a,b) => a.date.localeCompare(b.date) || a.id - b.id);
  for (const payment of ordered) {
    if (payment.date > bill.due_date && payment.date > last && balance > 0) {
      const exponent = fractionalPeriods(bill.due_date, payment.date, bill.interest_period) - fractionalPeriods(bill.due_date, last, bill.interest_period);
      balance = Math.round(balance * Math.pow(1 + bill.interest_rate_basis_points / 10_000, exponent));
    }
    balance = Math.max(0, balance - payment.amount_cents);
    if (payment.date > last) last = payment.date;
  }
  if (asOfDate > bill.due_date && asOfDate > last && balance > 0) {
    const exponent = fractionalPeriods(bill.due_date, asOfDate, bill.interest_period) - fractionalPeriods(bill.due_date, last, bill.interest_period);
    balance = Math.round(balance * Math.pow(1 + bill.interest_rate_basis_points / 10_000, exponent));
  }
  return Number.isSafeInteger(balance) ? balance : null;
}

export function billStatus(bill, payments, date) {
  const balance = debtCentsAt(bill, payments, date);
  return balance === 0 ? 'paid' : bill.due_date < date ? 'overdue' : 'pending';
}

export function todayInZone(timeZone = 'America/Araguaina') {
  return new Intl.DateTimeFormat('en-CA', { timeZone, year:'numeric', month:'2-digit', day:'2-digit' }).format(new Date());
}
