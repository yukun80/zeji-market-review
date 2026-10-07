import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from zeji_data.core import SHANGHAI, write_json, Client
from zeji_data.cooperation import account_check, handoff, ingest, resume
from zeji_data.etf_observation import parse_article

ARTICLE='''【ETF观察】9月29日行业主题ETF净流入17.47亿元
来源：证券之星ETF 2026-09-30 06:23:31
行业主题ETF基金合计资金净流入17.47亿元，近5个交易日累计净流入48.63亿元。
当日有112只行业主题ETF基金出现资金净流入，其中净流入排首位的是有色矿业ETF博时（562450），份额增加了14.96亿份，净流入额为13.86亿元。
当日有114只行业主题ETF基金出现资金净流出，其中净流出排首位的是通信ETF国泰（515880），份额减少了10.04亿份，净流出额为6.29亿元。'''

class CooperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.days=[(datetime(2026,8,1)+timedelta(days=i)).date().isoformat() for i in range(21)]
        write_json(self.root/'manifest.json',dict(review_date=self.days[-1],cutoff='2026-08-22T23:59:59+08:00'))
        write_json(self.root/'sessions.json',self.days)
        self.payload=dict(kind='price_history',object='600000',source='synthetic unit test',read_location='完整表',
            published_at='2026-08-21',material_complete=True,uncertain_fields=[],price_unit='CNY',adjustment='unadjusted',
            rows=[dict(date=d,close=100+i) for i,d in enumerate(self.days)])
    def submit(self,p=None):
        write_json(self.root/'input.json',p or self.payload)
        return ingest(self.root,self.root/'input.json')
    def test_article_signed_units_and_dates(self):
        r=parse_article(ARTICLE,'https://stock.stockstar.com/test','2026-09-29',datetime(2026,10,1,tzinfo=SHANGHAI))
        self.assertTrue(r['usable_before_cutoff']);self.assertEqual(r['estimated_daily_flow_yuan'],1747000000)
        self.assertEqual(r['body_fund_details'][1]['estimated_flow_yuan'],-629000000)
        self.assertEqual(r['inflow_count'],112);self.assertFalse(r['image_details_verified'])
    def test_next_day_article_excluded(self):
        r=parse_article(ARTICLE,'test','2026-09-29',datetime(2026,9,29,23,59,tzinfo=SHANGHAI))
        self.assertFalse(r['usable_before_cutoff'])
    def test_wrong_trade_date_excluded(self):
        r=parse_article(ARTICLE,'test','2026-09-30',datetime(2026,10,1,tzinfo=SHANGHAI))
        self.assertFalse(r['usable_before_cutoff'])
    def test_handoff_has_concrete_required_fields(self):
        r=handoff(self.root)
        self.assertEqual(r['status'],'waiting_for_materials')
        self.assertTrue(all(x['fields'] and x['url'] and x['impact'] for x in r['requests']))
    def test_resume_prices_without_network(self):
        self.assertEqual(self.submit()['status'],'validated')
        with patch('subprocess.run', side_effect=AssertionError('must not fetch again')):
            r=resume(self.root)
        self.assertEqual(r['validated_objects'],['600000'])
        data=json.loads((self.root/'manual-calculations.json').read_text(encoding='utf-8'))
        self.assertAlmostEqual(data['accepted'][0]['metrics']['return_20d_pct'],20)
    def test_wrong_unit(self):
        self.payload['price_unit']='USD';self.assertEqual(self.submit()['status'],'pending')
    def test_wrong_row_date(self):
        self.payload['rows'][-1]['date']='2026-08-23';self.assertEqual(self.submit()['status'],'pending')
    def test_market_material_and_industry_resume_clear_required_gaps(self):
        from zeji_data.pipeline import save_csv
        write_json(self.root/'coverage.json',[dict(classification='申万',level='一级行业',expected=1,complete_21_sessions=0)])
        save_csv(self.root/'industry-scan.csv',[dict(code='801010',classification='申万',level='一级行业',close=None,complete_21_sessions=False)])
        self.payload.update(object='801010',classification='申万',level='一级行业',price_unit='index_points')
        self.submit()
        market=dict(self.payload,kind='market_snapshot',object='market',price_unit='CNY',amount_unit='CNY',expected_count=1,
            rows=[dict(code='600000',date=self.days[-1],quote_time=self.days[-1]+'T15:01:00+08:00',close=10,change_pct=1,amount=100)])
        self.assertEqual(self.submit(market)['status'],'validated')
        result=resume(self.root)
        self.assertEqual(result['status'],'research_pending')
        self.assertEqual(json.loads((self.root/'merged-coverage.json').read_text(encoding='utf-8'))[0]['complete_21_sessions'],1)
    def test_future_material(self):
        self.payload['published_at']='2026-08-23';self.assertEqual(self.submit()['status'],'pending')
    def test_incomplete_screenshot(self):
        self.payload['material_complete']=False;self.payload['uncertain_fields']=['close'];self.assertEqual(self.submit()['status'],'pending')
    def test_conflict_not_overwritten(self):
        self.submit();self.payload['rows'][-1]['close']=130;self.submit()
        r=resume(self.root);self.assertEqual(r['validated_objects'],[]);self.assertEqual(r['conflicts'],['600000'])
    def test_missing_accounts_and_no_secret_disclosure(self):
        with patch.dict(os.environ,{},clear=True):self.assertFalse(account_check()[0]['configured'])
        with patch.dict(os.environ,{'TUSHARE_TOKEN':'test-secret-value'}):
            r=account_check();self.assertTrue(r[0]['configured']);self.assertNotIn('test-secret-value',json.dumps(r))
    def test_permission_denial_reported(self):
        c=Client(self.root,self.root/'cache',runner=lambda op,p:dict(status='auth_required',rows=[]))
        c.fetch('stock_ts');r=handoff(self.root)
        self.assertEqual(r['restrictions'][0]['status'],'auth_required')
    def test_original_preserved_and_repeat_idempotent(self):
        (self.root/'original.csv').write_text('date,close\n2026-08-21,120',encoding='utf-8')
        self.payload['original_file']='original.csv';a=self.submit();b=self.submit()
        self.assertEqual(a['id'],b['id']);self.assertEqual(len(list((self.root/'manual').glob('*'))),1)
        self.assertEqual((self.root/'manual'/a['id']/'original.csv').read_bytes(),(self.root/'original.csv').read_bytes())

if __name__=='__main__':unittest.main()
