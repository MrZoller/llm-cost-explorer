// Dollars per million tokens throughout. Input, cache read, cache write, output
// are disjoint billing categories. No currency rounding until presentation.
export const PRESETS = {
  chat: { name: 'Everyday chat', kind: 'chat', calls: 5, context: 2000, growth: 800, output: 500, reasoning: 0, sessions: 8, hours: 2, perHour: 4 },
  research: { name: 'Chat with documents', kind: 'chat', calls: 10, context: 12000, growth: 2000, output: 1000, reasoning: 500, sessions: 4, hours: 2, perHour: 2 },
  small: { name: 'Focused coding fix', kind: 'agent', calls: 8, context: 12000, growth: 2000, output: 800, reasoning: 500, sessions: 6, hours: 3, perHour: 2 },
  feature: { name: 'Feature or multi-file bug fix', kind: 'agent', calls: 25, context: 24000, growth: 3500, output: 1500, reasoning: 1000, sessions: 3, hours: 3, perHour: 1 },
  large: { name: 'Extended coding task', kind: 'agent', calls: 60, context: 40000, growth: 5000, output: 2000, reasoning: 2000, sessions: 2, hours: 4, perHour: 0.5 },
};
export const DEFAULTS = { days: 20, week: 5, low: 0.5, high: 2, period: 'month', mode: 'daily' };
export function newGroup(preset = 'feature', modelId = '', index = 1) {
  return { people: 1, preset, modelId,
    ...PRESETS[preset], name: index === 1 ? 'Developers' : `Group ${index}`,
    cap: 128000, cacheHit: 60, cacheWrite: 25, retry: 10,
    measuredInput: 1000000, measuredRead: 600000, measuredWrite: 100000, measuredOutput: 100000,
    measuredDays: 1, measuredPeak: 32000, rateOverride: null };
}
export function finite(value, min = 0, max = 1e12) {
  return typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;
}
export function validateGroup(g, mode = 'daily') {
  const limits = { people: [1, 1000000], calls: [1, 10000], context: [0, 10000000], growth: [0, 10000000],
    output: [0, 10000000], reasoning: [0, 10000000], sessions: [0, 10000], hours: [0, 24], perHour: [0, 1000],
    cap: [1, 10000000], cacheHit: [0, 100], cacheWrite: [0, 100], retry: [0, 1000],
    measuredInput: [0, 1e12], measuredRead: [0, 1e12], measuredWrite: [0, 1e12], measuredOutput: [0, 1e12],
    measuredDays: [0.01, 366], measuredPeak: [0, 10000000] };
  for (const [k, [min, max]] of Object.entries(limits)) if (!finite(g[k], min, max)) throw new Error(`Check ${k}: enter a number from ${min} to ${max}.`);
  if (!Number.isInteger(g.people)) throw new Error('People must be a whole number.');
  if (mode === 'measured' && g.measuredRead + g.measuredWrite > g.measuredInput) throw new Error('Cached reads plus cache writes cannot exceed total input tokens.');
  if (mode !== 'measured' && g.context > g.cap) throw new Error('The context cap must be at least the starting context.');
}
export function priceTokens(tokens, model, peak = 0) {
  let rates = model;
  let longContext = false;
  if (model.longContext && peak > model.longContext.threshold) { rates = { ...model, ...model.longContext }; longContext = true; }
  if (!finite(rates.input) || !finite(rates.output)) throw new Error('This offering has no complete rate. Select another offering or enter custom rates.');
  if (['read', 'write'].some(k => rates[k] != null && !finite(rates[k]))) throw new Error('Enter valid, nonnegative cache rates.');
  const components = {
    input: tokens.input * rates.input / 1e6,
    read: tokens.read * (rates.read ?? rates.input) / 1e6,
    write: tokens.write * (rates.write ?? rates.input) / 1e6,
    output: tokens.output * rates.output / 1e6,
  };
  return { total: Object.values(components).reduce((a, b) => a + b, 0), components, longContext, rates };
}
export function workload(g, mode, factor = 1) {
  validateGroup(g, mode);
  if (mode === 'measured') {
    const f = factor / g.measuredDays;
    return { input: (g.measuredInput - g.measuredRead - g.measuredWrite) * f,
      read: g.measuredRead * f, write: g.measuredWrite * f, output: g.measuredOutput * f,
      calls: null, peak: g.measuredPeak, sessions: null, capped: false };
  }
  // Sensitivity scenarios scale call count; context grows with the longer task.
  // They are transparent what-if scenarios, not statistical confidence bounds.
  const calls = Math.max(1, Math.round(g.calls * factor));
  const count = mode === 'daily' ? g.hours * g.perHour : 1;
  const result = { input: 0, read: 0, write: 0, output: 0, calls: calls * count, peak: 0, sessions: count, capped: false };
  let previous = 0;
  for (let i = 0; i < calls; i++) {
    const raw = g.context + g.growth * i;
    const input = Math.min(raw, g.cap);
    // A cold first call cannot have a cache hit; only a reused prefix can hit.
    const read = Math.min(input, previous) * g.cacheHit / 100;
    const write = (input - read) * g.cacheWrite / 100;
    result.input += input - read - write;
    result.read += read;
    result.write += write;
    result.output += g.output + g.reasoning;
    result.peak = Math.max(result.peak, input);
    result.capped ||= raw > g.cap;
    previous = input;
  }
  const multiplier = count * (1 + g.retry / 100);
  for (const key of ['input', 'read', 'write', 'output']) result[key] *= multiplier;
  result.calls *= 1 + g.retry / 100;
  return result;
}
export function groupEstimate(g, model, settings, factor = 1) {
  const rates = g.rateOverride ? { ...g.rateOverride, name: 'Custom rates', sourceType: 'manual' } : model;
  if (!rates) throw new Error('The saved model is no longer in the catalog. Choose a current offering or enter custom rates.');
  const tokens = workload(g, settings.mode, factor);
  const price = priceTokens(tokens, rates, tokens.peak);
  const dailyScale = settings.mode === 'task' ? g.sessions : 1;
  const periods = { task: settings.mode === 'task' ? 1 : 0, day: dailyScale, week: dailyScale * settings.week, month: dailyScale * settings.days };
  return { ...price, tokens, rates, periods, perPerson: Object.fromEntries(Object.entries(periods).map(([key, f]) => [key, price.total * f])),
    team: Object.fromEntries(Object.entries(periods).map(([key, f]) => [key, price.total * f * g.people])) };
}
export function estimate(state, models) {
  if (!finite(state.days, 1, 31) || !finite(state.week, 1, 7) || !finite(state.low, 0.01, 1) || !finite(state.high, 1, 10)) throw new Error('Check workdays and scenario multipliers.');
  const scenarios = [state.low, 1, state.high].map(f => {
    const groups = state.groups.map(g => groupEstimate(g, models.find(m => m.id === g.modelId), state, f));
    const total = Object.fromEntries(['task', 'day', 'week', 'month'].map(p => [p, groups.reduce((sum, g) => sum + g.team[p], 0)]));
    return { groups, total };
  });
  return { scenarios, people: state.groups.reduce((sum, g) => sum + g.people, 0) };
}
export function encodeScenario(state) {
  const bytes = new TextEncoder().encode(JSON.stringify({ version: 1, ...state }));
  return btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}
export function decodeScenario(encoded) {
  if (encoded.length > 60000) throw new Error('This scenario link is too large.');
  const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(encoded.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0))));
  if (data.version !== 1 || !['task', 'daily', 'measured'].includes(data.mode) || !['day', 'week', 'month', 'task'].includes(data.period) || !Array.isArray(data.groups) || !data.groups.length || data.groups.length > 12) throw new Error('This scenario link has an unsupported format.');
  if (!finite(data.days, 1, 31) || !finite(data.week, 1, 7) || !finite(data.low, 0.01, 1) || !finite(data.high, 1, 10)) throw new Error('Invalid calendar or scenario range.');
  const groups = data.groups.map(g => {
    const clean = newGroup();
    for (const k of Object.keys(clean)) {
      if (k === 'rateOverride') continue;
      if (typeof clean[k] === 'number') clean[k] = g[k];
      else if (typeof clean[k] === 'string') clean[k] = typeof g[k] === 'string' ? g[k].slice(0, 300) : clean[k];
    }
    if (g.rateOverride) {
      clean.rateOverride = {};
      for (const k of ['input', 'read', 'write', 'output']) {
        if (!finite(g.rateOverride[k], 0, 1000000)) throw new Error('Invalid custom rate in shared scenario.');
        clean.rateOverride[k] = g.rateOverride[k];
      }
    }
    validateGroup(clean, data.mode);
    return clean;
  });
  return { mode: data.mode, period: data.period, days: data.days, week: data.week, low: data.low, high: data.high, groups };
}
