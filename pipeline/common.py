import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
RAW = DATA / 'raw'          # 年報每頁文字，一家一個 jsonl.gz
EXTRACT = DATA / 'extract'  # AI 從年報抽出的關係，一家一個 json
DB = DATA / 'ch'            # ClickHouse（chDB）資料目錄
for d in (RAW, EXTRACT):
    d.mkdir(parents=True, exist_ok=True)

# 這一版先收錄跟能源、原物料、運輸有關的產業鏈（依序下載），其餘之後補
SCOPE = [
    '石化及塑橡膠', '油電燃氣', '交通運輸及航運', '汽電共生',
    '紡織:石化原料', '紡織:人造纖維產品', '紡織:化學助劑',
    '半導體:晶圓製造', '半導體:IC_晶圓製造', '半導體:DRAM製造', '半導體:化學品',
    '印刷電路板:環氧樹脂', '印刷電路板:酚醛樹脂', '印刷電路板:聚亞醯胺樹脂', '印刷電路板:銅箔基板', '印刷電路板:玻璃纖維_玻纖布',
    '鋼鐵:煤、鐵礦砂、鎳鐵、鉻鐵、廢鋼', '鋼鐵:鋼胚', '鋼鐵:冷熱軋鋼板捲', '鋼鐵:不鏽鋼胚', '鋼鐵:冷熱軋不鏽鋼板捲',
    '食品:原物料', '造紙', '水泥', '休閒娛樂:旅遊服務業', '航太週邊', '平面顯示器:化學品(如光阻劑、靶材等)',
]


def categories(v):
    return ast.literal_eval(v) if isinstance(v, str) else list(v)


def scope_companies(themes):
    """依 SCOPE 順序列出要收錄年報的公司代號。"""
    out = []
    for prefix in SCOPE:
        for sid, cats in zip(themes['stock_id'], themes['category']):
            if sid not in out and any(c == prefix or c.startswith(prefix + ':') for c in categories(cats)):
                out.append(sid)
    return out
