"""戰情室網站：靜態頁面＋三個查詢（圖、股價反應、原文證據）＋桌機版的 AI 推論。

有本機資料庫（data/ch）就自己查；沒有（桌機版）就向公開站要資料，AI 用這台電腦上使用者自己的 Claude Code 或 Codex。
"""
import argparse, gzip, json, mimetypes, os, re, threading, time, webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from war_room import ai

WEB = Path(__file__).parent / 'web'
DB = Path(os.environ.get('WAR_ROOM_DB', Path(__file__).resolve().parent.parent / 'data' / 'ch'))
PUBLIC = 'https://war-room-748308483506.asia-east1.run.app'


class Store:
    """ClickHouse（chDB）只有一個連線，查詢排隊。"""

    def __init__(self, path):
        from chdb import session
        self.sess, self.lock = session.Session(str(path)), threading.Lock()
        self.graph = gzip.compress(json.dumps(self.load_graph(), ensure_ascii=False, separators=(',', ':')).encode())

    def rows(self, sql, **params):
        # 查詢參數的陣列要寫成 ClickHouse 字面值
        params = {k: '[' + ','.join(f"'{x}'" for x in v) + ']' if isinstance(v, list) else v for k, v in params.items()}
        with self.lock:
            out = self.sess.query(sql, 'JSONCompact', params=params).bytes()
        return json.loads(out)['data'] if out else []

    def load_graph(self):
        nodes = self.rows('SELECT id, kind, name, sub FROM wr.nodes')
        edges = self.rows('SELECT src, dst, effect, weight, kind, id FROM wr.edges')
        events = self.rows('SELECT id, name, sub, toString(start), seeds, note, source FROM wr.events ORDER BY start DESC')
        size = self.rows("""SELECT sum(data_compressed_bytes), sum(data_uncompressed_bytes) FROM system.parts
                            WHERE database = 'wr' AND table = 'docs' AND active""")[0]
        reports = self.rows('SELECT count(), sum(pdf_bytes), sum(pages) FROM wr.reports')[0]
        kept = self.rows('SELECT sum(kept), sum(dropped) FROM wr.extract_stats')[0]
        latest = self.rows("SELECT toString(max(date)) FROM wr.prices WHERE stock_id = 'TAIEX'")[0][0]
        return {
            'nodes': nodes, 'edges': edges,
            'events': [dict(zip(['id', 'name', 'sub', 'start', 'seeds', 'note', 'source'], e)) | {'seeds': e[4].split(',')} for e in events],
            'stats': {'reports': reports[0], 'pdf_bytes': reports[1], 'pages': reports[2], 'docs_stored': size[0], 'docs_raw': size[1],
                      'relations': kept[0], 'dropped': kept[1], 'edges': len(edges), 'latest': latest},
        }

    WINDOWS = (5, 20, 60)  # 一週、一個月、三個月（交易日）

    def reaction(self, start, ids):
        """以事件前最後一個收盤為基準，算每檔相對等權大盤的累積超額報酬，以及事件後的月營收年增率。"""
        ids = [i for i in ids if re.fullmatch(r'[0-9A-Z]{4,6}', i)][:800]
        base = self.rows("SELECT toString(max(date)) FROM wr.prices WHERE stock_id = 'EQW' AND date < {s:Date}", s=start)[0][0]
        rows = self.rows("""
            WITH b AS (SELECT stock_id, argMax(close, date) AS c0 FROM wr.prices
                       WHERE stock_id IN {ids:Array(String)} AND date <= {b:Date} AND date > {b:Date} - 30 GROUP BY stock_id)
            SELECT p.stock_id, groupArray(toString(p.date)), groupArray(round(p.close / b.c0 - 1, 4))
            FROM (SELECT * FROM wr.prices WHERE stock_id IN {ids:Array(String)} AND date > {b:Date} AND date <= {b:Date} + 400 ORDER BY date) AS p
            JOIN b USING stock_id GROUP BY p.stock_id""", ids=ids + ['TAIEX', 'EQW'], b=base)
        series = {sid: (dates, rets) for sid, dates, rets in rows}
        if 'EQW' not in series:
            return {'base': base, 'stocks': {}}
        mdates, mrets = series.pop('EQW')
        tdates, trets = series.pop('TAIEX', ([], []))
        market = dict(zip(mdates, mrets))
        at = lambda xs, n: xs[n - 1] if len(xs) >= n else None
        revenue = {sid: list(zip(d, y)) for sid, d, y in self.rows("""
            SELECT stock_id, groupArray(toString(date - toIntervalMonth(1))), groupArray(round(yoy, 1))
            FROM (SELECT * FROM wr.revenue WHERE stock_id IN {ids:Array(String)} ORDER BY date)
            WHERE toStartOfMonth(date - toIntervalMonth(1)) >= toStartOfMonth({s:Date} + 7)  -- 事件後第一個完整受影響的月份起
            GROUP BY stock_id""", ids=ids, s=start)}
        stocks = {}
        for sid, (dates, rets) in series.items():
            excess = [round((1 + r) / (1 + market[d]) - 1, 4) for d, r in zip(dates, rets) if d in market]
            if excess:
                stocks[sid] = {'at': {n: at(excess, n) for n in self.WINDOWS}, 'now': excess[-1],
                               'spark': excess[::max(1, len(excess) // 40)], 'revenue': [[d[:7], y] for d, y in revenue.get(sid, [])][:6]}
        return {'base': base, 'end': mdates[-1], 'stocks': stocks,
                'windows': {n: {'end': at(mdates, n), 'market': at(mrets, n), 'taiex': at(trets, n)} for n in self.WINDOWS}}

    def evidence(self, eid):
        row = self.rows("""SELECT e.src, e.dst, e.effect, e.kind, e.why, e.source, e.stock_id, e.page, e.quote, r.url, d.text
                           FROM wr.edges AS e LEFT JOIN wr.reports AS r ON r.stock_id = e.stock_id
                           LEFT JOIN wr.docs AS d ON d.stock_id = e.stock_id AND d.page = e.page WHERE e.id = {id:UInt32}""", id=eid)
        if not row:
            return {}
        src, dst, effect, kind, why, source, sid, page, quote, url, text = row[0]
        out = {'src': src, 'dst': dst, 'effect': effect, 'kind': kind, 'why': why, 'source': source or url, 'page': page}
        if quote and text:
            m = re.search(r'\s*'.join(map(re.escape, re.sub(r'\s+', '', quote))), text)
            a, b = (m.start(), m.end()) if m else (0, 0)
            # PDF 私用區的符號（項目符號）拿掉，換行併成空白
            clean = lambda s: re.sub(r'\s+', ' ', re.sub(r'[\ue000-\uf8ff\ufffd]', '', s)).strip()
            out['context'] = [clean(text[max(0, a - 70):a]), clean(text[a:b]) or quote, clean(text[b:b + 70])]
        return out


def catalog(graph):
    keep = [n for n in graph['nodes'] if n[1] in ('event', 'commodity', 'entity', 'segment')]
    return '\n'.join(f'{i}｜{name}｜{sub}' for i, kind, name, sub in keep)


AI_SYSTEM = """你是供應鏈分析師。使用者描述一個事件，請從節點清單中挑出事件最先衝擊的 1 到 6 個起點（事件、原物料或產業），只用清單裡的 id。
如果事件點名了台灣上市櫃公司，把公司簡稱放進 companies。只輸出 JSON：
{"title": "事件的短名稱（12 字內）", "start": "事件開始日 YYYY-MM-DD，未知或尚未發生給空字串",
 "seeds": [{"id": "節點 id", "why": "為什麼是起點（20 字內）"}], "companies": ["公司簡稱"]}"""


def handler(store, cfg):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype='application/json', status=200, zipped=False, cache='no-store'):
            if not zipped and len(body) > 1024 and 'gzip' in self.headers.get('Accept-Encoding', ''):
                body, zipped = gzip.compress(body), True
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', cache)
            self.send_header('Access-Control-Allow-Origin', '*')
            if zipped:
                self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, status=200):
            self.send(json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode(), status=status)

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            t = time.perf_counter()
            if url.path == '/api/config':
                return self.json(cfg)
            if store and url.path == '/api/graph':
                return self.send(store.graph, zipped=True, cache='public, max-age=300')
            if store and url.path == '/api/reaction':
                out = store.reaction(q['start'], q.get('ids', '').split(','))
                return self.json(out | {'ms': round((time.perf_counter() - t) * 1000, 1)})
            if store and url.path == '/api/evidence':
                return self.json(store.evidence(int(q['id'])))
            path = WEB / (url.path.lstrip('/') or 'index.html')
            if url.path.startswith('/api/') or not path.resolve().is_relative_to(WEB.resolve()) or not path.is_file():
                return self.send(b'{}', status=404)
            cache = 'public, max-age=86400' if '/vendor/' in url.path or '/fonts/' in url.path else 'no-cache'
            self.send(path.read_bytes(), mimetypes.guess_type(path.name)[0] or 'application/octet-stream', cache=cache)

        def do_POST(self):
            if self.path != '/api/ai' or not cfg['ai']:
                return self.send(b'{}', status=404)
            text = json.loads(self.rfile.read(int(self.headers['Content-Length'])))['text'][:2000]
            try:
                graph = store.load_graph() if store else ai_graph(cfg['api'])
                reply = ai.ask_json(f'{AI_SYSTEM}\n\n節點清單（id｜名稱｜所屬）：\n{catalog(graph)}', f'事件：{text}\n今天是 {date.today()}。', timeout=120)
                ids = {n[0] for n in graph['nodes']}
                names = {n[2]: n[0] for n in graph['nodes'] if n[1] == 'company'}
                seeds = [s for s in reply.get('seeds', []) if s.get('id') in ids]
                seeds += [{'id': names[c], 'why': '事件點名的公司'} for c in reply.get('companies', []) if c in names]
                self.json({'title': reply.get('title') or text[:12], 'start': reply.get('start') or '', 'seeds': seeds})
            except Exception as e:
                self.json({'error': f'AI 沒有回應：{e}'[:200]}, status=502)

    return H


def ai_graph(api):
    from urllib.request import Request, urlopen
    with urlopen(Request(api + '/api/graph', headers={'Accept-Encoding': 'gzip'}), timeout=30) as r:
        body = r.read()
    return json.loads(gzip.decompress(body) if r.headers.get('Content-Encoding') == 'gzip' else body)


def main():
    p = argparse.ArgumentParser(description='供應鏈戰情室')
    p.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8765)))
    p.add_argument('--host', default='0.0.0.0' if 'PORT' in os.environ else '127.0.0.1')
    args = p.parse_args()
    store = Store(DB) if DB.exists() else None
    local = args.host in ('127.0.0.1', 'localhost')
    # 只有在使用者自己的電腦上才接本機 AI；公開站不跑 AI
    cfg = {'api': '' if store else PUBLIC, 'ai': ai.engine() if local else None}
    server = ThreadingHTTPServer((args.host, args.port), handler(store, cfg))
    url = f'http://{args.host}:{args.port}'
    print(f'戰情室：{url}（資料：{"本機 ClickHouse" if store else PUBLIC}；AI：{cfg["ai"] or "無"}）', flush=True)
    if local and 'PORT' not in os.environ and not os.environ.get('WAR_ROOM_NO_BROWSER'):
        webbrowser.open(url)
    server.serve_forever()


if __name__ == '__main__':
    main()
