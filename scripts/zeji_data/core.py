"""Bounded provider calls, provenance, caching and date-safe calculations."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]


def now():
    return datetime.now(SHANGHAI)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def number(value):
    try:
        result = float(str(value).replace(',', ''))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def day(value):
    text = str(value).strip()[:10]
    if len(text) == 8 and text.isdigit():
        text = f'{text[:4]}-{text[4:6]}-{text[6:]}'
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def parse_cutoff(value=None):
    if not value:
        return now()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    if parsed > now() + timedelta(minutes=1):
        raise ValueError('信息截止时间不能晚于现在')
    return parsed.astimezone(SHANGHAI)


def resolve_session(sessions, requested, cutoff):
    """A supplied holiday resolves backwards; an unclosed session is excluded."""
    ceiling = min(date.fromisoformat(requested) if requested else cutoff.date(), cutoff.date())
    valid = []
    for value in sessions:
        d = date.fromisoformat(value)
        close = datetime(d.year, d.month, d.day, 15, tzinfo=SHANGHAI)
        if d <= ceiling and close <= cutoff:
            valid.append(value)
    if not valid:
        raise ValueError('交易日历未覆盖指定日期，不能猜测最近交易日')
    return max(valid)


def price_metrics(rows, asof, sessions=None):
    """Missing dates are not compressed into apparently complete trading windows."""
    by_date = {}
    duplicates = set()
    for row in rows:
        d = day(row.get('date'))
        if not d or d > asof:
            continue
        if d in by_date and by_date[d] != row:
            duplicates.add(d)
        by_date[d] = row
    out = {'date': asof, 'close': None, 'return_1d_pct': None, 'return_5d_pct': None,
           'return_20d_pct': None, 'amount_ratio_20d': None, 'complete_21_sessions': False,
           'missing_dates': [], 'reason': None}
    if duplicates:
        out['reason'] = 'conflicting_duplicate_dates'
        return out
    if asof not in by_date or number(by_date[asof].get('close')) is None:
        out['reason'] = 'missing_target_date'
        return out
    out['close'] = number(by_date[asof]['close'])
    dates = sorted(d for d in (sessions or by_date) if d <= asof)
    if not dates or dates[-1] != asof:
        out['reason'] = 'calendar_mismatch'
        return out
    for n in (1, 5, 20):
        if len(dates) < n + 1:
            continue
        window = dates[-n - 1:]
        if any(d not in by_date or number(by_date[d].get('close')) is None for d in window):
            continue
        start = number(by_date[window[0]]['close'])
        if start and start > 0:
            out[f'return_{n}d_pct'] = (out['close'] / start - 1) * 100
    expected = dates[-21:]
    out['missing_dates'] = [d for d in expected if d not in by_date]
    out['complete_21_sessions'] = len(expected) == 21 and out['return_20d_pct'] is not None
    if len(expected) == 21 and all(d in by_date for d in expected):
        amounts = [number(by_date[d].get('amount')) for d in expected]
        if all(a is not None and a >= 0 for a in amounts) and sum(amounts[:-1]) > 0:
            out['amount_ratio_20d'] = amounts[-1] / (sum(amounts[:-1]) / 20)
    out['reason'] = None if out['complete_21_sessions'] else 'incomplete_history'
    return out


def publication_allowed(published, cutoff):
    """Date-only documents are conservatively available after that local day ends."""
    if not published:
        return False
    try:
        if len(str(published)) == 10:
            dt = datetime.fromisoformat(str(published) + 'T23:59:59').replace(tzinfo=SHANGHAI)
        else:
            dt = datetime.fromisoformat(str(published))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SHANGHAI)
        return dt <= cutoff
    except ValueError:
        return False


class Client:
    def __init__(self, output, cache, timeout=90, retries=2, runner=None, sleeper=time.sleep):
        self.output, self.cache = Path(output), Path(cache)
        self.timeout, self.retries = timeout, min(2, max(0, retries))
        self.runner, self.sleeper = runner or self._run, sleeper
        manifest = self.output / 'sources.json'
        self.records = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else []
        self.blocked = set()
        self.registry = json.loads((ROOT / 'references/tool-pool.json').read_text(encoding='utf-8'))

    def _run(self, operation, params):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        try:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/zeji_data/worker.py'),
                                     operation, json.dumps(params, ensure_ascii=True)],
                                    capture_output=True, text=True, encoding='utf-8',
                                    timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            return {'status': 'timeout', 'rows': [], 'error': 'provider call exceeded time limit'}
        try:
            value = json.loads(result.stdout)
            return value
        except (ValueError, TypeError):
            return {'status': 'error', 'rows': [], 'error': 'provider returned invalid output'}

    def fetch(self, operation, params=None, ttl_hours=12):
        params = params or {}
        spec = self.registry['operations'][operation]
        key = hashlib.sha256(json.dumps([operation, params], sort_keys=True).encode()).hexdigest()
        cache_path = self.cache / (key + '.json')
        value = None
        if cache_path.exists():
            saved = json.loads(cache_path.read_text(encoding='utf-8'))
            age = (now() - datetime.fromisoformat(saved['fetched_at'])).total_seconds()
            if 0 <= age < ttl_hours * 3600:
                value = dict(saved, cache_hit=True)
        if value is None:
            if spec['host'] in self.blocked:
                value = {'status': 'blocked', 'rows': [], 'error': 'source unavailable earlier in this run'}
            else:
                for attempt in range(self.retries + 1):
                    value = self.runner(operation, params)
                    if value.get('status') not in {'timeout', 'transient'}:
                        break
                    if attempt < self.retries:
                        self.sleeper(2 ** attempt)
                if value.get('status') in {'blocked', 'auth_required'}:
                    self.blocked.add(spec['host'])
            value = dict(value, fetched_at=now().isoformat(), cache_hit=False)
            if value.get('status') == 'ok' and value.get('rows'):
                write_json(cache_path, value)
        record = dict(value, operation=operation, parameters=params, source_url=params.get('url', spec['url']),
                      provider=spec['provider'], source_id=f'S{len(self.records) + 1:04d}')
        record['row_count'] = len(record.get('rows', []))
        self.records.append(record)
        write_json(self.output / 'raw' / f'{record["source_id"]}_{operation}.json', record)
        write_json(self.output / 'sources.json', [{k: v for k, v in x.items() if k != 'rows'}
                                                  for x in self.records])
        print(f'{operation}: {record["status"]}, {len(record.get("rows", []))} rows', flush=True)
        return record

    def first(self, calls):
        """Only equivalent datasets should be passed here; classifications stay separate."""
        last = None
        for operation, params in calls:
            last = self.fetch(operation, params)
            if last.get('status') == 'ok' and last.get('rows'):
                return last
        return last
