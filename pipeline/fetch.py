"""下載公開資訊觀測站的股東會年報，每頁文字存成 data/raw/<代號>.jsonl.gz。PDF 只在記憶體裡，不落地。

已經抓過的公司會跳過，所以每天重跑就是增量更新。
"""
import gzip, json, re, ssl, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

import pymupdf
from finlab import data

from common import RAW, scope_companies

BASE = 'https://doc.twse.com.tw/server-java/t57sb01'
# 證交所憑證缺 Subject Key Id，Python 3.13 的嚴格檢查會擋；其餘驗證照常
SSL = ssl.create_default_context()
SSL.verify_flags &= ~ssl.VERIFY_X509_STRICT


def get(url, timeout=120):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL) as r:
        return r.read()


def page(url):
    """證交所限流時回「查詢過量」，等一下再查。"""
    for wait in (10, 30, 60, 120, 300):
        html = get(url).decode('big5', 'ignore')
        if '查詢過量' not in html:
            return html
        time.sleep(wait)
    raise RuntimeError('查詢過量')


def report_name(sid):
    for year in (115, 114):  # 民國 115 年股東會＝2025 年報；還沒出就用前一年
        html = page(f'{BASE}?step=1&colorchg=1&co_id={sid}&year={year}&mtype=F&')
        names = re.findall(rf'\d{{4}}_{sid}_\d+F04\.pdf', html)
        if names:
            return names[0]


def fetch(sid):
    out = RAW / f'{sid}.jsonl.gz'
    if out.exists():
        return
    try:
        name = report_name(sid)
        if not name:
            print(sid, '沒有年報', flush=True)
            return
        page_url = f'{BASE}?step=9&kind=F&co_id={sid}&filename={name}'
        link = re.search(r"href='(/pdf/[^']+\.pdf)'", page(page_url)).group(1)
        pdf = get('https://doc.twse.com.tw' + link, timeout=900)
        pages = [p.get_text() for p in pymupdf.open(stream=pdf, filetype='pdf')]
    except Exception as e:
        print(sid, '失敗', repr(e)[:120], flush=True)
        return
    tmp = out.with_suffix('.tmp')
    with gzip.open(tmp, 'wt') as f:
        meta = {'stock_id': sid, 'file': name, 'url': page_url, 'pdf_bytes': len(pdf), 'pages': len(pages)}
        f.write(json.dumps(meta, ensure_ascii=False) + '\n')
        for i, text in enumerate(pages, 1):
            f.write(json.dumps({'page': i, 'text': text}, ensure_ascii=False) + '\n')
    tmp.rename(out)
    print(sid, name, f'{len(pdf) >> 10} KB', len(pages), '頁', flush=True)


if __name__ == '__main__':
    sids = sys.argv[1:] or scope_companies(data.get('security_industry_themes'))
    print(len(sids), '家', flush=True)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(fetch, sids))
