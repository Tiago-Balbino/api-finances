import test from 'node:test';
import assert from 'node:assert/strict';
import { fullPeriods, projectedCents, addMonths, debtCentsAt, billStatus } from '../finance.js';

test('juros mensais preservam a taxa nos aniversários', () => {
  assert.equal(projectedCents(100_000, 100, '2026-01-01', '2026-02-01', 'monthly'), 101_000);
  assert.equal(projectedCents(100_000, 100, '2026-01-01', '2027-01-01', 'monthly'), 112_683);
  assert.ok(projectedCents(100_000, 100, '2026-01-01', '2026-01-31', 'monthly') > 100_000);
});

test('investimento mensal acumula valor a cada dia', () => {
  const first = projectedCents(1_000_000, 100, '2026-01-01', '2026-01-02', 'monthly');
  const second = projectedCents(1_000_000, 100, '2026-01-01', '2026-01-03', 'monthly');
  assert.ok(first > 1_000_000);
  assert.ok(second > first);
});

test('dívida só rende após vencimento e aceita pagamento parcial', () => {
  const bill = { amount_cents: 10_000, due_date:'2026-10-10', interest_rate_basis_points:100, interest_period:'daily' };
  assert.equal(debtCentsAt(bill, [], '2026-10-09'), 10_000);
  assert.equal(billStatus(bill, [], '2026-10-09'), 'pending');
  assert.equal(debtCentsAt(bill, [], '2026-10-11'), 10_100);
  assert.equal(billStatus(bill, [], '2026-10-11'), 'overdue');
  const payments = [{id:1, date:'2026-10-11', amount_cents:4_000}];
  assert.equal(debtCentsAt(bill, payments, '2026-10-11'), 6_100);
  assert.equal(debtCentsAt(bill, payments, '2026-10-12'), 6_161);
  assert.equal(billStatus(bill, [...payments,{id:2,date:'2026-10-12',amount_cents:6_161}], '2026-10-12'), 'paid');
});

test('aniversários de fim de mês e datas futuras', () => {
  assert.equal(addMonths('2026-01-31', 1), '2026-02-28');
  assert.equal(fullPeriods('2026-01-31', '2026-02-28', 'monthly'), 1);
  assert.equal(fullPeriods('2027-01-01', '2026-01-01', 'yearly'), 0);
});
