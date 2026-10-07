import { PRESETS, DEFAULTS, newGroup, estimate, encodeScenario, decodeScenario } from './engine.js';

const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const money = (value, precise = false) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: precise ? 4 : value < 10 ? 2 : 0 }).format(value);
const num = value => new Intl.NumberFormat('en-US', { maximumFractionDigits: 1, notation: value >= 10000 ? 'compact' : 'standard' }).format(value);
const date = value => value ? new Date(value).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : 'Never';
const url = value => typeof value === 'string' && value.startsWith('https://') ? esc(value) : '#';
const plural = (n, singular, multiple = singular + 's') => n === 1 ? singular : multiple;
let catalog, state, currentResult;
let activeGroups = [];

function toast(message, error = false) {
  $('#notice').innerHTML = `<div class="${error ? 'error-box' : 'toast'}">${esc(message)}</div>`;
}
function options(items, selected) {
  return items.map(item => {
    const [value, label] = Array.isArray(item) ? item : [item, item];
    return `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(label)}</option>`;
  }).join('');
}
function field(label, key, value, i, { min = 0, max = 1e12, step = 1, hint = '', type = 'number' } = {}) {
  return `<label class="field"><span>${label}</span><input type="${type}" data-group="${i}" data-key="${key}" value="${esc(value)}" ${type === 'number' ? `min="${min}" max="${max}" step="${step}"` : 'maxlength="80"'}>${hint ? `<span class="help">${hint}</span>` : ''}</label>`;
}
function select(label, key, value, items, i, hint = '') {
  return `<label class="field"><span>${label}</span><select data-group="${i}" data-key="${key}">${options(items, value)}</select>${hint ? `<span class="help">${hint}</span>` : ''}</label>`;
}
const providers = () => [...new Set(catalog.models.map(m => m.provider))].sort((a, b) => {
  const order = ['Azure Government', 'AWS GovCloud', 'Azure', 'AWS Bedrock', 'OpenAI', 'Anthropic'];
  return (order.includes(a) ? order.indexOf(a) : 99) - (order.includes(b) ? order.indexOf(b) : 99) || a.localeCompare(b);
});
function preferred(models) {
  return models.find(m => m.name === 'GPT-4.1' && m.region === 'usgovvirginia' && m.deployment === 'Regional') || models.find(m => m.name === 'Claude Sonnet 4.5') || models[0];
}
function offering(g) { return catalog.models.find(m => m.id === g.modelId); }
function rateStrip(g) {
  const m = offering(g), r = g.rateOverride || m;
  if (!r) return '<p class="warning">This saved offering is no longer listed. Choose a current offering or enter custom rates.</p>';
  return `<div class="price-strip">${[['input', 'Fresh input'], ['read', 'Cache read'], ['write', 'Cache write'], ['output', 'Output']].map(([k, label]) => `<div><span>${label}</span><strong>${r[k] == null ? 'Not listed' : money(r[k], true)}</strong></div>`).join('')}</div><p class="rate-foot">USD per 1M tokens${g.rateOverride ? ' · Custom rates' : ' · Standard on-demand'}${r.longContext ? ` · Higher rates above ${num(r.longContext.threshold)} input tokens applied automatically` : ''}</p>`;
}

function groupCard(g, i) {
  const m = offering(g);
  const provider = m?.provider || activeGroups[i]?.provider || providers()[0];
  const regions = [...new Set(catalog.models.filter(m => m.provider === provider).map(m => m.region))];
  const region = m?.region || activeGroups[i]?.region || regions[0];
  activeGroups[i] = { provider, region };
  const models = catalog.models.filter(m => m.provider === provider && m.region === region);
  const modelOptions = models.map(m => [m.id, `${m.name} · ${m.deployment}`]);
  if (!m) modelOptions.unshift([g.modelId, 'Saved offering unavailable — choose a replacement']);
  const usageFields = state.mode === 'measured'
    ? `<p class="preset-note">Enter totals <strong>per person</strong> over the measured active workdays. Total input includes cache reads and cache writes. Output already includes billed reasoning.</p>
       <div class="row">${field('Total input tokens', 'measuredInput', g.measuredInput, i)}${field('Total output tokens', 'measuredOutput', g.measuredOutput, i)}</div>
       <div class="row">${field('Of input: cached reads', 'measuredRead', g.measuredRead, i)}${field('Of input: cache writes', 'measuredWrite', g.measuredWrite, i)}</div>
       <div class="row">${field('Measured active workdays', 'measuredDays', g.measuredDays, i, { min: 0.01, max: 366, step: 0.5 })}${field('Largest input in one call', 'measuredPeak', g.measuredPeak, i, { max: 10000000, hint: 'Used to check context pricing tiers.' })}</div>`
    : `${select('Starting workload', 'preset', g.preset, Object.entries(PRESETS).map(([key, p]) => [key, `${p.kind === 'chat' ? 'Chat' : 'Agent'} · ${p.name}`]), i)}
       <p class="preset-note">Illustrative starting assumptions. Choosing a workload resets its call, context, output, and frequency settings.</p>
       ${state.mode === 'daily' ? `<div class="row">${field('Active AI hours / day', 'hours', g.hours, i, { max: 24, step: 0.5, hint: 'Exclude time spent away from the assistant.' })}${field('Tasks or chats / active hour', 'perHour', g.perHour, i, { max: 1000, step: 0.5, hint: 'A task includes all of its model calls.' })}</div>` : `<div class="row">${field('Tasks or chats / day', 'sessions', g.sessions, i, { max: 10000, step: 0.5, hint: 'Used when scaling a task to a workday.' })}${field('Model calls / task', 'calls', g.calls, i, { min: 1, max: 10000 })}</div>`}
       <details data-detail="assumptions-${i}"><summary>Adjust context, calls, caching & retries</summary><div class="details-body">
       <p class="help">A call is one model request, including requests made after tool results. Token counts cover instructions, code, history, and tools—not your entire repository.</p>
       <div class="row">${field('Model calls / task', 'calls', g.calls, i, { min: 1, max: 10000 })}${field('Starting context (tokens)', 'context', g.context, i, { max: 10000000, step: 1000 })}</div>
       <div class="row">${field('Context growth / call', 'growth', g.growth, i, { max: 10000000, step: 500, hint: 'Retained replies, new files, and tool results.' })}${field('Context cap (tokens)', 'cap', g.cap, i, { min: 1, max: 10000000, step: 1000, hint: 'Approximate context after truncation/compaction.' })}</div>
       <div class="row">${field('Visible output / call', 'output', g.output, i, { max: 10000000, step: 100 })}${field('Billed reasoning / call', 'reasoning', g.reasoning, i, { max: 10000000, step: 100, hint: 'Set to 0 for non-reasoning models.' })}</div>
       <div class="row">${field('Reused prefix cache hit (%)', 'cacheHit', g.cacheHit, i, { max: 100, step: 5, hint: 'First call is cold. Cache hits require a matching prefix.' })}${field('Cache writes (% of misses)', 'cacheWrite', g.cacheWrite, i, { max: 100, step: 5, hint: 'Share of non-cached input written to a cache.' })}</div>
       ${field('Retry / extra-work overhead (%)', 'retry', g.retry, i, { max: 1000, step: 5, hint: 'Adds proportional tokens. Include rework and unmodeled extra calls.' })}
       </div></details>`;
  return `<article class="group-card" data-card="${i}"><div class="group-head"><span class="group-icon" aria-hidden="true">▤</span><input aria-label="Group ${i + 1} name" data-group="${i}" data-key="name" value="${esc(g.name)}" maxlength="80">${state.groups.length > 1 ? `<button data-action="remove" data-index="${i}" aria-label="Remove ${esc(g.name)}">Remove</button>` : ''}</div><div class="group-body">
    <div class="row">${field('People in this group', 'people', g.people, i, { min: 1, max: 1000000 })}${select('Provider / cloud', 'provider', provider, providers(), i)}</div>
    ${select('Region / scope', 'region', region, regions, i)}
    ${select('Model & deployment', 'modelId', g.modelId, modelOptions, i)}
    <div class="offering-label"><span class="badge ${g.rateOverride ? 'manual' : m?.sourceType === 'secondary' ? 'secondary' : ''}">${g.rateOverride ? 'Manual override' : m?.sourceType === 'secondary' ? 'Secondary source' : 'Official source'}</span>${m ? `<a href="${url(m.source)}" target="_blank" rel="noopener">View price source</a><span>Checked ${date(m.checkedAt)}</span>` : ''}</div>
    <div id="rates-${i}">${rateStrip(g)}</div>
    <hr class="divider">${usageFields}
    <details data-detail="override-${i}" ${g.rateOverride ? 'open' : ''}><summary>Use your own rates</summary><div class="details-body"><label class="check-field"><input type="checkbox" data-group="${i}" data-key="custom" ${g.rateOverride ? 'checked' : ''}>Override with negotiated or unlisted rates</label><p class="help">All rates are USD per 1M tokens. Custom rates apply to every context length. A missing cache discount should use the input rate.</p><div class="row">${['input', 'output', 'read', 'write'].map(k => field(`${{ input: 'Fresh input', output: 'Output', read: 'Cache read', write: 'Cache write' }[k]} rate`, `rate-${k}`, g.rateOverride?.[k] ?? m?.[k] ?? m?.input ?? 0, i, { max: 1000000, step: 0.01 }).replace('<input ', g.rateOverride ? '<input ' : '<input disabled ')).join('')}</div></div></details>
  </div></article>`;
}

function render() {
  const open = new Set([...document.querySelectorAll('details[open][data-detail]')].map(d => d.dataset.detail));
  const descriptions = { task: 'Start with one chat or coding task, then scale it across your team.', daily: 'Describe an active day. Hours become tasks, and tasks become model calls.', measured: 'Use token totals from your own tools to replace illustrative workload assumptions.' };
  $('#app').innerHTML = `<div class="layout"><section class="panel" aria-labelledby="workload-title"><div class="panel-header"><h2 class="panel-title" id="workload-title"><span class="section-no">1</span> Describe your workload</h2></div>
    <div class="mode-tabs" role="group" aria-label="Estimation method">${[['task', 'By task'], ['daily', 'Daily usage'], ['measured', 'Measured usage']].map(([mode, text]) => `<button data-mode="${mode}" aria-pressed="${state.mode === mode}">${text}</button>`).join('')}</div>
    <p class="mode-description">${descriptions[state.mode]}</p><div class="groups">${state.groups.map(groupCard).join('')}<button class="add-group" data-action="add" ${state.groups.length >= 12 ? 'disabled' : ''}>+ Add a group with a different workload</button></div>
    <div class="calendar"><div class="row"><label class="field"><span>Active workdays / month</span><input data-setting="days" type="number" min="1" max="31" value="${state.days}"></label><label class="field"><span>Active workdays / week</span><input data-setting="week" type="number" min="1" max="7" value="${state.week}"></label></div><p>Usage scales by active workdays, not calendar days.</p><details data-detail="range"><summary style="margin-top:14px">Adjust the scenario range</summary><div class="details-body"><div class="row"><label class="field"><span>Lighter workload multiplier</span><input data-setting="low" type="number" min="0.01" max="1" step="0.1" value="${state.low}"></label><label class="field"><span>Heavier workload multiplier</span><input data-setting="high" type="number" min="1" max="10" step="0.1" value="${state.high}"></label></div><p class="help">${state.mode === 'measured' ? 'Scales measured daily token totals.' : 'Scales calls per task. More calls also mean more repeated context; task frequency stays the same.'} These scenarios are not a maximum bill.</p></div></details></div>
    </section><aside class="results" id="results" aria-label="Estimated costs" aria-live="polite" aria-atomic="true"></aside></div><section class="print-summary" id="print-summary"></section>`;
  for (const d of document.querySelectorAll('details[data-detail]')) if (open.has(d.dataset.detail)) d.open = true;
  updateResults();
}

function totalsForPeriod(result, period) {
  const components = { input: 0, read: 0, write: 0, output: 0 }, tokens = { ...components };
  result.scenarios[1].groups.forEach((r, i) => {
    const f = state.groups[i].people * r.periods[period];
    for (const key of Object.keys(components)) { components[key] += r.components[key] * f; tokens[key] += r.tokens[key] * f; }
  });
  return { components, tokens };
}
function warnings(result) {
  const messages = new Set();
  result.scenarios.forEach((s, si) => s.groups.forEach((r, i) => {
    const g = state.groups[i], m = offering(g), label = g.name || `Group ${i + 1}`;
    if (r.longContext) messages.add(`${label}: long-context prices apply in the ${['lighter', 'central', 'heavier'][si]} scenario (conservatively to the full task or measured period).`);
    if (r.tokens.capped && si === 1) messages.add(`${label}: context reaches its cap. Extra compaction calls are not modeled separately; include them in overhead.`);
    if (r.tokens.read > 0 && r.rates.read == null) messages.add(`${label}: no cache-read price is listed. Those tokens are charged at the full input rate in this estimate.`);
    if (r.tokens.write > 0 && r.rates.write == null) messages.add(`${label}: no separate cache-write rate is listed. These tokens use the regular input rate.`);
    if (m?.contextLimit && r.tokens.peak > m.contextLimit && !g.rateOverride) messages.add(`${label}: estimated input exceeds the catalog’s context limit. Reduce context or select a different model.`);
    if (m?.band && !g.rateOverride) messages.add(`${label}: you selected ${m.band.toLowerCase()} meters. Confirm this band matches your deployment; Azure band selection is manual.`);
    if (m?.note && si === 1 && !g.rateOverride) messages.add(`${label}: ${m.note}`);
    if (m && !g.rateOverride && (Date.now() - Date.parse(m.checkedAt) > 3 * 86400000 || catalog.sources.find(s => s.key === m.sourceKey)?.status !== 'ok')) messages.add(`${label}: these rates are stale or their latest refresh failed. Last successful check: ${date(m.checkedAt)}.`);
    if (state.mode !== 'measured' && r.tokens.peak > 200000 && !r.rates.longContext && !m?.band && !g.rateOverride) messages.add(`${label}: this workload exceeds 200K input tokens per call. Check the provider for context limits or premiums not represented in this catalog.`);
  }));
  return [...messages];
}
function updateResults() {
  const target = $('#results');
  try {
    currentResult = estimate(state, catalog.models);
    $('#share').disabled = false; $('#print').disabled = false;
    const result = currentResult;
    const period = state.period, central = result.scenarios[1].total[period];
    const { components, tokens } = totalsForPeriod(result, period);
    const periods = [['day', 'Day'], ['week', 'Week'], ['month', 'Month']];
    if (state.mode === 'task') periods.unshift(['task', 'Task']);
    const labels = { task: 'one task per person', day: 'active workday', week: `${state.week}-day workweek`, month: `${state.days} active days / month` };
    const cacheCost = components.read + components.write;
    const repeat = result.scenarios[1].groups.reduce((sum, r, i) => sum + (r.tokens.calls || 0) * r.periods[period] * state.groups[i].people, 0);
    const messages = warnings(result);
    target.innerHTML = `<div class="estimate-hero"><div class="estimate-top"><p class="eyebrow">YOUR TEAM’S TOKEN COST</p><div class="period-switch" role="group" aria-label="Cost period">${periods.map(([p, name]) => `<button data-period="${p}" class="${p === period ? 'active' : ''}" aria-pressed="${p === period}">${name}</button>`).join('')}</div></div><div class="big-number">${money(central)}</div><p class="estimate-caption">Central scenario · <strong>${num(result.people)} ${plural(result.people, 'person', 'people')}</strong> · ${labels[period]}</p><div class="range-cards">${['Lighter', 'Central', 'Heavier'].map((name, i) => `<div class="range-card ${i === 1 ? 'central' : ''}"><span>${name}</span><strong>${money(result.scenarios[i].total[period])}</strong></div>`).join('')}</div><p class="range-note">A planning range, not a spending cap or a prediction.</p></div>
      <div class="panel result-detail"><h3>Where the spend goes</h3><div class="breakdown-bar" role="img" aria-label="Cost split across fresh input, cache reads, cache writes, and output">${Object.entries(components).map(([key, value]) => `<span class="segment ${key}" style="width:${central ? value / central * 100 : 0}%"></span>`).join('')}</div>${Object.entries({ input: 'Fresh input', read: 'Cache reads', write: 'Cache writes', output: 'Output + reasoning' }).map(([key, name]) => `<div class="cost-row"><span class="swatch ${key}" aria-hidden="true"></span><span>${name}</span><span class="token-count">${num(tokens[key])} tok</span><span class="cost">${money(components[key])}</span></div>`).join('')}<p class="help">${state.mode === 'measured' ? 'Based on your measured token totals.' : `About ${num(repeat)} model calls across the team in this period.`} Cache costs: ${money(cacheCost)}.</p></div>
      <div class="panel result-detail"><h3>Budget at a glance</h3><table class="summary-table"><thead><tr><th scope="col">Period</th><th scope="col">Per person, avg.</th><th scope="col">Whole team</th></tr></thead><tbody>${[['day', 'Active day'], ['week', `${state.week}-day week`], ['month', `${state.days}-day month`]].map(([p, name]) => `<tr><td>${name}</td><td>${money(result.scenarios[1].total[p] / result.people)}</td><td>${money(result.scenarios[1].total[p])}</td></tr>`).join('')}</tbody></table>${state.groups.length > 1 ? `<hr class="divider"><table class="summary-table"><thead><tr><th>Group</th><th>${esc(period)} total</th></tr></thead><tbody>${result.scenarios[1].groups.map((r, i) => `<tr><td>${esc(state.groups[i].name)}<small>${state.groups[i].people} ${plural(state.groups[i].people, 'person', 'people')} · ${esc(PRESETS[state.groups[i].preset]?.kind || 'custom')}</small></td><td>${money(r.team[period])}</td></tr>`).join('')}</tbody></table>` : ''}</div>
      <div class="insight"><p><strong>Context is often the cost driver.</strong> Files and conversation history may be billed again on every call. Caching and shorter tasks can change the budget substantially.</p></div><div class="warnings">${messages.map(message => `<p class="warning">${esc(message)}</p>`).join('')}</div><div class="snapshot-date"><span>Catalog refreshed ${date(catalog.generatedAt)}</span><a href="#pricing">View sources</a></div>`;
    $('#print-summary').innerHTML = printSummary(result, messages);
  } catch (error) {
    currentResult = null;
    target.innerHTML = `<div class="error-box" role="alert"><strong>Check your inputs</strong><p>${esc(error.message)}</p></div>`;
    $('#print-summary').innerHTML = `<p>Estimate unavailable: ${esc(error.message)}</p>`;
    $('#share').disabled = true; $('#print').disabled = true;
  }
}
function printSummary(result, messages) {
  const central = result.scenarios[1], p = state.period;
  return `<p>Prepared ${date(new Date().toISOString())} · ${esc(state.mode)} mode · ${result.people} ${plural(result.people, 'person', 'people')} · ${state.days} active days/month · ${state.week} days/week</p><p class="print-total">${money(central.total[p])} / ${p === 'task' ? 'one task per person' : p}</p><p>Lighter: ${money(result.scenarios[0].total[p])} · Central: ${money(central.total[p])} · Heavier: ${money(result.scenarios[2].total[p])}</p><table><thead><tr><th>Period</th><th>Lighter</th><th>Central</th><th>Heavier</th></tr></thead><tbody>${['day', 'week', 'month'].map(period => `<tr><td>${period}</td>${result.scenarios.map(s => `<td>${money(s.total[period])}</td>`).join('')}</tr>`).join('')}</tbody></table><h2>Workload and rate assumptions</h2>${state.groups.map((g, i) => {
    const m = offering(g), r = central.groups[i], rates = r.rates;
    return `<div class="print-group"><h3>${esc(g.name)} · ${g.people} ${plural(g.people, 'person', 'people')}</h3><p>${esc(m?.provider)} · ${esc(m?.region)} · ${esc(m?.name)} · ${esc(m?.deployment)}${g.rateOverride ? ' · MANUAL RATES' : ''}</p><p>${state.mode === 'measured' ? `${g.measuredInput.toLocaleString()} total input tokens (including ${g.measuredRead.toLocaleString()} cache reads and ${g.measuredWrite.toLocaleString()} cache writes), ${g.measuredOutput.toLocaleString()} output tokens per person over ${g.measuredDays} active days. Peak input: ${g.measuredPeak.toLocaleString()}.` : `${esc(PRESETS[g.preset]?.name || 'Custom workload')}: ${g.calls} calls/task; ${state.mode === 'daily' ? `${g.hours} active hours/day × ${g.perHour} tasks/hour` : `${g.sessions} tasks/day`}. Starting context ${num(g.context)}, growth ${num(g.growth)}/call, cap ${num(g.cap)}. Output ${num(g.output)} + reasoning ${num(g.reasoning)}/call. Reused prefix hit rate ${g.cacheHit}%; cache writes ${g.cacheWrite}% of misses; retry overhead ${g.retry}%.`}</p><p>Central-scenario USD/1M tokens: input ${money(rates.input, true)}, read ${money(rates.read ?? rates.input, true)}, write ${money(rates.write ?? rates.input, true)}, output ${money(rates.output, true)}.${r.longContext ? ' Long-context rates apply to this central scenario.' : ''}${!g.rateOverride && m?.longContext ? ` Published rates above ${num(m.longContext.threshold)} input tokens: input ${money(m.longContext.input, true)}, read ${money(m.longContext.read ?? m.longContext.input, true)}, write ${money(m.longContext.write ?? m.longContext.input, true)}, output ${money(m.longContext.output, true)}.` : ''}</p><p>Group totals: ${money(r.team.day)}/day · ${money(r.team.week)}/week · ${money(r.team.month)}/month.</p><p class="print-source">${g.rateOverride ? 'Custom rates entered by the scenario author.' : `${esc(m.sourceType)} source, checked ${date(m.checkedAt)}: <a href="${url(m.source)}">${esc(m.source)}</a>`}</p></div>`;
  }).join('')}<p>Range assumptions: ${state.low}× and ${state.high}× ${state.mode === 'measured' ? 'token volume' : 'calls per task, with context growth'}. No statistical confidence is implied.</p>${messages.map(m => `<p>${esc(m)}</p>`).join('')}<p class="print-note"><strong>Scope:</strong> text/code model token costs only, in USD. Excludes subscriptions, tool/search fees, infrastructure, storage, retrieval, embeddings, taxes, and negotiated discounts unless manually entered in token rates. Presets are illustrative assumptions, not measured benchmarks. This is not a spending cap. Published rates do not establish deployment access or government authorization.</p><p class="print-source">Formula and methodology: https://MrZoller.github.io/llm-cost-explorer/</p>`;
}

function renderSources() {
  const secondary = catalog.models.filter(m => m.sourceType === 'secondary').length;
  $('#sources').innerHTML = `<p class="source-intro">${catalog.models.length.toLocaleString()} text/code offerings across ${providers().length} providers and cloud environments. AWS and Azure prices come from official regional feeds; OpenAI and Anthropic prices come from official price tables. ${secondary} additional direct-provider offerings use the community-maintained LiteLLM catalog and are labeled. All rates are USD per million tokens.</p><p class="source-intro">Daily refresh is scheduled in GitHub Actions. Each source keeps its own last-successful-check timestamp; failures retain older data with a warning. Coverage is intentionally limited to supported token meters: Azure Virginia/Arizona government and East US/West US commercial regions; AWS US East/West commercial and GovCloud regions. Other regions or missing models can use manual rates. Only standard on-demand text rates are imported; batch, provisioned, priority, and non-token fees are excluded. Price listings are not an availability catalog.</p><div class="source-table-wrap"><table class="source-table"><thead><tr><th scope="col">Source</th><th scope="col">Offerings</th><th scope="col">Last successful check</th><th scope="col">Status</th></tr></thead><tbody>${catalog.sources.map(s => `<tr><td><a href="${url(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a></td><td>${s.count}</td><td>${date(s.checkedAt)}</td><td>${s.status === 'ok' ? '<span class="badge">Fetched</span>' : `<span class="badge secondary">${esc(s.status)}</span><details><summary>Details</summary>${esc(s.error)}</details>`}</td></tr>`).join('')}</tbody></table></div><p class="source-intro" style="margin-top:14px">A successful fetch means the source was retrieved and parsed, not independently audited against a bill. Shared scenario links use the latest catalog when opened; print a summary to preserve the rates used today. <a href="data/prices.json">Download the catalog</a> · <a href="https://github.com/MrZoller/llm-cost-explorer/actions">Refresh history</a></p>`;
}

$('#app').addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.dataset.mode) { state.mode = button.dataset.mode; if (state.mode === 'task') state.period = 'task'; else if (state.period === 'task') state.period = 'month'; render(); }
  if (button.dataset.period) { state.period = button.dataset.period; updateResults(); }
  if (button.dataset.action === 'add' && state.groups.length < 12) { state.groups.push(newGroup('chat', state.groups[0].modelId, state.groups.length + 1)); render(); }
  if (button.dataset.action === 'remove') { state.groups.splice(Number(button.dataset.index), 1); activeGroups.splice(Number(button.dataset.index), 1); render(); }
});
$('#app').addEventListener('input', event => {
  const el = event.target;
  if (el.tagName === 'SELECT' || el.type === 'checkbox') return;
  if (el.dataset.setting) { state[el.dataset.setting] = el.valueAsNumber; updateResults(); return; }
  if (el.dataset.group == null) return;
  const i = Number(el.dataset.group), g = state.groups[i], key = el.dataset.key;
  if (key.startsWith('rate-')) {
    if (!g.rateOverride) return;
    g.rateOverride[key.slice(5)] = el.valueAsNumber;
    $(`#rates-${i}`).innerHTML = rateStrip(g);
  } else {
    g[key] = el.type === 'number' ? el.valueAsNumber : el.value;
    // Calls appears in the task summary and expanded assumptions; keep in sync.
    document.querySelectorAll(`[data-group="${i}"][data-key="${key}"]`).forEach(peer => { if (peer !== el) peer.value = el.value; });
  }
  el.setAttribute('aria-invalid', String(!el.checkValidity()));
  updateResults();
});
$('#app').addEventListener('change', event => {
  const el = event.target;
  if (el.dataset.group == null || (el.tagName !== 'SELECT' && el.type !== 'checkbox')) return;
  const i = Number(el.dataset.group), g = state.groups[i], key = el.dataset.key;
  if (key === 'provider') {
    g.modelId = preferred(catalog.models.filter(m => m.provider === el.value)).id;
    g.rateOverride = null;
  } else if (key === 'region') {
    g.modelId = preferred(catalog.models.filter(m => m.provider === activeGroups[i].provider && m.region === el.value)).id;
    g.rateOverride = null;
  } else if (key === 'preset') {
    const { name, ...preset } = PRESETS[el.value];
    Object.assign(g, preset, { preset: el.value });
  } else if (key === 'custom') {
    const m = offering(g);
    g.rateOverride = el.checked ? Object.fromEntries(['input', 'read', 'write', 'output'].map(k => [k, m?.[k] ?? m?.input ?? 0])) : null;
  } else { g[key] = el.value; if (key === 'modelId') g.rateOverride = null; }
  render();
});

$('#share').addEventListener('click', async () => {
  if (!currentResult) return;
  const hash = encodeScenario(state);
  const link = `${location.origin}${location.pathname}#s=${hash}`;
  history.replaceState(null, '', '#s=' + hash);
  try { await navigator.clipboard.writeText(link); toast('Scenario link copied. It includes group names and assumptions, and uses the latest catalog when opened.'); }
  catch { toast('The scenario is now in your address bar. Copy that URL to share it.'); }
});
$('#print').addEventListener('click', () => { if (currentResult) window.print(); });

async function start() {
  try {
    const response = await fetch('data/prices.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error(`Pricing catalog returned HTTP ${response.status}.`);
    catalog = await response.json();
    if (!Array.isArray(catalog.models) || !catalog.models.length) throw new Error('No usable pricing catalog was found.');
    state = { ...DEFAULTS, groups: [newGroup('feature', preferred(catalog.models).id)] };
    if (location.hash.startsWith('#s=')) {
      try { state = decodeScenario(location.hash.slice(3)); toast('Shared scenario loaded. Costs use the latest catalog; custom rate overrides are preserved.'); }
      catch (error) { toast('Could not load the shared scenario: ' + error.message + ' Showing the default example.', true); }
    }
    render(); renderSources();
    if (catalog.sources.some(s => s.status !== 'ok')) toast('Some pricing feeds did not refresh successfully. Check Pricing sources for coverage and last-successful dates.');
    // Optional browser agent integration. Uses the exact same estimate as the UI.
    const context = document.modelContext;
    if (context?.registerTool) {
      const lifecycle = new AbortController();
      window.addEventListener('pagehide', () => lifecycle.abort(), { once: true });
      Promise.resolve(context.registerTool({ name: 'get_cost_estimate', title: 'Read current cost estimate', description: 'Read the currently displayed workload, assumptions, and scenario totals in USD. Does not change the scenario.', inputSchema: { type: 'object', properties: {}, additionalProperties: false }, annotations: { readOnlyHint: true, untrustedContentHint: true }, execute(input) {
        if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length) throw new Error('Expected an empty object.');
        if (!currentResult) throw new Error('Correct invalid inputs before reading the estimate.');
        return { currency: 'USD', state: structuredClone(state), totals: currentResult.scenarios.map(s => s.total), scope: 'Text/code token costs only; illustrative sensitivity scenarios, not predictions.' };
      } }, { signal: lifecycle.signal })).catch(() => {});
    }
  } catch (error) {
    $('#app').innerHTML = `<div class="error-box"><h2>Pricing couldn’t be loaded</h2><p>${esc(error.message)}</p><p>Reload the page to retry, or check the <a href="https://github.com/MrZoller/llm-cost-explorer/actions">pricing refresh history</a>.</p></div>`;
    $('#share').disabled = true; $('#print').disabled = true;
  }
}
start();
