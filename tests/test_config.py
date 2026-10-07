import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from zeji_data.config import load_local_config
from zeji_data.cooperation import account_check


class ConfigTests(unittest.TestCase):
    def test_local_loading_precedence_and_redaction(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'TUSHARE_TOKEN': 'test-private-token'}), encoding='utf-8-sig')
            load_local_config(path)
            self.assertTrue(account_check()[0]['configured'])
            self.assertEqual(os.environ['TUSHARE_TOKEN'], 'test-private-token')
            self.assertNotIn('test-private-token', json.dumps(account_check()))
            os.environ['TUSHARE_TOKEN'] = 'environment-value'
            load_local_config(path)
            self.assertEqual(os.environ['TUSHARE_TOKEN'], 'environment-value')

    def test_missing_empty_and_malformed(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / 'config.json'
            load_local_config(path)
            path.write_text('{"TUSHARE_TOKEN":""}', encoding='utf-8')
            load_local_config(path)
            self.assertFalse(account_check()[0]['configured'])
            path.write_text('test-private-broken', encoding='utf-8')
            with self.assertRaises(ValueError) as error:
                load_local_config(path)
            self.assertNotIn('test-private-broken', str(error.exception))
