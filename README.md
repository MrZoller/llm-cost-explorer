# LLM Cost Explorer

[Open the calculator](https://MrZoller.github.io/llm-cost-explorer/)

An interactive planning calculator for chat and coding-agent API costs, designed for managers and developers. Describe a task, an active day, or measured usage; explore lighter, central, and heavier workloads; and combine groups with different usage patterns into a team budget.

All calculations run in the browser. No cloud credentials, account connections, API keys, or model calls are needed. Group names and assumptions are only shared when you copy a scenario link. Links store the scenario in the URL fragment, not on a server. There is no analytics or application backend. Fonts are requested from Google Fonts with local fallbacks.

## Run locally

Requires Node.js 24+ and Python 3.10+. No npm or Python packages are needed.

```sh
npm start
# Open http://localhost:4173
npm test
npm run prices
python3 scripts/validate_catalog.py
```

## What the estimate means

Prices are USD per million tokens. The calculation uses four **disjoint** categories:

```text
cost = (fresh input × input rate
      + cached reads × read rate
      + cache writes × write rate
      + output including billed reasoning × output rate) / 1,000,000
```

For task and daily-use modes, call `i` receives `min(starting context + i × growth, cap)` input tokens. The first call starts cold. Subsequent calls can reuse the previous prefix; the hit percentage applies to that reused input. The write percentage applies to input not read from cache. Context growth already includes retained output and tool results. Retry overhead adds proportional token consumption. Active hours × tasks/hour defines daily task frequency; it is not a dollars/hour model.

Measured mode accepts **per-person totals over active workdays**. Total input includes cache reads and cache writes; these are subtracted to obtain fresh input. Output already includes billed reasoning, so it is never added again. Provider exports differ: for providers reporting fresh input separately, add the input categories before entering total input.

The default 0.5× and 2× scenarios change calls per task, so the longer scenario rereads more context. In measured mode they scale token volume. These are sensitivity scenarios, not confidence intervals, measured averages, spending limits, or forecasts. Mixed-group ranges assume the selected lighter/heavier behavior across every group; no probability distribution is asserted.

### Preset assumptions

These are editable design assumptions, **not empirical task benchmarks**. Task names are prompts to reason about consumption, not guarantees of difficulty or completion. They model chat and agent-style coding such as OpenCode or Codex without claiming tool-specific telemetry.

| Workload | Calls/task | Starting input | Growth/call | Visible output/call | Billed reasoning/call |
| --- | ---: | ---: | ---: | ---: | ---: |
| Everyday chat | 5 | 2,000 | 800 | 500 | 0 |
| Chat with documents | 10 | 12,000 | 2,000 | 1,000 | 500 |
| Focused coding fix | 8 | 12,000 | 2,000 | 800 | 500 |
| Feature or multi-file bug fix | 25 | 24,000 | 3,500 | 1,500 | 1,000 |
| Extended coding task | 60 | 40,000 | 5,000 | 2,000 | 2,000 |

Initial assumptions: 128K context cap, 60% hit rate on reused prefixes, 25% cache writes on misses, 10% retry overhead, 20 active days/month, and 5 days/week. A missing cache price falls back to regular input pricing with a warning. Set reasoning to zero for models/workloads without separately billed reasoning. Cache minimum lengths, TTL expiration, and exact prompt-prefix behavior must be reflected in the user's effective hit/write assumptions.

Repository size alone is not a billing input. Only material actually sent to the model matters, often repeatedly. Context truncation is approximated with a cap; summary-generation calls, subagents, and model routing require adjustment or measured data. The same assumed workload across models does not imply equivalent quality, speed, or success.

**Calibration:** collect a few representative chat and coding-agent sessions from a small pilot. Record fresh input, cache reads/writes, total billed output, and active days. Enter those totals in Measured usage, then compare against the task assumptions. Billing mechanics are grounded in [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), and [Azure caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching). [Claude Code cost-management guidance](https://code.claude.com/docs/en/costs) recommends a pilot to establish a team's baseline.

## Automatic pricing

The scheduled GitHub Actions workflow refreshes prices daily at **11:17 UTC** and republishes GitHub Pages. GitHub may delay scheduled jobs and may disable schedules in inactive public repositories; inspect Actions when timestamps age. `workflow_dispatch` supports an immediate refresh. Code pushes deploy the checked-in snapshot after tests. The initial deployment contains a freshly fetched snapshot.

| Source | Coverage | Provenance |
| --- | --- | --- |
| AWS Price List Bulk API: AmazonBedrock and AmazonBedrockFoundationModels | us-east-1, us-west-2, us-gov-east-1, us-gov-west-1; regional/global variants | Official, exact SKU evidence retained |
| Azure Retail Prices API | usgovvirginia, usgovarizona, eastus, westus; regional, data-zone, global, and context-band meters | Official, exact meter IDs retained |
| AWS GPT model documentation | GPT-5.4, GPT-5.5, GPT-5.6 Sol/Terra/Luna, GPT-6 Astra; only explicitly documented regions and standard pricing | Official, supplements gaps in the bulk feed |
| OpenAI pricing Markdown | Standard text pricing, with published long-context tiers | Official |
| Anthropic pricing Markdown | Direct standard text pricing, 5-minute cache-write rates | Official |
| LiteLLM public catalog | Google Gemini, Mistral, DeepSeek, xAI | Secondary; labeled in the interface |

Useful references: [AWS bulk price files](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/using-the-aws-price-list-bulk-api-fetching-price-list-files-manually.html), [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices), and [LiteLLM source](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json). The UI and JSON retain source links and per-source check timestamps. Listing a price does not prove model availability or government authorization in an account.

The importer rejects unknown billing units, incomplete price pairs, conflicting meters, and unsupported service tiers. No inference is made from commercial to government prices. Failed sources retain their original last-good timestamps and rows; the UI marks them stale. A successful fetch validates shape and units, not invoice accuracy. Supported AWS GPT model cards supplement missing bulk-feed offerings. Their explicit region and context-band tables take precedence over bulk-feed rates for the same offering. If those official sources disagree, both sets of rates and their check dates are retained, and the UI displays a warning and comparison. Commercial rates are never used to fill government gaps. Missing documented long-context pricing prevents estimates above the known threshold instead of silently using a short-context rate. Source layouts can change, so check refresh history and compare important budgets with actual billing.

Known limitations:

- Standard text/code token charges only. Excludes subscriptions, built-in tool/search fees, sandboxes, storage/cache storage, retrieval, embeddings, networking, taxes, and provisioned capacity.
- No batch, flex, priority, reserved, fine-tuning, audio, video, or image pricing. Five-minute cache-write rates are used for Anthropic; supported AWS GPT model documentation specifies 30-minute writes. The applicable TTL is shown with the rates.
- Azure imports OpenAI-family products in the listed regions, not the entire Foundry catalog. Other providers/regions and contract rates can be entered manually.
- Supported long-context rate tiers apply conservatively to a full task (or measured period) when its peak input exceeds the published threshold. Mixed short/long measured usage should be entered as separate groups or calculations for a closer estimate. Azure context bands remain explicit selectable offerings; users must choose the applicable band.
- Context limits and tier metadata are incomplete in cloud price feeds. A warning prompts verification for large inputs. Direct-provider secondary metadata may also lag.
- Shared links preserve assumptions and overrides but use current catalog rates when reopened. Print to PDF to preserve a dated estimate with its rates and sources. Never put sensitive names or contract rates into a link you intend to share publicly.

## Repository layout

- `site/`: static app; deployed as-is to GitHub Pages.
- `site/engine.js`: pure estimation, validation, and scenario serialization.
- `site/data/prices.json`: last published price snapshot and source health.
- `scripts/update_prices.py`: daily source adapters, unit normalization, and stale-data retention.
- `tests/`: hand-calculated billing cases and price-adapter regression fixtures.
- `.github/workflows/pages.yml`: tests, scheduled refresh, and Pages deployment.

The optional browser WebMCP integration exposes a read-only `get_cost_estimate` tool when supported. It returns the same state and totals shown by the UI; unsupported browsers need no fallback.
