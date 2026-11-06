import test from 'node:test';
import assert from 'node:assert/strict';
import { parseCsv } from '../src/csv.mjs';
import { applyMovements } from '../src/stock.mjs';
import { lowStock } from '../src/report.mjs';

test('doubled quotes', () => assert.equal(parseCsv('a,b\n"say ""hi""",2')[0].a, 'say "hi"'));
test('quoted value keeps spaces, unquoted trimmed', () => assert.deepEqual(parseCsv('a,b\n" x ", y \n'), [{ a: ' x ', b: 'y' }]));
test('blank lines skipped', () => assert.equal(parseCsv('a\n1\n\n2\n\n').length, 2));
test('crlf tolerated', () => assert.deepEqual(parseCsv('a,b\r\n1,2\r\n'), [{ a: '1', b: '2' }]));
test('out below zero throws with sku', () => assert.throws(() => applyMovements(new Map([['Z9', 1]]), [{ sku: 'Z9', kind: 'out', qty: '2' }]), /Z9/));
test('bad kind throws with row number', () => assert.throws(() => applyMovements(new Map(), [{ sku: 'A', kind: 'in', qty: '1' }, { sku: 'A', kind: 'move', qty: '1' }]), /2/));
test('bad qty throws with row number', () => {
  for (const q of ['0', '-1', '1.5', 'x', '']) assert.throws(() => applyMovements(new Map(), [{ sku: 'A', kind: 'in', qty: q }]), /1/);
});
test('unknown sku starts at zero', () => assert.equal(applyMovements(new Map(), [{ sku: 'N', kind: 'in', qty: '3' }]).get('N'), 3));
test('returns a new map', () => { const t = new Map(); assert.notEqual(applyMovements(t, []), t); });
test('ties by sku', () => assert.deepEqual(lowStock(new Map([['b', 1], ['a', 1], ['c', 0]]), 1), ['c', 'a', 'b']));
test('threshold inclusive', () => assert.deepEqual(lowStock(new Map([['a', 3]]), 3), ['a']));
