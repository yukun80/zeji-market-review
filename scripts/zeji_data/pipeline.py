"""Collect a reproducible data pack. Codex completes the evidence-led narrative."""
from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from .core import SHANGHAI, day, now, number, price_metrics, publication_allowed, resolve_session, write_json


def pick(row, *keys):
    return next((row[k] for k in keys if k in row and row[k] is not None), None)


def prices(rows, operation):
    result = []
    for r in rows:
        amount = number(pick(r, 'amount', '成交额'))
        if operation == 'stock_ts' and amount is not None:
            amount *= 1000  # Tushare daily amount is thousands of RMB.
        # SW analysis contains volume and a share, not a yuan turnover amount.
        if operation == 'sw_daily':
            amount = None
        result.append({'date': day(pick(r, 'date', '日期', '发布日期', 'trade_date')),
                       'close': number(pick(r, 'close', '收盘', '收盘指数')),
                       'amount': amount, 'code': str(pick(r, 'code', '代码', '指数代码', '股票代码') or '')})
    return result


def save_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text('', encoding='utf-8-sig')
        return
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                             for k, v in row.items()})


def market_summary(record, asof, cutoff=None):
    rows = record.get('rows', [])
    dated = [r for r in rows if r.get('date') == asof and r.get('quote_time')
             and datetime.fromisoformat(r['quote_time']) >= datetime.fromisoformat(asof + 'T15:00:00+08:00')
             and (cutoff is None or datetime.fromisoformat(r['quote_time']) <= cutoff)]
    valid = [r for r in dated if number(r.get('close')) is not None and number(r.get('change_pct')) is not None
             and number(r.get('amount')) is not None and number(r.get('amount')) > 0]
    date_counts = dict(Counter(r.get('date') or 'unknown' for r in rows))
    return {'status': 'ok' if record.get('complete') and len(dated) == len(rows) and valid else 'partial',
            'expected_count': record.get('expected_count'), 'received_count': len(rows),
            'quote_date_counts': date_counts, 'target_date_count': len(dated), 'traded_valid_count': len(valid),
            'advancing': sum(number(r['change_pct']) > 0 for r in valid) if valid else None,
            'declining': sum(number(r['change_pct']) < 0 for r in valid) if valid else None,
            'unchanged': sum(number(r['change_pct']) == 0 for r in valid) if valid else None,
            'turnover_yuan': sum(number(r['amount']) for r in valid) if valid else None,
            'breadth': sum(number(r['change_pct']) > 0 for r in valid) / len(valid) if valid else None,
            'source_id': record['source_id'],
            'note': '仅统计行情日期匹配且成交额大于零的有效股票；不同日期、停牌或缺数据另列，非净流入。'}


def calendar(client, requested, cutoff):
    ceiling = requested or cutoff.date().isoformat()
    sessions = []
    calls = [('calendar', {}), ('calendar_bs', {
        'start_date': (date.fromisoformat(ceiling) - timedelta(days=550)).isoformat(), 'end_date': cutoff.date().isoformat()})]
    for operation, params in calls:
        record = client.fetch(operation, params)
        sessions = sorted(set(filter(None, [day(pick(r, 'trade_date', 'calendar_date')) for r in record.get('rows', [])
                                             if str(r.get('is_trading_day', '1')) == '1'])))
        if sessions and min(sessions) <= ceiling and max(sessions) >= (date.fromisoformat(ceiling) - timedelta(days=20)).isoformat():
            break
    else:
        raise ValueError('未取得覆盖目标日期的交易日历；不能猜测最近交易日')
    return resolve_session(sessions, requested, cutoff), sessions, record['source_id']


def industry_scan(client, asof, sessions, start):
    all_rows, coverage = [], []
    for level, operation in [('一级行业', 'sw_l1'), ('二级行业', 'sw_l2')]:
        directory = client.fetch(operation)
        daily = client.fetch('sw_daily', {'symbol': level, 'start_date': start.replace('-', ''), 'end_date': asof.replace('-', '')})
        members = directory.get('rows', [])
        grouped = {}
        for row in daily.get('rows', []):
            code = str(pick(row, '指数代码', '代码') or '').split('.')[0]
            grouped.setdefault(code, []).append(row)
        valid = complete = 0
        for item in members:
            code = str(pick(item, '行业代码', '指数代码') or '').split('.')[0]
            history = grouped.get(code, [])
            source = daily['source_id']
            metrics = price_metrics(prices(history, 'sw_daily'), asof, sessions)
            # Only isolated missing histories are retried from the same source; a whole-source failure switches classification below.
            if daily.get('status') == 'ok' and not metrics['complete_21_sessions']:
                extra = client.fetch('sw_history', {'symbol': code, 'period': 'day'})
                metrics = price_metrics(prices(extra.get('rows', []), 'sw_history'), asof, sessions)
                source = extra['source_id']
            valid += metrics['close'] is not None
            complete += metrics['complete_21_sessions']
            all_rows.append(dict(metrics, classification='申万', level=level, code=code,
                                 name=pick(item, '行业名称', '指数名称'), parent=item.get('上级行业'),
                                 member_count=item.get('成份个数'), source_id=source,
                                 directory_source=directory['source_id'], directory_asof='current_only'))
        coverage.append({'classification': '申万', 'level': level, 'expected': len(members) if members else None,
                         'valid_target_date': valid, 'complete_21_sessions': complete,
                         'directory_historical_verified': False,
                         'missing_codes': [r['code'] for r in all_rows if r['level'] == level and not r['complete_21_sessions']]})
    if not any(r['complete_21_sessions'] for r in all_rows):
        directory = client.fetch('em_industries')
        valid = complete = 0
        for item in directory.get('rows', []):
            name, code = item.get('板块名称'), item.get('板块代码')
            history = client.fetch('em_industry_history', {'symbol': code or name, 'start_date': start.replace('-', ''),
                'end_date': asof.replace('-', ''), 'period': '日k', 'adjust': ''})
            metrics = price_metrics(prices(history.get('rows', []), 'em_industry_history'), asof, sessions)
            valid += metrics['close'] is not None
            complete += metrics['complete_21_sessions']
            all_rows.append(dict(metrics, classification='东方财富', level='供应商行业分类', code=code, name=name,
                                 source_id=history['source_id'], directory_source=directory['source_id'], directory_asof='current_only'))
        coverage.append({'classification': '东方财富', 'level': '供应商行业分类',
                         'expected': len(directory.get('rows', [])) or None,
                         'valid_target_date': valid, 'complete_21_sessions': complete,
                         'directory_historical_verified': False,
                         'missing_codes': [r['code'] for r in all_rows if r['classification'] == '东方财富' and not r['complete_21_sessions']]})
    return all_rows, coverage


def choose_directions(rows, max_directions=3):
    # A reproducible research order, not an entry signal or a claim of market consensus.
    usable = [r for r in rows if r['complete_21_sessions'] and r['level'] != '二级行业']
    by_strength = sorted(usable, key=lambda r: r['return_5d_pct'], reverse=True)
    selected = [dict(r, selection_reason='近5日相对表现优先补证') for r in by_strength[:max_directions]]
    selected_codes = {r['code'] for r in selected}
    weak = [dict(r, selection_reason='价格较弱方向，主动检索经营改善与反证')
            for r in by_strength[-2:] if r['code'] not in selected_codes]
    return selected, weak


def stock_data(client, symbol, asof, start, sessions=None):
    p = {'symbol': symbol, 'start_date': start.replace('-', ''), 'end_date': asof.replace('-', ''), 'period': 'daily', 'adjust': ''}
    calls = [('stock', p), ('stock_bs', {'symbol': symbol, 'start_date': start, 'end_date': asof})]
    if os.environ.get('TUSHARE_TOKEN'):
        calls.append(('stock_ts', {'symbol': symbol, 'start_date': start, 'end_date': asof}))
    choices = []
    for operation, params in calls:
        candidate = client.fetch(operation, params)
        check = price_metrics(prices(candidate.get('rows', []), operation), asof, sessions)
        choices.append((candidate, check))
        if check['complete_21_sessions']:
            break
    result, _ = max(choices, key=lambda x: (x[1]['complete_21_sessions'], x[1]['close'] is not None, sum(x[1][f'return_{n}d_pct'] is not None for n in (1,5,20))))
    normalized = prices(result.get('rows', []), result['operation'])
    write_json(client.output / 'normalized' / f'stock_{symbol}.json', normalized)
    metrics = price_metrics(normalized, asof, sessions)
    return dict(metrics, symbol=symbol, source_id=result['source_id'], adjustment='unadjusted',
                adjustment_note='非复权收益需另查期间分红送转；不得用于跨除权日的直接收益比较')


def company_data(client, symbol, asof, cutoff, documents=1):
    info = client.fetch('company', {'symbol': symbol})
    start = (date.fromisoformat(asof) - timedelta(days=120)).strftime('%Y%m%d')
    news = client.fetch('announcements', {'symbol': symbol, 'market': '沪深京', 'start_date': start, 'end_date': cutoff.date().strftime('%Y%m%d')})
    eligible, excluded, docs = [], [], []
    for row in news.get('rows', []):
        published = pick(row, '公告时间', '公告日期', 'announcementTime')
        title = pick(row, '公告标题', '标题', 'announcementTitle')
        url = pick(row, '公告链接', '公告网址', '链接', 'adjunctUrl')
        if url and str(url).startswith('http://'):
            url = 'https://' + str(url)[7:]
        entry = {'title': title, 'published_at': str(published) if published else None,
                 'url': url, 'source_id': news['source_id'], 'after_session': str(published)[:10] > asof if published else None}
        (eligible if publication_allowed(entry['published_at'], cutoff) else excluded).append(entry)
    eligible.sort(key=lambda r: r['published_at'], reverse=True)
    for row in [r for r in eligible if r['url']][:documents]:
        doc = client.fetch('document', {'url': row['url']}, ttl_hours=24 * 365)
        docs.append(dict(row, document_source=doc['source_id'], text_available=any(r.get('page') and len(r.get('text', '')) > 100 for r in doc.get('rows', []))))
    result = {'symbol': symbol, 'profile_source': info['source_id'], 'profile_asof': 'current_only',
              'eligible_announcements': eligible, 'excluded_announcements': excluded, 'documents': docs,
              'business_verified': False, 'note': '已取得文字不等于业务关联已核实；由 Codex 阅读原文并写出引用位置。'}
    write_json(client.output / 'normalized' / f'company_{symbol}.json', result)
    return result


def etf_data(client, asof, sessions, symbol=None):
    earlier = [d for d in sessions if d < asof]
    dates = ([earlier[-1]] if earlier else []) + [asof]
    records = [client.fetch('etf_sse', {'date': d.replace('-', '')}) for d in dates]
    sz = client.fetch('etf_szse')
    holdings = client.fetch('etf_holdings', {'symbol': symbol, 'date': asof[:4]}) if symbol else None
    result = {'shanghai': [{'date_requested': d, 'source_id': r['source_id'], 'status': 'ok' if r.get('rows') and all(day(x.get('统计日期')) == d for x in r['rows']) and len({x.get('基金代码') for x in r['rows']}) == len(r['rows']) else 'partial',
                             'actual_dates': sorted(set(filter(None, [day(x.get('统计日期')) for x in r.get('rows', [])])))}
                           for d, r in zip(dates, records)],
              'shenzhen': {'source_id': sz['source_id'], 'status': sz['status'], 'data_date': None,
                           'note': '该表未提供份额统计日期；上市日期不是统计日期，不据此推算本次日变化。'},
              'holdings': {'source_id': holdings['source_id'], 'status': holdings['status'],
                           'note': '季度披露持仓；须查基金报告实际发布时间，历史判断暂不采用未核实发布时间的持仓。'} if holdings else None,
              'net_inflow_estimated': False, 'note': '本工具保存份额证据，不把份额差或成交额写成精确资金净流入。'}
    write_json(client.output / 'normalized/etf.json', result)
    return result


def scan(client, requested, cutoff, max_directions=3, per_direction=2, etf_symbol='510300'):
    asof, sessions, calendar_source = calendar(client, requested, cutoff)
    write_json(client.output / 'sessions.json', sessions)
    start = (date.fromisoformat(asof) - timedelta(days=75)).isoformat()
    manifest = {'review_date': asof, 'requested_date': requested, 'cutoff': cutoff.isoformat(),
                'calendar_source': calendar_source, 'started_at': now().isoformat(), 'status': 'running'}
    write_json(client.output / 'manifest.json', manifest)
    snapshot = client.fetch('market', ttl_hours=0)
    market = market_summary(snapshot, asof, cutoff)
    write_json(client.output / 'normalized/market.json', market)
    benchmark = client.fetch('index', {'symbol': '1.000300', 'start_date': start.replace('-', ''), 'end_date': asof.replace('-', '')})
    benchmark_metrics = price_metrics(prices(benchmark.get('rows', []), 'index'), asof, sessions)
    write_json(client.output / 'normalized/benchmark.json', benchmark_metrics)
    industry, coverage = industry_scan(client, asof, sessions, start)
    for row in industry:
        for n in (1, 5, 20):
            a, b = row[f'return_{n}d_pct'], benchmark_metrics[f'return_{n}d_pct']
            row[f'relative_{n}d_pp'] = a - b if a is not None and b is not None else None
    save_csv(client.output / 'industry-scan.csv', industry)
    write_json(client.output / 'coverage.json', coverage)
    strongest, weak = choose_directions(industry, max_directions)
    # Current constituents cannot create a historically valid candidate pool.
    latest_closed = resolve_session(sessions, None, now())
    members_allowed = asof == latest_closed
    snapshot_by_code = {r['code']: r for r in snapshot.get('rows', []) if r.get('date') == asof}
    candidates, seen, member_notes = [], set(), []
    for direction in strongest:
        if not members_allowed:
            member_notes.append({'industry': direction['name'], 'reason': 'historical_members_unavailable'})
            continue
        operation = 'sw_members' if direction['classification'] == '申万' else 'em_members'
        result = client.fetch(operation, {'symbol': direction['code']})
        members = []
        for row in result.get('rows', []):
            raw_code = str(pick(row, '证券代码', '代码', '股票代码') or '')
            match = re.search(r'\d{6}', raw_code)
            if not match:
                continue
            code = match.group()
            if code in snapshot_by_code:
                members.append(snapshot_by_code[code])
        member_notes.append({'industry': direction['name'], 'source_id': result['source_id'],
                             'current_constituents': True, 'matched_dated_quotes': len(members),
                             'advancing': sum((number(r.get('change_pct')) or 0) > 0 for r in members),
                             'note': '当日成员参与仅作线索，不能代替多日扩散证据。'})
        members.sort(key=lambda r: number(r.get('amount')) or 0, reverse=True)
        added = 0
        for item in members:
            code = item['code']
            if code in seen:
                continue
            seen.add(code)
            stock = stock_data(client, code, asof, start, sessions)
            company = company_data(client, code, asof, cutoff)
            candidates.append({'symbol': code, 'name': item['name'], 'industry': direction['name'],
                               'stage': '待核实', 'selection_reason': '重点方向中成交活跃，等待公司业务和反证核实',
                               'price': stock, 'company': company})
            added += 1
            if added >= per_direction:
                break
    save_csv(client.output / 'candidate-pool.csv', [{'symbol': x['symbol'], 'name': x['name'],
        'industry': x['industry'], 'stage': x['stage'], 'reason': x['selection_reason']} for x in candidates])
    write_json(client.output / 'candidates.json', candidates)
    write_json(client.output / 'member-coverage.json', member_notes)
    client.fetch('themes')
    etf_data(client, asof, sessions, etf_symbol)
    tasks = []
    for direction in strongest + weak:
        tasks.append({'direction': direction['name'], 'reason': direction['selection_reason'],
                      'queries': [f'{direction["name"]} 订单 供需 价格 经营 {asof[:4]}',
                                  f'{direction["name"]} 需求下降 竞争 利润 风险 {asof[:4]}'],
                      'cutoff': cutoff.isoformat(), 'requirement': '搜索并读公开原始资料；记录发布时间、原文链接和支持或反对的结论。'})
    tasks.extend([{'purpose': '官方交易日期核对', 'queries': [f'site:sse.com.cn {asof[:4]} 休市安排'], 'cutoff': cutoff.isoformat()},
                  {'purpose': '跨行业逻辑先行线索', 'queries': [f'site:stats.gov.cn {asof[:7]} 工业 行业',
                    f'site:miit.gov.cn {asof[:7]} 行业 运行'], 'cutoff': cutoff.isoformat()}])
    write_json(client.output / 'research-tasks.json', tasks)
    manifest.update(status='partial', data_ready=bool(market['status'] == 'ok' and any(r['complete_21_sessions'] for r in industry)),
                    finished_at=now().isoformat(), latest_closed=latest_closed, historical_members_allowed=members_allowed,
                    unresolved=['官方日历交叉核对', '原始产业资料与反证阅读', '公司业务与价格核验', '按十步模板完成复盘'],
                    note='此处状态为取数结果，最终研究完成状态由 Codex 写入 review.md；不自动给出买卖建议。')
    write_json(client.output / 'manifest.json', manifest)
    render_data_report(client.output, manifest, market, industry, coverage, candidates)
    return manifest


def render_data_report(output, manifest, market, industry, coverage, candidates):
    def fmt(value):
        return '缺数据' if value is None else f'{value:.2f}'
    lines = [f'# {manifest["review_date"]} 复盘资料摘要', '',
             f'信息截止：{manifest["cutoff"]}。这是已取得资料的摘要，完整复盘见完成研究后的 review.md。', '',
             f'有效交易股票：{market["traded_valid_count"]}；上涨：{market["advancing"]}；下跌：{market["declining"]}。',
             f'有效样本成交额：{fmt(market["turnover_yuan"] / 1e8 if market["turnover_yuan"] is not None else None)} 亿元；市场数据状态：{market["status"]}。', '',
             '| 分类 | 层级 | 应检查 | 当日有效 | 21日完整 |', '|---|---|---:|---:|---:|']
    for c in coverage:
        lines.append(f'| {c["classification"]} | {c["level"]} | {c["expected"]} | {c["valid_target_date"]} | {c["complete_21_sessions"]} |')
    lines += ['', '## 行情线索', '', '| 分类 | 方向 | 1日% | 5日% | 20日% | 来源 |', '|---|---|---:|---:|---:|---|']
    usable = sorted([r for r in industry if r['complete_21_sessions']], key=lambda r: r['return_5d_pct'], reverse=True)
    for row in usable[:10]:
        lines.append(f'| {row["classification"]} | {row["name"]} | {fmt(row["return_1d_pct"])} | {fmt(row["return_5d_pct"])} | {fmt(row["return_20d_pct"])} | {row["source_id"]} |')
    lines += ['', '## 待核实公司', '']
    lines += [f'- {x["name"]}（{x["symbol"]}）：{x["industry"]}；{x["selection_reason"]}。' for x in candidates] or ['暂无可形成的公司线索。']
    lines += ['', '## 继续研究', '',
              '按 research-tasks.json 查阅产业资料与反证，阅读 raw 中已取得的公告文字，完成现有十步复盘。',
              '行业强势不代表公司业务已核实；当前行业目录不能证明历史分类完整；缺持仓和账户规则时不填写调仓比例。',
              '数据、日期和缺口见 sources.json、coverage.json、member-coverage.json；未取得数据不能补零。', '']
    (Path(output) / 'data-review.md').write_text('\n'.join(lines), encoding='utf-8')
