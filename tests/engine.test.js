import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULTS, newGroup, workload, priceTokens, estimate, encodeScenario, decodeScenario } from '../site/engine.js';

const model = { id: 'fixture', input: 2, read: 0.2, write: 2.5, output: 10 };
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);
const group = () => ({ ...newGroup('small', 'fixture'), calls: 2, context: 1000, growth: 1000, cap: 100000, output: 100, reasoning: 50, cacheHit: 50, cacheWrite: 25, retry: 0, sessions: 3, hours: 2, perHour: 1.5 });

test('cold call, repeated prefix, writes, and billed reasoning are disjoint', () => {
  const t = workload(group(), 'task');
  assert.deepEqual([t.input, t.read, t.write, t.output], [1875, 500, 625, 300]);
  close(t.input + t.read + t.write, 3000);
  close(priceTokens(t, model).total, 0.0084125);
});
test('daily active hours and per-task model share give identical token cost', () => {
  const g = group();
  const task = estimate({ ...DEFAULTS, mode: 'task', groups: [g] }, [model]);
  const daily = estimate({ ...DEFAULTS, mode: 'daily', groups: [g] }, [model]);
  close(task.scenarios[1].total.day, daily.scenarios[1].total.day);
  close(daily.scenarios[1].total.month, 0.0084125 * 3 * 20);
});
test('mixed groups scale their own usage and headcount, not an average workload', () => {
  const a = { ...group(), people: 5 }, b = { ...group(), people: 15, perHour: 0.25 };
  const r = estimate({ ...DEFAULTS, groups: [a, b] }, [model]);
  assert.equal(r.people, 20);
  close(r.scenarios[1].total.day, 0.0084125 * (5 * 3 + 15 * 0.5));
});
test('measured input includes cached tokens; output already includes reasoning', () => {
  const g = { ...group(), measuredInput: 1000000, measuredRead: 600000, measuredWrite: 100000, measuredOutput: 100000, measuredDays: 2 };
  const r = estimate({ ...DEFAULTS, mode: 'measured', groups: [g] }, [model]);
  close(r.scenarios[1].total.day, (0.6 + 0.12 + 0.25 + 1) / 2);
  close(r.scenarios[0].total.day, r.scenarios[1].total.day * 0.5);
});
test('cache parts exceeding total input are rejected instead of negative billing', () => {
  assert.throws(() => workload({ ...group(), measuredInput: 1 }, 'measured'), /cannot exceed/);
});
test('unlisted cache rates use regular input rate; zero is a valid listed rate', () => {
  close(priceTokens({ input: 1e6, read: 1e6, write: 1e6, output: 0 }, { input: 2, output: 10 }).total, 6);
  close(priceTokens({ input: 1e6, read: 1e6, write: 1e6, output: 0 }, { input: 2, output: 10, read: 0, write: 0 }).total, 2);
});
test('long-context boundary uses the published threshold and full-task premium', () => {
  const m = { ...model, longContext: { threshold: 272000, input: 4, read: 0.4, write: 5, output: 15 } };
  const tokens = { input: 1e6, read: 0, write: 0, output: 1e6 };
  close(priceTokens(tokens, m, 272000).total, 12);
  close(priceTokens(tokens, m, 272001).total, 19);
});
test('context cap and retries are applied without dropping tokens', () => {
  const t = workload({ ...group(), calls: 3, cap: 1500, retry: 10 }, 'task');
  close(t.input + t.read + t.write, 4400);
  close(t.output, 495);
  assert.equal(t.capped, true);
});
test('scenario calls generate an ordered sensitivity range', () => {
  const r = estimate({ ...DEFAULTS, groups: [group()] }, [model]);
  assert.ok(r.scenarios[0].total.month < r.scenarios[1].total.month);
  assert.ok(r.scenarios[1].total.month < r.scenarios[2].total.month);
});
test('a Unicode scenario and manual rates survive sharing', () => {
  const state = { ...DEFAULTS, groups: [{ ...group(), name: 'Équipe 🧮', rateOverride: { input: 0, read: 0, write: 0, output: 0 } }] };
  assert.deepEqual(decodeScenario(encodeScenario(state)), state);
});
test('shared scenarios reject malformed bounds, missing fields, and unsafe rate values', () => {
  for (const state of [{ ...DEFAULTS, groups: [] }, { ...DEFAULTS, days: -1, groups: [group()] }, { ...DEFAULTS, groups: [{ ...group(), cacheHit: 101 }] }, { ...DEFAULTS, groups: [{ ...group(), rateOverride: { input: -1 } }] }]) {
    assert.throws(() => decodeScenario(encodeScenario(state)));
  }
  assert.throws(() => priceTokens({ input: 1, read: 1, write: 1, output: 1 }, { ...model, read: NaN }), /cache rates/);
});
test('missing offerings cannot silently produce a zero-cost estimate', () => {
  assert.throws(() => estimate({ ...DEFAULTS, groups: [group()] }, []), /no longer/);
});
