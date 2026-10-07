"""One bounded provider task per process. Never prints credentials or arbitrary tracebacks."""
from __future__ import annotations

import contextlib
import io
import ipaddress
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
SH = timezone(timedelta(hours=8))


def install_http_limits():
    import requests
    original = requests.sessions.Session.request
    last = [0.0]

    def request(self, method, url, **kwargs):
        # Older upstream wrappers use HTTP for these public sites; use their HTTPS endpoints.
        parsed = urlparse(url)
        if parsed.scheme == 'http' and parsed.hostname and any(parsed.hostname == h or parsed.hostname.endswith('.' + h)
                for h in ['cninfo.com.cn', 'eastmoney.com', 'sina.com.cn']):
            url = 'https://' + url[7:]
        delay = 0.25 - (time.monotonic() - last[0])
        if delay > 0:
            time.sleep(delay)
        kwargs['timeout'] = (8, 20)
        kwargs['verify'] = True
        response = original(self, method, url, **kwargs)
        last[0] = time.monotonic()
        response.raise_for_status()
        return response

    requests.sessions.Session.request = request


def table_rows(value):
    import pandas as pd
    if isinstance(value, pd.DataFrame):
        # ISO dates rather than pandas' default millisecond timestamps.
        return json.loads(value.to_json(orient='records', date_format='iso', force_ascii=False))
    return value


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('document URL must be public HTTPS')
    for result in socket.getaddrinfo(parsed.hostname, parsed.port or 443):
        if not ipaddress.ip_address(result[4][0]).is_global:
            raise ValueError('non-public address rejected')


def get_document(url):
    import requests
    from bs4 import BeautifulSoup
    if 'cninfo.com.cn/new/disclosure/detail' in url:
        from urllib.parse import parse_qs
        query = parse_qs(urlparse(url).query)
        value = requests.post('https://www.cninfo.com.cn/new/announcement/bulletin_detail',
            data={'announceId': query['announcementId'][0], 'announcementTime': query.get('announcementTime', [''])[0]}).json()
        detail = value.get('announcement') or value
        attachment = detail.get('adjunctUrl')
        if not attachment:
            raise ValueError('announcement attachment unavailable')
        url = 'https://static.cninfo.com.cn/' + attachment.lstrip('/')
    for _ in range(5):
        public_url(url)
        response = requests.get(url, allow_redirects=False, stream=True)
        if 300 <= response.status_code < 400:
            from urllib.parse import urljoin
            url = urljoin(url, response.headers['Location'])
            response.close()
            continue
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 25 * 1024 * 1024:
                response.close()
                raise ValueError('document exceeds 25 MiB')
            chunks.append(chunk)
        content = b''.join(chunks)
        if content.startswith(b'%PDF'):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            return [{'url': url, 'page': i + 1, 'text': page.extract_text() or ''}
                    for i, page in enumerate(reader.pages)]
        response._content = content
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav']):
            tag.decompose()
        from urllib.parse import urljoin
        images = [urljoin(url, tag.get('src') or tag.get('data-src') or '') for tag in soup.find_all('img') if tag.get('src') or tag.get('data-src')]
        return [{'url': url, 'page': None, 'text': soup.get_text('\n', strip=True), 'images': images}]
    raise ValueError('too many redirects')


def market():
    import requests
    url = 'https://82.push2.eastmoney.com/api/qt/clist/get'
    params = {'pn': 1, 'pz': 100, 'po': 1, 'np': 1, 'ut': 'bd1d9ddb04089700cf9c27f6f7426281',
              'fltt': 2, 'invt': 2, 'fid': 'f12',
              'fs': 'm:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048',
              'fields': 'f2,f3,f5,f6,f12,f13,f14,f124'}
    rows, seen, total = [], set(), None
    for page in range(1, 201):
        params['pn'] = page
        value = requests.get(url, params=params).json().get('data') or {}
        total = value.get('total', total)
        batch = value.get('diff') or []
        if isinstance(batch, dict):
            batch = list(batch.values())
        if not batch:
            break
        added = 0
        for item in batch:
            key = (item.get('f13'), item.get('f12'))
            if key in seen:
                continue
            seen.add(key)
            stamp = item.get('f124')
            moment = datetime.fromtimestamp(stamp, SH).isoformat() if isinstance(stamp, (int, float)) and stamp > 0 else None
            rows.append({'code': item.get('f12'), 'market_id': item.get('f13'), 'name': item.get('f14'),
                         'close': item.get('f2'), 'change_pct': item.get('f3'), 'volume_lots': item.get('f5'),
                         'amount': item.get('f6'), 'quote_time': moment, 'date': moment[:10] if moment else None})
            added += 1
        if total is not None and len(rows) >= total:
            break
        if not added:
            break
    return {'status': 'ok' if total == len(rows) and rows else 'partial', 'rows': rows,
            'expected_count': total, 'actual_url': url, 'complete': total == len(rows)}


def baostock_query(operation, params):
    import baostock as bs
    login = bs.login()
    if login.error_code != '0':
        raise ConnectionError('BaoStock login failed')
    try:
        if operation == 'calendar_bs':
            rs = bs.query_trade_dates(start_date=params['start_date'], end_date=params['end_date'])
        else:
            code = params['symbol']
            if code.startswith(('4', '8', '92')):
                return {'status': 'unsupported', 'rows': [], 'error': 'BaoStock Beijing coverage not assumed'}
            code = ('sh.' if code.startswith('6') else 'sz.') + code
            rs = bs.query_history_k_data_plus(code, 'date,code,open,high,low,close,volume,amount,adjustflag,tradestatus',
                                               start_date=params['start_date'], end_date=params['end_date'],
                                               frequency='d', adjustflag='3')
        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        if rs.error_code != '0':
            raise ConnectionError('BaoStock query failed')
        return rows
    finally:
        bs.logout()


def dispatch(operation, params):
    import requests
    registry = json.loads((ROOT / 'references/tool-pool.json').read_text(encoding='utf-8'))['operations']
    if operation not in registry:
        raise ValueError('unknown operation')
    if operation == 'market':
        return market()
    if operation in {'calendar_bs', 'stock_bs'}:
        return baostock_query(operation, params)
    if operation == 'document':
        return get_document(params['url'])
    if operation == 'stock_ts':
        token = os.environ.get('TUSHARE_TOKEN')
        if not token:
            return {'status': 'auth_required', 'rows': [], 'error': 'TUSHARE_TOKEN is not configured'}
        code = params['symbol']
        suffix = 'SH' if code.startswith('6') else ('BJ' if code.startswith(('4', '8', '92')) else 'SZ')
        payload = {'api_name': 'daily', 'token': token, 'params': {'ts_code': code + '.' + suffix,
                   'start_date': params['start_date'].replace('-', ''), 'end_date': params['end_date'].replace('-', '')},
                   'fields': 'ts_code,trade_date,open,high,low,close,vol,amount'}
        value = requests.post('https://api.tushare.pro', json=payload).json()
        if value.get('code') != 0:
            return {'status': 'auth_required', 'rows': [], 'error': 'Tushare account permission unavailable'}
        data = value.get('data') or {}
        return [dict(zip(data['fields'], row)) for row in data.get('items', [])]
    if operation == 'sec_filings':
        identity = os.environ.get('SEC_USER_AGENT')
        if not identity:
            return {'status': 'needs_identity', 'rows': [], 'error': 'Set SEC_USER_AGENT to a real application/contact identity, not an API key'}
        cik = str(params['cik']).zfill(10)
        if not cik.isdigit() or len(cik) != 10:
            raise ValueError('CIK must contain at most 10 digits')
        url = 'https://data.sec.gov/submissions/CIK' + cik + '.json'
        data = requests.get(url, headers={'User-Agent': identity}).json()
        recent = data.get('filings', {}).get('recent', {})
        return [dict(zip(recent, values)) for values in zip(*recent.values())]
    if operation == 'index':
        p = {'fields1': 'f1,f2,f3,f4,f5,f6', 'fields2': 'f51,f52,f53,f54,f55,f56,f57',
             'ut': '7eea3edcaed734bea9cbfc24409ed989', 'klt': 101, 'fqt': 0,
             'secid': params.get('symbol', '1.000300'), 'beg': params['start_date'], 'end': params['end_date']}
        value = requests.get('https://push2his.eastmoney.com/api/qt/stock/kline/get', params=p).json()
        return [dict(zip(['date', 'open', 'close', 'high', 'low', 'volume', 'amount'], line.split(',')))
                for line in (value.get('data') or {}).get('klines', [])]
    import akshare as ak
    # pandas 3 no longer accepts literal workbook bytes; some AKShare adapters still supply them.
    import pandas as pd
    read_excel = pd.read_excel
    def compatible_excel(value, *args, **kwargs):
        return read_excel(io.BytesIO(value) if isinstance(value, bytes) else value, *args, **kwargs)
    pd.read_excel = compatible_excel
    return table_rows(getattr(ak, registry[operation]['function'])(**params))


def main():
    install_http_limits()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            value = dispatch(sys.argv[1], json.loads(sys.argv[2]))
        if isinstance(value, dict) and 'status' in value:
            result = value
        else:
            result = {'status': 'ok' if value else 'empty', 'rows': value or []}
    except Exception as exc:
        import requests
        if isinstance(exc, requests.HTTPError):
            code = exc.response.status_code
            status = 'blocked' if code in (401, 403, 429) else ('transient' if code >= 500 else 'error')
            message = f'HTTP {code}'
        elif isinstance(exc, (requests.Timeout, TimeoutError)):
            status, message = 'timeout', 'network timeout'
        elif isinstance(exc, (requests.ConnectionError, ConnectionError, socket.gaierror)):
            status, message = 'transient', 'network connection failed'
        elif isinstance(exc, ModuleNotFoundError):
            status, message = 'missing_dependency', 'run setup using requirements.txt'
        else:
            status, message = 'error', type(exc).__name__ + ': provider response could not be processed'
        result = {'status': status, 'rows': [], 'error': message}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False, default=str))


if __name__ == '__main__':
    main()
