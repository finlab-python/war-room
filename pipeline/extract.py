"""用 AI 把一份兩百頁的年報壓成幾十條供應鏈關係，每條附原文頁碼與引文；引文對不上原文的直接丟掉。

結果存 data/extract/<代號>.json；已抽過的跳過，所以每天重跑只處理新年報。
"""
import gzip, json, re, sys
from concurrent.futures import ThreadPoolExecutor

from war_room.ai import ask_json
from common import EXTRACT, RAW

SYSTEM = """你在整理台灣上市櫃公司的供應鏈關係。使用者會給你一家公司的年報節錄，每頁開頭標 [第N頁]。只根據節錄內容，輸出一個 JSON 物件，不要任何其他文字：
{"products": [{"name": "主要產品或服務", "page": N, "quote": "原文"}],
 "inputs": [{"name": "主要原料", "origin": "來源地或供應商，沒寫就空字串", "page": N, "quote": "原文"}],
 "suppliers": [{"name": "供應商名稱", "share": 占進貨百分比或 null, "page": N, "quote": "原文"}],
 "customers": [{"name": "客戶名稱", "share": 占銷貨百分比或 null, "page": N, "quote": "原文"}],
 "exposures": [{"factor": "影響因素，例如中東戰爭、油價、運價、天然氣價格、匯率", "effect": "+ 代表對公司有利，- 代表不利", "page": N, "quote": "原文"}]}
規則：
- quote 必須從節錄逐字複製一段連續文字（15 到 60 字），不可改寫、不可拼接、不可加刪標點。
- products、inputs 各最多 8 個，用原文寫法；代號化的客戶或供應商（甲公司、A 客戶、客戶一）不要列。
- 沒有的類別給空陣列，不要猜。"""

KEYS = re.compile(r'主要原料|供應狀況|主要產品|產製過程|進貨|銷貨|供應商|客戶|用途')
RISK = re.compile(r'中東|伊朗|以色列|荷姆茲|荷莫茲|戰爭|油價|原油|運價|運費|天然氣')
TOC = re.compile(r'[-－.．…]{6,}')


def norm(s):
    return re.sub(r'\s+', '', s)


def read(sid):
    with gzip.open(RAW / f'{sid}.jsonl.gz', 'rt') as f:
        meta, *pages = map(json.loads, f)
    return meta, pages


def pick(pages):
    """營運概況的原料、產品、進銷貨頁，加上幾頁談中東、油價、運價的；目錄頁不要。"""
    body = [p for p in pages if len(TOC.findall(p['text'])) < 5]
    main = [p for p in sorted(body, key=lambda p: -len(KEYS.findall(p['text'])))[:8] if KEYS.search(p['text'])]
    risk = [p for p in sorted(body, key=lambda p: -len(RISK.findall(p['text']))) if RISK.search(p['text']) and p not in main][:4]
    return sorted(main + risk, key=lambda p: p['page'])


def verify(result, pages):
    """只留引文真的出現在那一頁的項目。"""
    text = {p['page']: norm(p['text']) for p in pages}
    kept, dropped = {}, 0
    for kind, items in result.items():
        if not isinstance(items, list):
            continue
        kept[kind] = [x for x in items if isinstance(x, dict) and len(norm(x.get('quote') or '')) >= 6
                      and norm(x['quote']) in text.get(x.get('page'), '')]
        dropped += len(items) - len(kept[kind])
    return kept, dropped


def extract(sid):
    out = EXTRACT / f'{sid}.json'
    if out.exists():
        return
    meta, pages = read(sid)
    chosen = pick(pages)
    text = '\n\n'.join(f"[第{p['page']}頁]\n{p['text']}" for p in chosen)[:40000]
    try:
        result = ask_json(SYSTEM, text)
    except Exception as e:
        print(sid, '失敗', repr(e)[:150], flush=True)
        return
    kept, dropped = verify(result, pages)
    out.write_text(json.dumps({'stock_id': sid, 'pages_read': [p['page'] for p in chosen], 'dropped': dropped, **kept},
                              ensure_ascii=False, indent=1))
    print(sid, {k: len(v) for k, v in kept.items()}, '丟掉', dropped, flush=True)


if __name__ == '__main__':
    sids = sys.argv[1:] or sorted(p.name.split('.')[0] for p in RAW.glob('*.jsonl.gz'))
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(extract, sids))
