import test from 'node:test';
import assert from 'node:assert/strict';
import * as t from '../src/textkit.mjs';

test('01 code', () => { assert.equal(t.code('abc'), 'CBA'); assert.equal(t.code('Hello'), 'OLLEH'); });
test('02 mix', () => { assert.equal(t.mix('abc', 'xyz'), 'axbycz'); assert.equal(t.mix('ab', 'wxyz'), 'awbxyz'); });
test('03 score', () => { assert.equal(t.score('abc'), 6); assert.equal(t.score('Zz'), 52); });
test('04 shift', () => { assert.equal(t.shift('abc'), 'def'); assert.equal(t.shift('xyz'), 'abc'); });
test('05 squash', () => { assert.equal(t.squash('banana'), 'bnn'); assert.equal(t.squash('AEIOUy'), 'y'); });
test('06 pairsum', () => { assert.deepEqual(t.pairsum([1, 2, 3, 4]), [3, 5, 7]); assert.deepEqual(t.pairsum([5]), []); });
test('07 rle', () => { assert.equal(t.rle('aaab'), 'a3b1'); assert.equal(t.rle(''), ''); });
test('08 stamp', () => { assert.equal(t.stamp(4), 'iv'); assert.equal(t.stamp(1994), 'mcmxciv'); });
test('09 tri', () => { assert.equal(t.tri(4), 10); assert.equal(t.tri(1), 1); });
test('10 mirror', () => { assert.equal(t.mirror('ab'), 'abba'); assert.equal(t.mirror(''), ''); });
