import test from 'node:test';
import assert from 'node:assert/strict';
import { parseCsv } from '../src/csv.mjs';
import { applyMovements } from '../src/stock.mjs';
import { lowStock } from '../src/report.mjs';

test('csv with a quoted comma', () => {
  assert.deepEqual(parseCsv('sku,kind,qty,note\nA1,in,5,"box, large"\n'), [{ sku: 'A1', kind: 'in', qty: '5', note: 'box, large' }]);
});
test('in and out', () => {
  const s = applyMovements(new Map([['A1', 10]]), [{ sku: 'A1', kind: 'out', qty: '3' }, { sku: 'B2', kind: 'in', qty: '4' }]);
  assert.equal(s.get('A1'), 7);
  assert.equal(s.get('B2'), 4);
});
test('input table not modified', () => {
  const t = new Map([['A1', 1]]);
  applyMovements(t, [{ sku: 'A1', kind: 'in', qty: '1' }]);
  assert.equal(t.get('A1'), 1);
});
test('low stock at or below, by quantity', () => {
  assert.deepEqual(lowStock(new Map([['C', 2], ['A', 5], ['B', 2], ['D', 9]]), 5), ['B', 'C', 'A']);
});
