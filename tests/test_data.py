import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from zeji_data.core import Client, SHANGHAI, price_metrics, publication_allowed, resolve_session
from zeji_data.pipeline import calendar, company_data, etf_data, market_summary, stock_data


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = [(datetime(2026, 8, 1) + timedelta(days=i)).date().isoformat() for i in range(21)]
        self.rows = [{'date': d, 'close': 100+i, 'amount': 1000} for i, d in enumerate(self.sessions)]

    def client(self, runner, retries=0):
        return Client(self.root/'out', self.root/'cache', runner=runner, retries=retries, sleeper=lambda _: None)

    def test_returns_against_hand_calculation(self):
        result = price_metrics(self.rows, self.sessions[-1], self.sessions)
        for n in (1,5,20):
            self.assertAlmostEqual(result[f'return_{n}d_pct'], (120/(120-n)-1)*100)
        self.assertEqual(result['amount_ratio_20d'], 1)

    def test_missing_day_not_compressed(self):
        result = price_metrics(self.rows[:10]+self.rows[11:], self.sessions[-1], self.sessions)
        self.assertIsNone(result['return_20d_pct'])
        self.assertIn(self.sessions[10], result['missing_dates'])

    def test_conflicting_duplicate(self):
        result = price_metrics(self.rows+[dict(self.rows[-1], close=130)], self.sessions[-1], self.sessions)
        self.assertEqual(result['reason'], 'conflicting_duplicate_dates')

    def test_unclosed_and_holiday(self):
        sessions = ['2026-09-29', '2026-09-30', '2026-10-08']
        self.assertEqual(resolve_session(sessions, None, datetime(2026,10,7,16,tzinfo=SHANGHAI)), '2026-09-30')
        self.assertEqual(resolve_session(sessions, None, datetime(2026,9,30,14,tzinfo=SHANGHAI)), '2026-09-29')

    def test_publication_boundary(self):
        cutoff = datetime(2026,9,30,15,tzinfo=SHANGHAI)
        self.assertFalse(publication_allowed('2026-10-01', cutoff))
        self.assertFalse(publication_allowed('2026-09-30', cutoff))
        self.assertTrue(publication_allowed('2026-09-30T14:00:00+08:00', cutoff))

    def test_timeout_bounded_and_not_cached(self):
        calls=[]
        c=self.client(lambda op,p: calls.append(op) or {'status':'timeout','rows':[]}, retries=2)
        self.assertEqual(c.fetch('stock')['status'], 'timeout')
        self.assertEqual(len(calls),3)
        self.assertFalse(list((self.root/'cache').glob('*')))

    def test_empty_not_zero(self):
        result=price_metrics([],self.sessions[-1], self.sessions)
        self.assertIsNone(result['close'])
        self.assertIsNone(result['return_1d_pct'])

    def test_stale_stock_falls_back(self):
        calls=[]
        def runner(op,p):
            calls.append(op)
            return {'status':'ok','rows':self.rows[:-1] if op=='stock' else self.rows}
        result=stock_data(self.client(runner),'600000',self.sessions[-1],self.sessions[0],self.sessions)
        self.assertEqual(calls,['stock','stock_bs'])
        self.assertTrue(result['complete_21_sessions'])

    def test_stale_calendar_falls_back(self):
        calls=[]
        def runner(op,p):
            calls.append(op)
            return {'status':'ok','rows':[{'trade_date':'2020-01-01' if op=='calendar' else '2026-09-30'}]}
        result=calendar(self.client(runner),None,datetime(2026,10,7,16,tzinfo=SHANGHAI))
        self.assertEqual(result[0],'2026-09-30')
        self.assertEqual(calls,['calendar','calendar_bs'])

    def test_intraday_quote_rejected(self):
        r={'source_id':'sample','complete':True,'rows':[{'date':'2026-09-30','quote_time':'2026-09-30T11:00:00+08:00','close':10,'amount':100,'change_pct':1}]}
        result=market_summary(r,'2026-09-30')
        self.assertEqual(result['status'],'partial')
        self.assertIsNone(result['advancing'])

    def test_holiday_announcement_query(self):
        calls=[]
        def runner(op,p):
            calls.append((op,p))
            return {'status':'empty','rows':[]}
        company_data(self.client(runner),'600000','2026-09-30',datetime(2026,10,7,16,tzinfo=SHANGHAI))
        self.assertEqual(calls[1][1]['end_date'],'20261007')

    def test_etf_actual_date_required(self):
        c=self.client(lambda op,p: {'status':'ok','rows':[{'统计日期':'2026-09-28','基金代码':'510300'}]})
        result=etf_data(c,'2026-09-30',['2026-09-29','2026-09-30'])
        self.assertTrue(all(r['status']=='partial' for r in result['shanghai']))

    def test_no_account_skips_tushare(self):
        calls=[]
        with patch.dict('os.environ',{},clear=True):
            stock_data(self.client(lambda op,p: calls.append(op) or {'status':'empty','rows':[]}), '600000',self.sessions[-1],self.sessions[0],self.sessions)
        self.assertEqual(calls,['stock','stock_bs'])

    def test_blocked_source_stops_and_cache_provenance(self):
        calls=[]
        c=self.client(lambda op,p: calls.append(op) or {'status':'blocked','rows':[]}, retries=2)
        c.fetch('stock'); c.fetch('stock')
        self.assertEqual(len(calls),1)
        c=self.client(lambda op,p: {'status':'ok','rows':[{'trade_date':'2026-09-30'}]})
        a=c.fetch('calendar');b=c.fetch('calendar')
        self.assertTrue(b['cache_hit'])
        self.assertEqual(a['fetched_at'],b['fetched_at'])


if __name__ == '__main__':
    unittest.main()
