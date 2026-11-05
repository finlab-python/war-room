"""把股價、營收、年報全文、世界層與 AI 抽出的關係寫進 ClickHouse（chDB）。每次重建，只要幾十秒。"""
import gzip, json, re

import pandas as pd
from chdb import session
from finlab import data

import world
from common import DB, EXTRACT, RAW, categories

SINCE = '2021-06-01'
TPEX = 'https://ic.tpex.org.tw/'
SCHEMA = """
CREATE TABLE wr.prices (stock_id LowCardinality(String), date Date, close Float32 CODEC(Gorilla, ZSTD)) ENGINE = MergeTree ORDER BY (stock_id, date);
CREATE TABLE wr.revenue (stock_id LowCardinality(String), date Date, yoy Float32) ENGINE = MergeTree ORDER BY (stock_id, date);
CREATE TABLE wr.docs (stock_id LowCardinality(String), page UInt16, text String CODEC(ZSTD(19))) ENGINE = MergeTree ORDER BY (stock_id, page);
CREATE TABLE wr.reports (stock_id String, file String, url String, pdf_bytes UInt64, pages UInt16) ENGINE = MergeTree ORDER BY stock_id;
CREATE TABLE wr.nodes (id String, kind LowCardinality(String), name String, sub String) ENGINE = MergeTree ORDER BY id;
CREATE TABLE wr.edges (id UInt32, src String, dst String, effect Int8, weight Float32, kind LowCardinality(String),
                       why String, source String, stock_id String, page UInt16, quote String) ENGINE = MergeTree ORDER BY id;
CREATE TABLE wr.events (id String, name String, sub String, start Date, seeds String, note String, source String) ENGINE = MergeTree ORDER BY start;
CREATE TABLE wr.extract_stats (stock_id String, kept UInt32, dropped UInt32) ENGINE = MergeTree ORDER BY stock_id;
"""


def norm(s):
    return re.sub(r'\s+', '', str(s)).upper()


def matcher(pairs):
    """別名越長越先比；英文別名要整個字相符，避免 PET 被當成 PE。"""
    pats = []
    for alias, nid in sorted(pairs, key=lambda p: -len(p[0])):
        a = norm(alias)
        pats.append((re.compile(rf'(?<![A-Z]){re.escape(a)}(?![A-Z])') if a.isascii() else re.compile(re.escape(a)), nid))
    return lambda text: next((nid for p, nid in pats if p.search(norm(text))), None)


def long_table(df, value):
    df = df[df.index >= SINCE]
    out = df.stack().reset_index()
    out.columns = ['date', 'stock_id', value]
    out['stock_id'] = out['stock_id'].astype(str)
    out['date'] = pd.to_datetime(out['date']).dt.date
    return out.dropna()


def insert(sess, table, df):
    sess.query(f'INSERT INTO wr.{table} SELECT * FROM Python(df)')


def graph(themes, basic):
    nodes = {nid: (name, kind, '') for nid, (name, kind, _) in world.NODES.items()}
    edges = []

    def edge(src, dst, effect, weight, kind, why, source='', stock_id='', page=0, quote=''):
        edges.append((len(edges), src, dst, effect, weight, kind, why, source, stock_id, page, quote))

    for src, dst, effect, weight, why, source in world.EDGES:
        edge(src, dst, effect, weight, 'world', why, source)

    names = dict(zip(basic['stock_id'], basic['公司簡稱']))
    for sid, name, cats in zip(themes['stock_id'], themes['name'], themes['category']):
        nodes['co:' + sid] = (name, 'company', sid)
        for c in categories(cats):
            if ':' in c:
                theme, seg = c.split(':', 1)
                nodes['seg:' + c] = (seg, 'segment', theme)
                edge('seg:' + c, 'co:' + sid, 1, .7, 'theme', f'{name}屬於「{theme}／{seg}」', TPEX)
    missing = {e[2] for e in edges if e[2] not in nodes} | {e[1] for e in edges if e[1] not in nodes}
    assert not missing, f'世界層接到不存在的節點：{missing}'

    commodity = matcher([(a, nid) for nid, (_, kind, aliases) in world.NODES.items() if kind in ('commodity', 'entity') for a in aliases])
    event = matcher([(a, nid) for nid, (_, kind, aliases) in world.NODES.items() if kind == 'event' for a in aliases])
    company = {}
    for sid, short, full in zip(basic['stock_id'], basic['公司簡稱'], basic['公司名稱']):
        company[norm(short)] = 'co:' + sid
        company[norm(re.sub(r'(股份)?有限公司|\(股\)公司|公司$', '', full))] = 'co:' + sid
    party = lambda n: company.get(norm(n)) or (commodity(n) if commodity(n) == 'x:cpc' else None)

    stats = []
    for f in sorted(EXTRACT.glob('*.json')):
        x = json.loads(f.read_text())
        sid, co = x['stock_id'], 'co:' + x['stock_id']
        if co not in nodes:
            continue
        name, kept = nodes[co][0], 0
        ev = lambda it, kind, src, effect, weight, why: edge(src, co, effect, weight, kind, why, '', sid, it['page'], it['quote'])
        for it in x.get('inputs', []):
            if (c := commodity(it['name'])) and c != 'x:cpc':
                ev(it, 'input', c, -1, .8, f"{it['name']}是{name}的主要原料"); kept += 1
            if event(it.get('origin') or ''):
                ev(it, 'input', 'ev:hormuz', -1, .8, f"{name}的{it['name']}來自中東"); kept += 1
        for it in x.get('products', []):
            if (c := commodity(it['name'])) and c != 'x:cpc':
                ev(it, 'product', c, 1, .8, f"{name}生產{it['name']}"); kept += 1
        for it in x.get('suppliers', []):
            if (s := party(it['name'])) and s != co:
                share = f"，占進貨 {it['share']}%" if it.get('share') else ''
                edge(s, co, 0, min(.9, .4 + (it.get('share') or 0) / 100), 'supplier', f"{it['name']}是{name}的主要供應商{share}", '', sid, it['page'], it['quote']); kept += 1
        for it in x.get('customers', []):
            if (c := party(it['name'])) and c != co:
                share = f"，占營收 {it['share']}%" if it.get('share') else ''
                edge(co, c, 0, min(.9, .4 + (it.get('share') or 0) / 100), 'customer', f"{name}的主要客戶是{it['name']}{share}", '', sid, it['page'], it['quote']); kept += 1
        for it in x.get('exposures', []):
            text = f"{it.get('factor', '')} {it['quote']}"
            src = event(text) or commodity(text)
            effect = {'+': 1, '-': -1}.get(str(it.get('effect', '')).strip()[:1], 0)
            if src and src != 'x:cpc':
                # 自述權重低於結構路徑：有原料、產品、產業鏈可循時，傳導鏈走結構那條，自述留在證據裡
                ev(it, 'exposure', src, effect, .5 if src.startswith('ev:') else .6, f"{name}自述：{it.get('factor', '')}"); kept += 1
        stats.append((sid, kept, x.get('dropped', 0)))

    used = {e[1] for e in edges} | {e[2] for e in edges}
    node_rows = pd.DataFrame([(i, k, n, s) for i, (n, k, s) in nodes.items() if i in used], columns=['id', 'kind', 'name', 'sub'])
    edge_rows = pd.DataFrame(edges, columns=['id', 'src', 'dst', 'effect', 'weight', 'kind', 'why', 'source', 'stock_id', 'page', 'quote'])
    return node_rows, edge_rows, pd.DataFrame(stats, columns=['stock_id', 'kept', 'dropped'])


def reports():
    metas, pages = [], []
    for f in sorted(RAW.glob('*.jsonl.gz')):
        with gzip.open(f, 'rt') as fh:
            meta, *rows = map(json.loads, fh)
        metas.append(meta)
        pages += [(meta['stock_id'], r['page'], r['text']) for r in rows]
    return (pd.DataFrame(metas)[['stock_id', 'file', 'url', 'pdf_bytes', 'pages']],
            pd.DataFrame(pages, columns=['stock_id', 'page', 'text']))


def main():
    themes = data.get('security_industry_themes')
    basic = data.get('company_basic_info')
    close = data.get('etl:adj_close')
    prices = long_table(close, 'close')
    taiex = data.get('benchmark_return:發行量加權股價報酬指數').squeeze()
    # 等權大盤：全體上市櫃普通股每天報酬的平均。加權指數四成是台積電，拿來當基準會把「非電子股」誤判成受惠
    stocks = close[[c for c in close.columns if re.fullmatch(r'[1-9]\d{3}', c)]]
    eqw = (1 + stocks[stocks.index >= SINCE].pct_change(fill_method=None).clip(-.11, .11).mean(axis=1).fillna(0)).cumprod()
    bench = pd.concat([pd.DataFrame({'stock_id': sid, 'date': pd.to_datetime(s.index).date, 'close': s.values})
                       for sid, s in (('TAIEX', taiex[taiex.index >= SINCE]), ('EQW', eqw))])
    revenue = long_table(data.get('monthly_revenue:去年同月增減(%)'), 'yoy')
    nodes, edges, stats = graph(themes, basic)
    meta, pages = reports()
    events = pd.DataFrame([(e['id'], e['name'], e['sub'], pd.Timestamp(e['start']).date(), ','.join(e['seeds']), e['note'], e['source'])
                           for e in world.EVENTS], columns=['id', 'name', 'sub', 'start', 'seeds', 'note', 'source'])

    sess = session.Session(str(DB))
    sess.query('DROP DATABASE IF EXISTS wr')
    sess.query('CREATE DATABASE wr')
    for stmt in filter(str.strip, SCHEMA.split(';')):
        sess.query(stmt)
    for table, df in [('prices', pd.concat([prices, bench])[['stock_id', 'date', 'close']]), ('revenue', revenue[['stock_id', 'date', 'yoy']]),
                      ('docs', pages), ('reports', meta), ('nodes', nodes), ('edges', edges), ('events', events), ('extract_stats', stats)]:
        insert(sess, table, df)
    sess.query('OPTIMIZE TABLE wr.docs FINAL')
    print(sess.query("""SELECT table, sum(rows) AS rows, formatReadableSize(sum(data_uncompressed_bytes)) AS raw,
                               formatReadableSize(sum(data_compressed_bytes)) AS stored
                        FROM system.parts WHERE database = 'wr' AND active GROUP BY table ORDER BY table""", 'PrettyCompact'))
    print(sess.query('SELECT kind, count() FROM wr.edges GROUP BY kind ORDER BY kind', 'PrettyCompact'))
    sess.close()


if __name__ == '__main__':
    main()
