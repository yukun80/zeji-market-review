"""Parse reported estimates, not audited subscriptions/redemptions."""
import re
from datetime import date
from urllib.parse import urlparse
from .core import publication_allowed, write_json


def parse_article(text, url, target_date, cutoff, images=None):
    text=re.sub(r'\s+', '', text)
    stamp=re.search(r'(20\d{2}-\d{2}-\d{2})[T ]?(\d{2}:\d{2}:\d{2})', text)
    published=stamp[1]+'T'+stamp[2]+'+08:00' if stamp else None
    heading=re.search(r'ETF观察[〗】]?(\d{1,2})月(\d{1,2})日(行业主题|风格策略|股票指数)ETF',text)
    actual=None
    if heading and published:
        year=int(published[:4]); month=int(heading[1]); d=int(heading[2])
        # January publication can describe the preceding December session.
        if month>int(published[5:7]): year-=1
        actual=date(year,month,d).isoformat()
    def money(match):
        if not match:return None
        return (1 if match[1]=='入' else -1)*float(match[2])*(1e8 if match[3]=='亿' else 1e4)
    daily=re.search(r'合计资金净流(入|出)([\d.]+)(亿|万)元',text)
    five=re.search(r'近5个交易日累计净流(入|出)([\d.]+)(亿|万)元',text)
    counts={}
    for direction,label in [('入','inflow_count'),('出','outflow_count')]:
        m=re.search(r'当日有(\d+)只[^。]*?出现资金净流'+direction,text)
        counts[label]=int(m[1]) if m else None
    funds=[]
    pattern=r'首位的是([^（(]+)[（(](\d{6})[）)]，份额(增加|减少)了([\d.]+)(亿|万)份，净流(入|出)额为([\d.]+)(亿|万)元'
    for m in re.finditer(pattern,text):
        funds.append(dict(name=m[1], code=m[2], shares_change=(1 if m[3]=='增加' else -1)*float(m[4])*(1e8 if m[5]=='亿' else 1e4),
            estimated_flow_yuan=(1 if m[6]=='入' else -1)*float(m[7])*(1e8 if m[8]=='亿' else 1e4)))
    errors=[]
    if actual!=target_date:errors.append('文章交易日与目标日不符或无法识别')
    if not publication_allowed(published,cutoff):errors.append('发布时间未知或超过截止时间')
    if not daily or not five or any(v is None for v in counts.values()):errors.append('正文核心字段不全')
    return dict(source=url, original_publisher='证券之星', trade_date=actual, published_at=published,
        scope=heading[3]+'ETF' if heading else None, estimated_daily_flow_yuan=money(daily),
        estimated_5d_flow_yuan=money(five), **counts, body_fund_details=funds,
        formula='（当日场内流通份额－前一交易日场内流通份额）×当日ETF均价；来源估算',
        usable_before_cutoff=not errors, errors=errors, images=images or [],
        image_details_verified=False, detail_coverage='仅正文所列基金，图表明细待实际阅读；不是全量明细',
        independent_source_group='stockstar_etf_observation')


def collect(client,url,target_date,cutoff):
    host=urlparse(url).hostname or ''
    if not (host=='stockstar.com' or host.endswith('.stockstar.com')):
        raise ValueError('此入口接收证券之星原始文章；转载请作为普通原文另行核对')
    record=client.fetch('document',{'url':url},ttl_hours=0)
    rows=record.get('rows',[])
    result=parse_article('\n'.join(r.get('text','') for r in rows),url,target_date,cutoff,
                         [x for r in rows for x in r.get('images',[])])
    result['source_id']=record['source_id']
    write_json(client.output/'etf-observation.json',result)
    return result
