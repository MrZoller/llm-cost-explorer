#!/usr/bin/env python3
"""Refresh USD text-token list prices. No cloud account or third-party API key.

Sources are refreshed independently; a failed source retains its last good rows
and original checkedAt timestamp. Unknown units and ambiguous meters fail closed.
"""
import concurrent.futures
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
                if any(word in compact for word in ['batch', 'priority', 'flex', 'reserved', 'provisioned', 'latency', '1hour', '1hcache', 'tokens1h', 'write1h', 'training', 'image', 'video', 'audio', 'embedding']):
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
                if 'longcontext' in compact or '200ktokens' in compact or 'above200k' in compact:
                    # Tiered legacy meters need a dedicated adapter; never blend.
                    continue
                key = (name, deployment)
                row = groups.setdefault(key, record('AWS GovCloud' if region.startswith('us-gov') else 'AWS Bedrock', region, name, deployment, url))
                price = float(dim['pricePerUnit']['USD']) * factor
                put_meter(row, kind, price, {'sku': sku, 'description': dim['description'], 'unit': dim['unit'], 'effectiveDate': term.get('effectiveDate')})
    return finish([r for r in groups.values() if not r.pop('_conflict', False)])

def aws(service, region):
    url = f'{AWS_BASE}/offers/v1.0/aws/{service}/current/{region}/index.json'
    return parse_aws(fetch(url), region, url)

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

def parse_openai(markdown, url):
    section = markdown.split('### Standard pricing data', 1)[1].split('### Batch pricing data', 1)[0]
    if 'Short context input' not in section or 'Long context output' not in section:
        raise ValueError('OpenAI price table schema changed')
    rows = []
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) != 9 or dollars(cells[1]) is None or dollars(cells[4]) is None:
            continue
        name = re.sub(r'\s*\(.*\)', '', cells[0])
        row = record('OpenAI', 'Global', name, 'Standard', url)
        row.update(dict(zip(['input', 'read', 'write', 'output'], map(dollars, cells[1:5]))))
        if dollars(cells[5]) is not None and dollars(cells[8]) is not None:
            if '≤272K input tokens' not in markdown:
                raise ValueError('OpenAI context threshold changed')
            row['longContext'] = {'threshold': 272000, **dict(zip(['input', 'read', 'write', 'output'], map(dollars, cells[5:9])))}
        rows.append(row)
    return finish(rows)

def parse_anthropic(markdown, url):
    section = markdown.split('## Model pricing', 1)[1].split('\n## ', 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) != 6 or dollars(cells[1]) is None or dollars(cells[5]) is None:
            continue
        name = re.sub(r'\s*\(.*', '', cells[0]).replace('**', '').strip()
        row = record('Anthropic', 'Global', name, 'Standard · 5-minute cache', url)
        row.update(input=dollars(cells[1]), write=dollars(cells[2]), read=dollars(cells[4]), output=dollars(cells[5]))
        if 'limited' in cells[0]:
            row['note'] = 'Limited availability; a listed rate does not grant access.'
        if 'deprecated' in cells[0].lower():
            continue
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
            retained = [r for r in previous.get('models', []) if r.get('sourceKey') == key]
            old_time = min((r['checkedAt'] for r in retained), default=None)
            return retained, dict(key=key, name=name, url=url, status='stale' if retained else 'unavailable', checkedAt=old_time, attemptedAt=NOW, count=len(retained), error=str(exc)[:200])
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for result, status in pool.map(run, jobs):
            rows.extend(result)
            statuses.append(status)
            print(f"{status['name']}: {status['status']} ({status['count']} offerings)", flush=True)
    # AWS services can overlap; keep separate identities only when prices agree.
    unique = {}
    for row in rows:
        if row['id'] in unique:
            old = unique[row['id']]
            if any(old[k] != row[k] for k in ['input', 'output', 'read', 'write']):
                row['id'] += '-' + hashlib.sha256(row['sourceKey'].encode()).hexdigest()[:6]
                row['deployment'] += ' · ' + row['sourceKey'].split(':')[0]
        unique[row['id']] = row
    return {'version': 1, 'generatedAt': NOW, 'currency': 'USD', 'sources': statuses, 'models': sorted(unique.values(), key=lambda r: (r['provider'], r['region'], r['name'], r['deployment']))}

def main():
    previous = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {}
    jobs = []
    for service in ['AmazonBedrock', 'AmazonBedrockFoundationModels']:
        for region in ['us-gov-west-1', 'us-gov-east-1', 'us-east-1', 'us-west-2']:
            url = f'{AWS_BASE}/offers/v1.0/aws/{service}/current/{region}/index.json'
            jobs.append((service + ':' + region, service + ' · ' + region, url, lambda s=service, r=region: aws(s, r)))
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
