import copy
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('prices', Path(__file__).resolve().parents[1] / 'scripts/update_prices.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class PriceTests(unittest.TestCase):
    def model_card_fixture(self):
        return '''## Pricing
All prices are USD per million tokens, Standard tier.
### Commercial Regions — short context (272K input tokens or fewer)
| Inference option | Input | Input — 30m cache write | Input — cache read | Output |
| In-Region | $2.20 | $2.75 | $0.22 | $13.20 |
### Commercial Regions — long context (more than 272K input tokens)
| In-Region | $4.40 | $5.50 | $0.44 | $19.80 |
### AWS GovCloud (US-East and US-West)
#### Short context (272K input tokens or fewer)
| In-Region | $2.70 | $3.375 | $0.27 | $16.20 |
#### Long context (more than 272K input tokens)
| In-Region | $5.40 | $6.75 | $0.54 | $24.30 |
### Ultrafast — Commercial Regions, short context (272K input tokens or fewer)
| In-Region | $66.00 | $82.50 | $6.60 | $330.00 |
## Regional Availability
| us-east-1 (Virginia) | ![yes](icon-yes.png) | ![no](icon-no.png) | ![no](icon-no.png) |
| us-west-2 (Oregon) | ![no](icon-no.png) | ![no](icon-no.png) | ![no](icon-no.png) |
| us-gov-east-1 | ![yes](icon-yes.png) | ![no](icon-no.png) | ![no](icon-no.png) |
| us-gov-west-1 | ![yes](icon-yes.png) | ![no](icon-no.png) | ![no](icon-no.png) |
## Other
'''

    def test_real_gpt_bulk_meters_keep_short_and_long_context_separate(self):
        data = json.loads((Path(__file__).parent / 'fixtures/aws-gpt-govcloud.json').read_text())
        rows = p.parse_aws(data, 'us-gov-west-1', data['sourceUrl'])
        self.assertEqual(len(rows), 5)  # 5.4 plus two context bands for Terra/Luna.
        terra = [r for r in rows if r['name'] == 'openai.gpt-5.6-terra']
        short = next(r for r in terra if r['band'] == 'Short context')
        long = next(r for r in terra if r['band'] == 'Long context')
        self.assertEqual([short[k] for k in ['input', 'read', 'write', 'output']], [2.64, .264, 3.3, 15.84])
        self.assertEqual([long[k] for k in ['input', 'read', 'write', 'output']], [5.28, .528, 6.6, 23.76])

    def test_cards_require_explicit_government_scope_and_supported_region(self):
        rows = p.parse_aws_model_card(self.model_card_fixture(), 'gpt-5.6-terra', 'https://example.com')
        self.assertEqual(len(rows), 3)
        east = next(r for r in rows if r['region'] == 'us-gov-east-1')
        self.assertEqual(east['input'], 2.7)
        self.assertEqual(east['longContext']['input'], 5.4)
        self.assertEqual(east['longContext']['threshold'], 272000)
        self.assertEqual(east['cacheWriteTtlMinutes'], 30)
        self.assertNotIn('us-west-2', [r['region'] for r in rows])
        self.assertEqual(next(r for r in rows if r['region'] == 'us-east-1')['input'], 2.2)
        commercial = self.model_card_fixture().split('### AWS GovCloud')[0] + '## Regional Availability\n' + self.model_card_fixture().split('## Regional Availability\n')[1]
        self.assertTrue(all(r['provider'] == 'AWS Bedrock' for r in p.parse_aws_model_card(commercial, 'gpt-5.6-sol', 'https://example.com')))

    def test_cards_keep_missing_long_context_prices_explicit(self):
        md = self.model_card_fixture().replace('#### Long context (more than 272K input tokens)\n| In-Region | $5.40 | $6.75 | $0.54 | $24.30 |\n', '')
        row = next(r for r in p.parse_aws_model_card(md, 'gpt-5.4', 'https://example.com') if r['provider'] == 'AWS GovCloud')
        self.assertEqual(row['unpricedAbove'], 272000)
        self.assertNotIn('longContext', row)

    def test_card_precedence_preserves_discrepancies_and_existing_share_id(self):
        data = json.loads((Path(__file__).parent / 'fixtures/aws-gpt-govcloud.json').read_text())
        bulk = p.parse_aws(data, 'us-gov-west-1', data['sourceUrl'])
        for row in bulk: row['sourceKey'] = 'AmazonBedrock:us-gov-west-1'
        cards = p.parse_aws_model_card(self.model_card_fixture(), 'gpt-5.6-terra', 'https://example.com')
        for row in cards: row['sourceKey'] = 'aws-model-card:gpt-5.6-terra'
        old_id = next(r['id'] for r in bulk if r['name'] == 'openai.gpt-5.6-terra' and r['band'] == 'Short context')
        out = p.reconcile_aws_cards(bulk + cards)
        west = [r for r in out if r['name'] == 'openai.gpt-5.6-terra' and r['region'] == 'us-gov-west-1']
        self.assertEqual(len(west), 1)
        self.assertEqual(west[0]['id'], old_id)
        self.assertEqual(west[0]['input'], 2.7)
        self.assertEqual(len(west[0]['priceDiscrepancies']), 2)
        self.assertEqual(west[0]['source'], 'https://example.com')

    def test_aws_comparisons_survive_independent_source_outages(self):
        data = json.loads((Path(__file__).parent / 'fixtures/aws-gpt-govcloud.json').read_text())
        bulk = lambda: p.parse_aws(data, 'us-gov-west-1', data['sourceUrl'])
        card = lambda: p.parse_aws_model_card(self.model_card_fixture(), 'gpt-5.6-terra', 'https://example.com')
        jobs = [('AmazonBedrock:us-gov-west-1', 'Bulk', data['sourceUrl'], bulk),
                ('aws-model-card:gpt-5.6-terra', 'Card', 'https://example.com', card)]
        initial = p.refresh({}, jobs)
        def fail(): raise RuntimeError('simulated source outage')
        for failed in [0, 1]:
            with self.subTest(failed_source=failed):
                attempt = list(jobs)
                attempt[failed] = (*jobs[failed][:3], fail)
                stale = copy.deepcopy(initial)
                for row in stale['models'] + stale['awsBulkSnapshots']:
                    row['checkedAt'] = '2025-01-01T00:00:00Z'
                for _ in range(2):
                    stale = p.refresh(stale, attempt)
                    west = next(r for r in stale['models'] if r['name'] == 'openai.gpt-5.6-terra' and r['region'] == 'us-gov-west-1')
                    self.assertEqual(len(west['priceDiscrepancies']), 2)
                    self.assertEqual(stale['sources'][failed]['status'], 'stale')
                    self.assertEqual(stale['sources'][failed]['checkedAt'], '2025-01-01T00:00:00Z')
                    self.assertEqual(west['input'], 2.7)

    def test_units_fail_closed(self):
        self.assertEqual(p.unit_factor('1K tokens'), 1000)
        self.assertEqual(p.unit_factor('1M tokens'), 1)
        self.assertIsNone(p.unit_factor('1M TPM Hour'))
        self.assertIsNone(p.unit_factor('1/Hour'))

    def aws_fixture(self):
        data = {'products': {}, 'terms': {'OnDemand': {}}}
        for sku, desc, unit, price in [('i', 'Million Input Tokens Regional', '1M tokens', '3.6'), ('o', 'Million Response Tokens Regional', '1M tokens', '18'), ('c', 'Million Cache Read Input Tokens Regional', '1M tokens', '.36'), ('w', 'Million Cache Write Input Tokens Regional', '1M tokens', '4.5'), ('h', 'Million 1 hour Cache Write Input Tokens Regional', '1M tokens', '7.2'), ('b', 'Million Batch Input Tokens Regional', '1M tokens', '1.8')]:
            data['products'][sku] = {'attributes': {'regionCode': 'us-gov-west-1', 'servicename': 'Claude Test (Amazon Bedrock Edition)', 'usagetype': sku}}
            data['terms']['OnDemand'][sku] = {'t': {'effectiveDate': '2026-01-01', 'priceDimensions': {'d': {'unit': unit, 'description': desc, 'beginRange': '0', 'endRange': 'Inf', 'pricePerUnit': {'USD': price}}}}}
        return data

    def test_aws_gov_is_not_commercial_and_cache_ttls_do_not_mix(self):
        row = p.parse_aws(self.aws_fixture(), 'us-gov-west-1', 'https://example.com')[0]
        self.assertEqual(row['provider'], 'AWS GovCloud')
        self.assertEqual([row[k] for k in ['input', 'output', 'read', 'write']], [3.6, 18, .36, 4.5])

    def test_conflicting_price_meters_are_not_chosen_arbitrarily(self):
        data = self.aws_fixture()
        data['products']['i2'] = copy.deepcopy(data['products']['i'])
        data['terms']['OnDemand']['i2'] = copy.deepcopy(data['terms']['OnDemand']['i'])
        data['terms']['OnDemand']['i2']['t']['priceDimensions']['d']['pricePerUnit']['USD'] = '99'
        with self.assertRaises(ValueError): p.parse_aws(data, 'us-gov-west-1', 'https://example.com')

    def test_azure_pairs_exact_deployment_and_converts_1k(self):
        items = []
        for sku, price in [('gpt 4.1 Inp regnl', .00275), ('gpt 4.1 Outp regnl', .011), ('gpt 4.1 cached Inp regnl', .000688), ('gpt 4.1 Inp Data Zone', .009)]:
            items.append({'armRegionName': 'usgovvirginia', 'currencyCode': 'USD', 'type': 'Consumption', 'unitOfMeasure': '1K', 'skuName': sku, 'retailPrice': price, 'meterId': sku})
        rows = p.parse_azure(items, 'usgovvirginia', 'https://example.com')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['input'], 2.75)
        self.assertEqual(rows[0]['output'], 11)
        self.assertEqual(rows[0]['deployment'], 'Regional')

    def test_openai_standard_table_does_not_import_batch(self):
        md = '''### Standard pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| model-test | $2 | $0.2 | $2.5 | $10 | $4 | $0.4 | $5 | $15 |
### Batch pricing data
| model-test | $1 | $0.1 | $1.25 | $5 | $2 | $0.2 | $2.5 | $7.5 |
Short context: ≤272K input tokens.'''
        row = p.parse_openai(md, 'https://example.com')[0]
        self.assertEqual(row['input'], 2)
        self.assertEqual(row['longContext']['output'], 15)

    def test_anthropic_uses_5min_cache_column(self):
        md = '## Model pricing\n| Claude Test | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.30 / MTok | $15 / MTok |\n## Other pricing\n'
        row = p.parse_anthropic(md, 'https://example.com')[0]
        self.assertEqual(row['write'], 3.75)
        self.assertEqual(row['read'], .3)

    def test_names_with_plus_have_distinct_identity(self):
        args = ['AWS Bedrock', 'us-east-1', 'Command R', 'Regional', 'https://example.com']
        a = p.record(*args)
        args[2] += '+'
        self.assertNotEqual(a['id'], p.record(*args)['id'])

    def test_failed_refresh_preserves_last_good_timestamp_and_marks_stale(self):
        old = {'id': 'example', 'sourceKey': 'fixture', 'checkedAt': '2025-01-01T00:00:00Z', 'provider': 'Test', 'region': 'Global', 'name': 'Test', 'deployment': 'Standard', 'input': 1, 'output': 2}
        def fail(): raise RuntimeError('simulated source outage')
        result = p.refresh({'models': [old]}, [('fixture', 'Test', 'https://example.com', fail)])
        self.assertEqual(result['models'][0]['checkedAt'], old['checkedAt'])
        self.assertEqual(result['sources'][0]['status'], 'stale')
        self.assertEqual(result['sources'][0]['count'], 1)

if __name__ == '__main__': unittest.main()
