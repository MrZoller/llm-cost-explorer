import copy
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('prices', Path(__file__).resolve().parents[1] / 'scripts/update_prices.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class PriceTests(unittest.TestCase):
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
