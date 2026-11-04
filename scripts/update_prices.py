#!/usr/bin/env python3
"""Refresh USD text-token list prices. No cloud account or third-party API key.

Sources are refreshed independently; a failed source retains its last good rows
and original checkedAt timestamp. Unknown units and ambiguous meters fail closed.
"""
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'site/data/prices.json'
AWS_BASE = 'https://pricing.us-east-1.amazonaws.com'
LITELLM = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'
NOW = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
TODAY = NOW[:10]

def fetch(url, json_data=True):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'LLM-Cost-Explorer/1.0 (public pricing catalog)'})
            with urllib.request.urlopen(req, timeout=50) as response:
                raw = response.read(50_000_001)
            if len(raw) > 50_000_000:
                raise ValueError('Source exceeds 50 MB limit')
            return json.loads(raw) if json_data else raw.decode('utf-8')
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)

def slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')

def valid(value):
    return isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value < 1_000_000

def unit_factor(unit):
    compact = re.sub(r'\s+', '', unit.lower())
    return {'1k': 1000, '1ktokens': 1000, '1m': 1, '1mtokens': 1, 'tokens': 1_000_000, '1token': 1_000_000}.get(compact)

def record(provider, region, name, deployment, source, source_type='official', **values):
    identity = '|'.join([provider, region, name, deployment])
    return dict(id=slug(identity) + '-' + hashlib.sha256(identity.encode()).hexdigest()[:8], provider=provider, region=region,
                name=name, deployment=deployment, source=source, sourceType=source_type, checkedAt=NOW,
                input=None, output=None, read=None, write=None, **values)

def finish(rows):
    complete = [r for r in rows if valid(r.get('input')) and valid(r.get('output'))]
    if not complete:
        raise ValueError('No complete input/output price pairs; retaining previous source snapshot')
    ids = set()
    for r in complete:
        if r['id'] in ids:
            raise ValueError('Duplicate offering identity: ' + r['id'])
        ids.add(r['id'])
        for field in ('input', 'output', 'read', 'write'):
            if r.get(field) is not None and not valid(r[field]):
                raise ValueError('Invalid price')
        if 'longContext' in r:
            tier = r['longContext']
            if not isinstance(tier.get('threshold'), int) or tier['threshold'] <= 0:
                raise ValueError('Invalid long-context threshold')
            if not valid(tier.get('input')) or not valid(tier.get('output')):
                raise ValueError('Invalid long-context input/output price')
            if any(tier.get(field) is not None and not valid(tier[field]) for field in ('read', 'write')):
                raise ValueError('Invalid long-context cache price')
    return complete

def put_meter(row, kind, price, evidence):
    # Differing duplicate prices indicate unrecognized dimensions. Never pick min.
    if row[kind] is not None and abs(row[kind] - price) > 1e-8:
        row['_conflict'] = True
    row[kind] = price
    row.setdefault('meters', {})[kind] = evidence

def parse_aws(data, region, url):
    groups = {}
    for sku, product in data.get('products', {}).items():
        a = product.get('attributes', {})
        if a.get('regionCode') != region:
            continue
        name = a.get('model') or a.get('servicename', '').replace(' (Amazon Bedrock Edition)', '')
        for term in data.get('terms', {}).get('OnDemand', {}).get(sku, {}).values():
            if term.get('effectiveDate', '')[:10] > TODAY:
                continue
            for dim in term.get('priceDimensions', {}).values():
                factor = unit_factor(dim.get('unit', ''))
                if factor is None or dim.get('beginRange', '0') != '0' or dim.get('endRange', 'Inf') != 'Inf':
                    continue
                text = ' '.join([a.get('usagetype', ''), a.get('inferenceType', ''), a.get('feature', ''), dim.get('description', ''), a.get('service_tier', '')]).lower()
                compact = re.sub(r'[^a-z0-9]', '', text)
                if any(word in compact for word in ['batch', 'priority', 'flex', 'ultrafast', 'reserved', 'provisioned', 'latency', '1hour', '1hcache', 'tokens1h', 'write1h', 'training', 'image', 'video', 'audio', 'embedding']):
                    continue
                if 'cacheread' in compact:
                    kind = 'read'
                elif 'cachewrite' in compact:
                    kind = 'write'
                elif 'inputtoken' in compact:
                    kind = 'input'
                elif 'outputtoken' in compact or 'responsetoken' in compact:
                    kind = 'output'
                else:
                    continue
                deployment = 'Global' if 'global' in text else 'Regional / geo'
                if '200ktokens' in compact or 'above200k' in compact:
                    # Tiered legacy meters need a dedicated adapter; never blend.
                    continue
                long_context = 'longctx' in compact or 'longcontext' in compact
                if long_context:
                    deployment += ' · Long context'
                key = (name, deployment)
                row = groups.setdefault(key, record('AWS GovCloud' if region.startswith('us-gov') else 'AWS Bedrock', region, name, deployment, url))
                if long_context:
                    row['band'] = 'Long context'
                price = float(dim['pricePerUnit']['USD']) * factor
                put_meter(row, kind, price, {'sku': sku, 'description': dim['description'], 'unit': dim['unit'], 'effectiveDate': term.get('effectiveDate')})
    for (name, deployment), row in groups.items():
        if row.get('band') == 'Long context':
            short = groups.get((name, deployment.replace(' · Long context', '')))
            if short:
                short['band'] = 'Short context'
    return finish([r for r in groups.values() if not r.pop('_conflict', False)])

def aws(service, region):
    url = f'{AWS_BASE}/offers/v1.0/aws/{service}/current/{region}/index.json'
    return parse_aws(fetch(url), region, url)

AWS_MODEL_CARDS = {
    'gpt-5.4': '54', 'gpt-5.5': '55', 'gpt-5.6-sol': '56-sol',
    'gpt-5.6-terra': '56-terra', 'gpt-5.6-luna': '56-luna', 'gpt-6-astra': '6-astra',
}
AWS_REGIONS = ['us-east-1', 'us-west-2', 'us-gov-east-1', 'us-gov-west-1']

def parse_aws_model_card(markdown, model, url):
    """Read scoped Standard tables and availability from AWS's official cards.

    Never infer a government offering from a commercial table. The bulk API
    omits some available models and can disagree with the model-card rates.
    """
    section = markdown.split('## Pricing\n', 1)[1].split('\n## ', 1)[0]
    availability = markdown.split('## Regional Availability\n', 1)[1].split('\n## ', 1)[0]
    supported = {r: [False, False, False] for r in AWS_REGIONS}
    for line in availability.splitlines():
        if not line.strip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        for region in AWS_REGIONS:
            if len(cells) == 4 and re.match(r'`?' + re.escape(region) + r'\b', cells[0]):
                supported[region] = [old or 'icon-yes.png' in cell for old, cell in zip(supported[region], cells[1:])]
    tables, regions, band, threshold, skip = {}, [], None, None, False
    for line in section.splitlines():
        lower = line.lower()
        if line.startswith('### '):
            skip = any(tier in lower for tier in ['ultrafast', 'priority', 'flex', 'batch'])
            if 'commercial' in lower:
                regions = ['us-east-1', 'us-west-2']
            elif 'aws govcloud' in lower:
                regions = [r for r, marker in [('us-gov-east-1', 'us-east'), ('us-gov-west-1', 'us-west')] if marker in lower]
            else:
                raise ValueError('Unrecognized AWS model-card pricing scope: ' + line)
            band = None
        if line.startswith('###'):
            if 'short context' in lower:
                band = 'short'
            elif 'long context' in lower:
                band = 'long'
            match = re.search(r'(\d+)k input tokens', lower)
            if match:
                threshold = int(match[1]) * 1000
        if skip or not line.strip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells) != 5 or cells[0] not in ['In-Region', 'Geo CRIS', 'Global CRIS']:
            continue
        if not regions or band is None or threshold is None:
            raise ValueError('AWS model-card table missing scope or context threshold')
        if dollars(cells[1]) is None or dollars(cells[4]) is None:
            raise ValueError('AWS model-card input/output pricing missing')
        column = ['In-Region', 'Geo CRIS', 'Global CRIS'].index(cells[0])
        deployment = 'Global' if column == 2 else 'Regional / geo'
        for region in regions:
            if not supported[region][column]:
                continue
            key = (region, deployment, band)
            row = tables.setdefault(key, record('AWS GovCloud' if region.startswith('us-gov') else 'AWS Bedrock', region, 'openai.' + model, deployment, url))
            row['contextThreshold'] = threshold
            for kind, cell in zip(['input', 'write', 'read', 'output'], cells[1:]):
                amount = dollars(cell)
                if amount is not None:
                    put_meter(row, kind, amount, {'description': f'{model} · {region} · {cells[0]} · {band} context', 'unit': '1M tokens', 'source': url})
            if row['write'] is not None:
                row['cacheWriteTtlMinutes'] = 30
    rows = []
    for (region, deployment, band), row in tables.items():
        if band != 'short':
            continue
        if row.pop('_conflict', False):
            raise ValueError('Conflicting AWS model-card rates for the same offering')
        long = tables.get((region, deployment, 'long'))
        if long:
            if long.pop('_conflict', False) or not valid(long['input']) or not valid(long['output']):
                raise ValueError('Incomplete/conflicting AWS model-card long-context table')
            row['longContext'] = {'threshold': row['contextThreshold'], **{k: long[k] for k in ['input', 'output', 'read', 'write']}, 'meters': long['meters']}
        else:
            row['unpricedAbove'] = row['contextThreshold']
        rows.append(row)
    return finish(rows)

def aws_model_card(model, suffix):
    url = f'https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-{suffix}.html'
    return parse_aws_model_card(fetch(url.replace('.html', '.md'), False), model, url)

def reconcile_aws_cards(rows):
    """Prefer model-card rates explicitly; retain bulk discrepancies as evidence."""
    def key(row):
        name = re.sub(r'[^a-z0-9]', '', row['name'].lower()).removeprefix('openai')
        return row['provider'], row['region'], name, row['deployment'].replace(' · Long context', '')
    cards = {key(r): r for r in rows if r.get('sourceKey', '').startswith('aws-model-card:')}
    result = []
    for row in rows:
        card = cards.get(key(row))
        if not card or row.get('sourceKey', '').startswith('aws-model-card:'):
            result.append(row)
            continue
        # Only combine tables if the documentation actually supplies the band.
        is_long = row.get('band') == 'Long context'
        expected = card.get('longContext') if is_long else card
        if expected is None:
            result.append(row)
            continue
        if not is_long:
            card['id'] = row['id']  # Preserve previously shared scenario identities.
        if any(row.get(k) != expected.get(k) for k in ['input', 'output', 'read', 'write']):
            card.setdefault('priceDiscrepancies', []).append({'source': row['source'], 'checkedAt': row['checkedAt'], 'band': 'long' if is_long else 'short', 'rates': {k: row[k] for k in ['input', 'output', 'read', 'write']}})
    return result

def parse_azure(items, region, url):
    groups = {}
    # Keep SKU family, context band, and deployment in the pairing key. Azure's
    # abbreviations vary across generations; unknown names are not guessed.
    for item in items:
        if item.get('armRegionName') != region or item.get('currencyCode') != 'USD' or item.get('type') != 'Consumption' or item.get('tierMinimumUnits', 0) != 0 or item.get('effectiveStartDate', '')[:10] > TODAY:
            continue
        factor = unit_factor(item.get('unitOfMeasure', ''))
        name = item.get('skuName', '').lower()
        if factor is None or re.search(r'fine.?tun|batch|flex|priority|train|audio|realtime|image|vision', name):
            continue
        if re.search(r'cached|\bcd\b', name):
            kind = 'read'
        elif re.search(r'\b(outp|output|opt)\b', name):
            kind = 'output'
        elif re.search(r'\b(inp|input)\b', name):
            kind = 'input'
        else:
            continue
        deployment = 'Data Zone' if re.search(r'data zone|dzone|\bdz\b', name) else 'Global' if re.search(r'global|glbl|\bgl\b', name) else 'Regional'
        band = 'Long context' if 'longco' in name else 'Short context' if 'shortco' in name else None
        cleaned = re.sub(r'\b(cached|cd|outp|output|opt|inp|input|regnl|regional|global|glbl|gl|data zone|dzone|dz|std|shortco|longco)\b', ' ', name)
        cleaned = re.sub(r'\s+', ' ', cleaned.replace('-', ' ')).strip()
        cleaned = re.sub(r'^az ', '', cleaned)
        if not re.match(r'gpt|o[1-9]|[5-9]\.', cleaned):
            continue
        label = cleaned.upper().replace('GPT ', 'GPT-')
        dep = deployment + (' · ' + band if band else '')
        key = (label, dep, item.get('productName'))
        row = groups.setdefault(key, record('Azure Government' if region.startswith('usgov') else 'Azure', region, label, dep, url))
        if band:
            row['band'] = band
        put_meter(row, kind, item['retailPrice'] * factor, {'meterId': item['meterId'], 'description': item.get('meterName'), 'unit': item['unitOfMeasure'], 'effectiveDate': item.get('effectiveStartDate')})
    return finish([r for r in groups.values() if not r.pop('_conflict', False)])

def azure(region):
    query = urllib.parse.urlencode({'$filter': f"armRegionName eq '{region}' and contains(productName, 'OpenAI')"})
    url = 'https://prices.azure.com/api/retail/prices?' + query
    next_url, items, seen = url, [], set()
    while next_url:
        if next_url in seen or len(seen) >= 100:
            raise ValueError('Unexpected Azure pagination')
        if urllib.parse.urlparse(next_url).hostname != 'prices.azure.com':
            raise ValueError('Unexpected pagination host')
        seen.add(next_url)
        data = fetch(next_url)
        items.extend(data['Items'])
        next_url = data.get('NextPageLink')
    return parse_azure(items, region, url)

def dollars(cell):
    match = re.search(r'\$([\d,.]+)', cell)
    return float(match[1].replace(',', '')) if match else None

def pricing_section(markdown, heading):
    """Use the first named section, stopping before another peer/parent section."""
    match = re.search(r'^' + re.escape(heading) + r'\s*$', markdown, re.M)
    if not match:
        raise ValueError('Missing pricing section: ' + heading)
    depth = len(heading.split(' ', 1)[0])
    return re.split(r'^#{1,' + str(depth) + r'}\s+', markdown[match.end():], maxsplit=1, flags=re.M)[0]

def price_table_rows(section):
    headers = None
    for line in section.splitlines():
        if not line.strip().startswith('|'):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if cells[0].lower() == 'model':
            headers = [c.lower() for c in cells]
            if len(set(headers)) != len(headers):
                raise ValueError('Duplicate pricing column')
        elif any('$' in cell for cell in cells):
            if not headers or len(cells) != len(headers):
                raise ValueError('Price table row does not match its header')
            yield dict(zip(headers, cells))

def parse_openai(markdown, url):
    # Later Standard sections are specialized/restricted model families. Do not
    # sweep their rates (or Batch/Fast/Flex tables) into the flagship section.
    section = pricing_section(markdown, '### Standard pricing data')
    rows = []
    for cells in price_table_rows(section):
        tiered = 'short context input' in cells
        prefix = 'short context ' if tiered else ''
        # Recognize both the legacy cache-write header and explicit TTL columns.
        write = '30m write' if prefix + '30m write' in cells else 'cache writes'
        fields = {'input': 'input', 'output': 'output', 'read': 'cached input', 'write': write}
        expected = {'model', 'input', 'output', 'cached input'}
        if tiered:
            suffixes = {'input', 'output', 'cached input', write}
            if write == '30m write':
                suffixes.add('12h write')
            expected = {'model'} | {band + suffix for band in ['short context ', 'long context '] for suffix in suffixes}
        if set(cells) != expected:
            raise ValueError('OpenAI price table schema changed')
        rates = {kind: dollars(cells.get(prefix + label, '')) for kind, label in fields.items()}
        if not valid(rates['input']) or not valid(rates['output']):
            raise ValueError('Incomplete or invalid OpenAI input/output pricing')
        name = re.sub(r'\s*\(.*\)', '', cells['model'])
        row = record('OpenAI', 'Global', name, 'Standard', url)
        row.update(rates)
        if write == '30m write' and row['write'] is not None:
            row['cacheWriteTtlMinutes'] = 30
        if tiered:
            if '≤272K input tokens' not in markdown:
                raise ValueError('OpenAI context threshold changed')
            long_rates = {kind: dollars(cells['long context ' + label]) for kind, label in fields.items()}
            if long_rates['input'] is not None and long_rates['output'] is not None:
                row['longContext'] = {'threshold': 272000, **long_rates}
            elif any(rate is not None for rate in long_rates.values()):
                raise ValueError('Incomplete OpenAI long-context pricing')
            else:
                row['unpricedAbove'] = 272000
        rows.append(row)
    return finish(rows)

def parse_anthropic(markdown, url):
    section = pricing_section(markdown, '## Model pricing')
    groups = {}
    fields = {'input': 'base input tokens', 'write': '5m cache writes', 'read': 'cache hits and refreshes', 'output': 'output tokens'}
    for cells in price_table_rows(section):
        if set(cells) != {'model', '1h cache writes', *fields.values()}:
            raise ValueError('Anthropic price table schema changed')
        label = cells['model'].replace('**', '').strip()
        if re.search(r'\b(deprecated|retired)\b', label, re.I):
            continue
        rates = {kind: dollars(cells[column]) for kind, column in fields.items()}
        if not valid(rates['input']) or not valid(rates['output']):
            raise ValueError('Incomplete or invalid Anthropic input/output pricing')
        name = re.sub(r'\s*\(.*', '', label).strip()
        band = re.search(r'\(for prompts (up to|over) ([\d,]+) tokens\)', label, re.I)
        if not band and re.search(r'\b(prompts|tokens|context)\b', label[len(name):], re.I):
            raise ValueError('Unrecognized Anthropic context band: ' + label)
        kind = band[1].lower() if band else 'base'
        threshold = int(band[2].replace(',', '')) if band else None
        if threshold is not None and threshold <= 0:
            raise ValueError('Invalid Anthropic context threshold')
        group = groups.setdefault(name, {})
        if kind in group:
            raise ValueError('Duplicate Anthropic pricing band: ' + label)
        row = record('Anthropic', 'Global', name, 'Standard · 5-minute cache', url)
        row.update(rates)
        if row['write'] is not None:
            row['cacheWriteTtlMinutes'] = 5
        if 'limited' in label.lower():
            row['note'] = 'Limited availability; a listed rate does not grant access.'
        group[kind] = (row, threshold)
    rows = []
    for name, group in groups.items():
        if set(group) == {'base'}:
            row = group['base'][0]
        elif set(group) == {'up to', 'over'}:
            row, threshold = group['up to']
            long, long_threshold = group['over']
            if threshold != long_threshold:
                raise ValueError('Mismatched Anthropic context thresholds: ' + name)
            row['longContext'] = {'threshold': threshold, **{kind: long[kind] for kind in fields}}
        else:
            raise ValueError('Incomplete or ambiguous Anthropic context bands: ' + name)
        rows.append(row)
    return finish(rows)

def direct(provider):
    url = 'https://developers.openai.com/api/docs/pricing' if provider == 'OpenAI' else 'https://platform.claude.com/docs/en/about-claude/pricing'
    parser = parse_openai if provider == 'OpenAI' else parse_anthropic
    return parser(fetch(url + '.md', False), url)

def secondary():
    data = fetch(LITELLM)
    providers = {'gemini': ('Google Gemini', 'https://ai.google.dev/gemini-api/docs/pricing'),
                 'deepseek': ('DeepSeek', 'https://api-docs.deepseek.com/quick_start/pricing'),
                 'mistral': ('Mistral', 'https://mistral.ai/pricing'),
                 'xai': ('xAI', 'https://docs.x.ai/docs/models')}
    rows = []
    for key, value in data.items():
        if not isinstance(value, dict) or value.get('litellm_provider') not in providers or value.get('mode') != 'chat':
            continue
        if value.get('deprecation_date', '9999') <= TODAY or any(s in key for s in ['audio', 'image', 'robotics', 'live-', 'realtime', 'computer-use']):
            continue
        if not valid(value.get('input_cost_per_token')) or not valid(value.get('output_cost_per_token')):
            continue
        provider, official = providers[value['litellm_provider']]
        row = record(provider, 'Global', key, 'Standard', LITELLM, 'secondary', officialSource=official)
        fields = {'input': 'input_cost_per_token', 'output': 'output_cost_per_token', 'read': 'cache_read_input_token_cost', 'write': 'cache_creation_input_token_cost'}
        for field, source_key in fields.items():
            val = value.get(source_key)
            row[field] = val * 1e6 if valid(val) else None
        for threshold in [128000, 200000, 272000]:
            suffix = f'_above_{threshold // 1000}k_tokens'
            if valid(value.get(fields['input'] + suffix)) and valid(value.get(fields['output'] + suffix)):
                tier = {'threshold': threshold}
                for field, source_key in fields.items():
                    val = value.get(source_key + suffix)
                    tier[field] = val * 1e6 if valid(val) else row[field]
                row['longContext'] = tier
                break
        row['contextLimit'] = value.get('max_input_tokens')
        row['note'] = 'Community-maintained LiteLLM catalog; verify against the provider. Context caching storage and other non-token fees are excluded.'
        rows.append(row)
    return finish(rows)

def refresh(previous, jobs):
    rows, statuses = [], []
    def run(job):
        key, name, url, fn = job
        try:
            result = finish(fn())
            for row in result:
                row['sourceKey'] = key
            return result, dict(key=key, name=name, url=url, status='ok', checkedAt=NOW, count=len(result))
        except Exception as exc:
            # Reconciliation hides duplicate bulk rows from the picker. Keep
            # their original snapshots so an outage cannot erase comparisons.
            snapshot = previous.get('awsBulkSnapshots', previous.get('models', [])) if key.startswith('AmazonBedrock') else previous.get('models', [])
            retained = copy.deepcopy([r for r in snapshot if r.get('sourceKey') == key])
            old_time = min((r['checkedAt'] for r in retained), default=None)
            return retained, dict(key=key, name=name, url=url, status='stale' if retained else 'unavailable', checkedAt=old_time, attemptedAt=NOW, count=len(retained), error=str(exc)[:200])
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for result, status in pool.map(run, jobs):
            rows.extend(result)
            statuses.append(status)
            print(f"{status['name']}: {status['status']} ({status['count']} offerings)", flush=True)
    # A retained card can contain last run's comparison. Recompute with the
    # source snapshots available in this refresh, without stale duplicate notes.
    for row in rows:
        row.pop('priceDiscrepancies', None)
    aws_bulk = copy.deepcopy([r for r in rows if r.get('sourceKey', '').startswith('AmazonBedrock')])
    rows = reconcile_aws_cards(rows)
    # AWS services can overlap; keep separate identities only when prices agree.
    unique = {}
    for row in rows:
        if row['id'] in unique:
            old = unique[row['id']]
            if any(old[k] != row[k] for k in ['input', 'output', 'read', 'write']):
                row['id'] += '-' + hashlib.sha256(row['sourceKey'].encode()).hexdigest()[:6]
                row['deployment'] += ' · ' + row['sourceKey'].split(':')[0]
        unique[row['id']] = row
    return {'version': 1, 'generatedAt': NOW, 'currency': 'USD', 'sources': statuses, 'awsBulkSnapshots': aws_bulk, 'models': sorted(unique.values(), key=lambda r: (r['provider'], r['region'], r['name'], r['deployment']))}

def main():
    previous = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
    jobs = []
    for service in ['AmazonBedrock', 'AmazonBedrockFoundationModels']:
        for region in ['us-gov-west-1', 'us-gov-east-1', 'us-east-1', 'us-west-2']:
            url = f'{AWS_BASE}/offers/v1.0/aws/{service}/current/{region}/index.json'
            jobs.append((service + ':' + region, service + ' · ' + region, url, lambda s=service, r=region: aws(s, r)))
    for model, suffix in AWS_MODEL_CARDS.items():
        url = f'https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-{suffix}.html'
        jobs.append(('aws-model-card:' + model, 'AWS model card · ' + model, url, lambda m=model, s=suffix: aws_model_card(m, s)))
    for region in ['usgovvirginia', 'usgovarizona', 'eastus', 'westus']:
        jobs.append(('azure:' + region, 'Azure · ' + region, 'https://prices.azure.com/api/retail/prices', lambda r=region: azure(r)))
    for provider in ['OpenAI', 'Anthropic']:
        jobs.append((provider, provider + ' official pricing', 'https://developers.openai.com/api/docs/pricing' if provider == 'OpenAI' else 'https://platform.claude.com/docs/en/about-claude/pricing', lambda p=provider: direct(p)))
    jobs.append(('litellm', 'LiteLLM · additional direct providers', 'https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json', secondary))
    catalog = refresh(previous, jobs)
    if not catalog['models']:
        raise RuntimeError('No usable prices; previous catalog was not overwritten')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix('.tmp')
    temp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
    temp.replace(OUTPUT)
    print(f"Saved {len(catalog['models'])} offerings to {OUTPUT}")
    # An all-failed refresh must be visible as a failed workflow, not fresh data.
    if not any(s['status'] == 'ok' for s in catalog['sources']):
        sys.exit(1)

if __name__ == '__main__':
    main()
