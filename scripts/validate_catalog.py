"""Check the deployable snapshot without making network requests."""
import json
from pathlib import Path
from update_prices import valid

catalog = json.loads((Path(__file__).resolve().parents[1] / 'site/data/prices.json').read_text())
assert catalog['currency'] == 'USD' and catalog['version'] == 1
assert catalog['models'], 'Empty catalog'
assert len({m['id'] for m in catalog['models']}) == len(catalog['models']), 'Duplicate IDs'
for model in catalog['models']:
    assert valid(model['input']) and valid(model['output']), model['id']
    for field in ['read', 'write']:
        assert model[field] is None or valid(model[field]), model['id']
    assert model['source'].startswith('https://') and model['checkedAt']
    assert model['sourceType'] in ['official', 'secondary']
    if 'longContext' in model:
        assert model['longContext']['threshold'] > 0
        assert valid(model['longContext']['input']) and valid(model['longContext']['output'])
    if model['region'].startswith('us-gov'):
        assert model['provider'] == 'AWS GovCloud'
    if model['region'].startswith('usgov'):
        assert model['provider'] == 'Azure Government'
print(f"Validated {len(catalog['models'])} offerings; {sum(s['status'] != 'ok' for s in catalog['sources'])} degraded sources.")
