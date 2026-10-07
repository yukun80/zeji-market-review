"""Human handoff, preserved evidence and incremental price recalculation."""
import hashlib
import csv
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from .core import day, number, price_metrics, publication_allowed, write_json, now


def account_check():
    return [
        {'source': 'Tushare', 'requires': '账号与访问密钥', 'configured': bool(os.getenv('TUSHARE_TOKEN')),
         'permission': '未验证；配置存在不代表权限可用', 'setting': 'TUSHARE_TOKEN',
         'url': 'https://tushare.pro/register', 'impact': '可选股票日线备用；缺少时继续免费来源'},
        {'source': 'SEC', 'requires': '真实应用名称和联系邮箱；无须密钥', 'configured': bool(os.getenv('SEC_USER_AGENT')),
         'permission': '未验证', 'setting': 'SEC_USER_AGENT',
         'url': 'https://www.sec.gov/search-filings/edgar-application-programming-interfaces',
         'impact': '自动披露查询需要联系身份；可继续读公开官网'},
        {'source': '证券之星、交易所、巨潮、AKShare公开来源、BaoStock', 'requires': '无个人账号配置',
         'configured': True, 'permission': '以实际访问为准，登录或访问限制会另报',
         'url': 'https://www.stockstar.com/', 'impact': '公开入口，不承诺永久免费或可用'}]


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def handoff(output, unavailable=False):
    output = Path(output)
    manifest = read(output/'manifest.json', {})
    asof = manifest.get('review_date', '待核对')
    requests = []
    def add(key, required, data, fields, url, impact):
        requests.append(dict(id=key, required=required, status='pending', data=data, date=asof,
            fields=fields, url=url, impact=impact,
            accepted='CSV/Excel、完整网页链接、PDF、清晰截图或填写数据；附来源、所属日期、单位与发布时间'))
    if not read(output/'sessions.json', []):
        add('calendar', True, '目标日期与交易日历', '目标日期前至少21个交易日及官方休市公告',
            'https://www.sse.com.cn/disclosure/dealinstruc/closed/', '复盘日期与全部收益窗口')
    market = read(output/'merged-market.json', read(output/'normalized/market.json', {}))
    if market.get('status') != 'ok':
        add('market', True, '目标交易日收盘市场概况与覆盖范围',
            '股票代码、收盘价、涨跌幅、成交额及单位、报价时刻、总样本数；或官方市场统计',
            'https://www.sse.com.cn/market/stockdata/overview/day/', '市场强弱、上涨覆盖和成交判断')
    coverage = read(output/'merged-coverage.json', read(output/'coverage.json', []))
    for c in coverage:
        if c.get('classification') != '申万':
            continue
        if c.get('expected') is None or c['complete_21_sessions'] < c['expected']:
            add('industry_'+c['level'], True, c['level']+'缺失行业历史，名单见coverage.json',
                '行业代码、名称、分类、截至目标日的21个连续交易日日期与收盘价；成交额可选',
                'https://www.swsresearch.com/institute_sw/allIndex/releasedIndex', '近1/5/20日比较与行业覆盖')
    if not coverage:
        add('industry', True, '行业目录与历史', '同一分类目录、行业代码、21交易日收盘价',
            'https://www.swsresearch.com/', '行业扫描')
    if not read(output/'etf-observation.json', {}).get('usable_before_cutoff'):
        add('etf_observation', False, '证券之星ETF观察目标日文章及图表',
            '当日/近5日估算资金方向、流入流出数量、基金代码、份额变化、金额、发布日期',
            'https://www.stockstar.com/', 'ETF关注方向补证；不能替代核心股票行情')
    sources = read(output/'sources.json', [])
    restrictions = [dict(source=r.get('provider'), status=r.get('status'), source_id=r.get('source_id'))
                    for r in sources if r.get('status') in ('blocked','auth_required','needs_identity')]
    state = 'partial' if unavailable else ('waiting_for_materials' if any(r['required'] for r in requests) else 'research_pending')
    manifest.update(status=state, human_collaboration=True)
    write_json(output/'manifest.json',manifest)
    value = dict(status=state, requests=requests, restrictions=restrictions, accounts=account_check())
    write_json(output/'supplement-requests.json', value)
    lines=['# 需要人工补充的资料','',f'状态：{"部分可用，用户已表示无法补充" if unavailable else "等待人工补充" if state=="waiting_for_materials" else "继续研究"}。复盘日：{asof}。',
           '', '已取得的资料会保留，补充后只重算受影响部分。可先提供必需资料；不要发送密码或密钥。','']
    for r in requests:
        lines += [f'## {r["id"]}：{"必需" if r["required"] else "可暂缺"}', '', r['data'],
                  f'- 范围：{r["date"]}；信息截止：{manifest.get("cutoff","待核对")}',
                  f'- 内容：{r["fields"]}',f'- 查询入口：{r["url"]}',f'- 可接受：{r["accepted"]}',f'- 影响：{r["impact"]}','']
    if restrictions:
        lines += ['## 访问受限', '', *[f'- {r["source"]}：{r["status"]}，请查看账号清单或提供公开导出材料。' for r in restrictions]]
    (output/'supplement-requests.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return value


def ingest(output, envelope):
    """Envelope is written by the agent after reading user material; originals remain intact.

    Metadata never substitutes for inspecting the supplied material. Data not supported
    by a validated automated calculation stays pending agent review.
    """
    output, envelope = Path(output), Path(envelope).resolve()
    payload = json.loads(envelope.read_text(encoding='utf-8'))
    digest = hashlib.sha256(envelope.read_bytes()).hexdigest()[:16]
    dest = output/'manual'/digest
    dest.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(envelope, dest/'submission.json') if envelope != (dest/'submission.json').resolve() else None
    original = payload.get('original_file')
    if original:
        source = (envelope.parent/original).resolve()
        shutil.copyfile(source, dest/('original'+source.suffix))
    manifest = read(output/'manifest.json', {})
    cutoff = datetime.fromisoformat(manifest['cutoff'])
    reasons=[]
    if not payload.get('source') or not payload.get('read_location'):
        reasons.append('缺少来源或原文阅读位置')
    if not publication_allowed(payload.get('published_at'), cutoff):
        reasons.append('发布时间未知或晚于截止时间')
    if payload.get('uncertain_fields') or payload.get('material_complete') is not True:
        reasons.append('材料不完整或识别内容待确认')
    kind=payload.get('kind')
    metrics=None
    if kind=='price_history':
        rows=payload.get('rows',[])
        if payload.get('price_unit') not in ('CNY','index_points') or payload.get('adjustment')!='unadjusted':
            reasons.append('价格单位或复权口径不匹配')
        if any(r.get('amount') is not None for r in rows) and payload.get('amount_unit')!='CNY':
            reasons.append('成交金额必须明确为元')
        if any(not day(r.get('date')) or day(r.get('date'))>manifest['review_date'] or number(r.get('close')) is None or number(r['close'])<=0 for r in rows):
            reasons.append('日期或价格无效')
        sessions=read(output/'sessions.json', [])
        if not sessions:
            reasons.append('缺少已经核对的交易日历')
        metrics=price_metrics(rows,manifest['review_date'],sessions)
        if not metrics['complete_21_sessions']:
            reasons.append(metrics['reason'] or '历史不足')
        if not payload.get('object'):
            reasons.append('缺少股票或指数标识')
    elif kind=='market_snapshot':
        from .pipeline import market_summary
        rows=payload.get('rows',[])
        if payload.get('price_unit')!='CNY' or payload.get('amount_unit')!='CNY':
            reasons.append('行情价格和成交额须为人民币元')
        if payload.get('expected_count') != len(rows) or len({r.get('code') for r in rows})!=len(rows):
            reasons.append('全量计数不符或代码重复')
        if not rows or any(not r.get('code') or not r.get('quote_time') for r in rows):
            reasons.append('缺少股票代码或报价时刻')
        try:
            metrics=market_summary(dict(source_id='manual:'+digest,rows=rows,complete=True,
                expected_count=payload.get('expected_count')),manifest['review_date'],cutoff)
            if metrics['status']!='ok':reasons.append('行情日期或收盘时点不合格')
        except (ValueError,TypeError):
            reasons.append('报价时间格式无效')
        payload['object']='market'
    else:
        reasons.append('非价格材料等待Agent逐项核验；不自动变更市场覆盖')
    result=dict(id=digest, kind=kind, object=payload.get('object'), status='pending' if reasons else 'validated',
                reasons=reasons, metrics=metrics, source=payload.get('source'), submitted_at=now().isoformat(),
                payload=payload, original_preserved=bool(original), origin='human_supplied')
    write_json(dest/'validation.json',result)
    return result


def resume(output):
    """No network rerun: calculate accepted data, retain conflicts for human/agent review."""
    output=Path(output)
    records=[read(p) for p in sorted((output/'manual').glob('*/validation.json'))]
    accepted={}; conflicts=set()
    for r in records:
        if r['status']!='validated':
            continue
        key=r['object']
        if key in accepted and accepted[key]['payload']['rows']!=r['payload']['rows']:
            conflicts.add(key)
        accepted[key]=r
    for key in conflicts:
        accepted.pop(key,None)
    write_json(output/'manual-calculations.json',dict(accepted=list(accepted.values()), conflicts=sorted(conflicts)))
    # Agent still verifies membership/identity and the narrative; a price file cannot
    # silently change the classification, denominator, or investment stage.
    changes = [{'object': k, 'change': '新增人工核验价量，待业务及分类核实', 'source': r['source'],
                'stage': '待核实', 'material_id': r['id']} for k,r in accepted.items()]
    write_json(output/'candidate-supplements.json', changes)
    if 'market' in accepted and accepted['market']['kind']=='market_snapshot':
        write_json(output/'merged-market.json',accepted['market']['metrics'])
    elif (output/'merged-market.json').exists():
        # Conflicting replacement removes its eligibility without deleting evidence.
        write_json(output/'merged-market.json',dict(status='partial',reason='manual_conflict'))
    scan_path=output/'industry-scan.csv'
    if scan_path.exists():
        with scan_path.open(encoding='utf-8-sig',newline='') as stream:
            scan_rows=list(csv.DictReader(stream))
        for row in scan_rows:
            r=accepted.get(row['code'])
            if r and r['kind']=='price_history' and r['payload'].get('classification')==row['classification'] and r['payload'].get('level')==row['level']:
                row.update(r['metrics'],source_id='manual:'+r['id'])
                # Relative performance is unknown until a comparable benchmark is checked.
                for n in (1,5,20):row[f'relative_{n}d_pp']=None
        from .pipeline import save_csv
        save_csv(output/'merged-industry-scan.csv',scan_rows)
        coverage=read(output/'coverage.json',[])
        for c in coverage:
            relevant=[r for r in scan_rows if r['classification']==c['classification'] and r['level']==c['level']]
            c['complete_21_sessions']=sum(str(r['complete_21_sessions']).lower()=='true' for r in relevant)
            c['valid_target_date']=sum(number(r.get('close')) is not None for r in relevant)
            c['missing_codes']=[r['code'] for r in relevant if str(r['complete_21_sessions']).lower()!='true']
        write_json(output/'merged-coverage.json',coverage)
    lines=['# 人工补充后继续复盘','', '仅使用检查通过的材料；原有自动取数文件保留。','']
    for key,r in accepted.items():
        m=r['metrics']
        if r['kind']=='price_history':
            lines.append(f'- {key}：1日 {m["return_1d_pct"]:.4f}%，5日 {m["return_5d_pct"]:.4f}%，20日 {m["return_20d_pct"]:.4f}%；出处 {r["source"]}。')
        else:
            lines.append(f'- 市场概况：已核对{m["traded_valid_count"]}条有效交易记录。')
    lines += [f'- 冲突待核对：{", ".join(sorted(conflicts)) or "无"}',
              '- 下一步：Agent对照原行业目录归并有效行、更新覆盖和候选，再更新review.md及candidate-changes.md。不能因单只股票补齐就宣称全市场通过。']
    (output/'resume-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    state=handoff(output)['status']
    return dict(validated_objects=list(accepted),conflicts=sorted(conflicts), pending=sum(r['status']=='pending' for r in records), status=state)
