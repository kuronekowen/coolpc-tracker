import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------- 常數 ----------
GMT8 = timezone(timedelta(hours=8))
URL = 'https://www.coolpc.com.tw/evaluate.php'
JSON_PATH = Path('docs/data/coolpc_prices.json')

HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
    'DNT': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36',
    'sec-ch-ua': '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
}

# 產品文字格式：｛產品名稱｝ (規格), $價格 ...
PRODUCT_RE = re.compile(
    r'^[｛{](?P<name>.+?)[｝}]\s*(?P<spec>.*?),\s*\$(?P<price>[\d,]+)'
)


# ---------- 抓取 ----------
def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw = resp.content
    for enc in ('utf-8', 'big5', 'big5hkscs', 'cp950'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('big5', errors='replace')


# ---------- 解析 ----------
def parse_products(html: str):
    soup = BeautifulSoup(html, 'lxml')
    results = []

    for td in soup.find_all('td', class_='t'):
        category = td.get_text(strip=True)
        if not category:
            continue

        # 找緊接著的 <select>
        select = None
        for sib in td.next_siblings:
            if getattr(sib, 'name', None) is None:
                continue
            if sib.name == 'td':
                inner = sib.find('select')
                if inner:
                    select = inner
                break
        if select is None:
            select = td.find_next('select')
        if select is None:
            continue

        products = []
        for opt in select.find_all('option'):
            text = opt.get_text(strip=True)
            if not text or text[0] not in ('｛', '{'):
                continue
            m = PRODUCT_RE.match(text)
            if not m:
                continue
            products.append({
                'value': opt.get('value', ''),
                'name': m.group('name').strip(),
                'spec': m.group('spec').strip(),
                'price': int(m.group('price').replace(',', '')),
            })

        if products:
            results.append({
                'category': category,
                'category_id': select.get('name', ''),
                'products': products,
            })

    return results


# ---------- 讀寫 JSON ----------
def load_existing():
    if JSON_PATH.exists():
        with JSON_PATH.open('r', encoding='utf-8') as f:
            return json.load(f)
    return None


def save_json(data):
    with JSON_PATH.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')


# ---------- 合併新舊資料 ----------
def merge(existing, scraped, today: str):
    """
    回傳 (merged_data, changed)
    - changed=False 時表示完全沒變，呼叫端不應寫檔
    """

    # 首次執行
    if existing is None:
        cats = []
        for c in scraped:
            prods = []
            for p in c['products']:
                prods.append({
                    'name': p['name'],
                    'spec': p['spec'],
                    'value': p['value'],
                    'current_price': p['price'],
                    'history': [{'date': today, 'price': p['price']}],
                })
            if prods:
                cats.append({
                    'category': c['category'],
                    'category_id': c['category_id'],
                    'products': prods,
                })
        return {'last_updated': today, 'categories': cats}, True

    # 既有資料索引：(category_id, name) -> product dict
    old_map = {}
    for cat in existing.get('categories', []):
        for p in cat['products']:
            old_map[(cat['category_id'], p['name'])] = p

    changed = False
    seen_keys = set()
    new_cats = []

    for c in scraped:
        prods = []
        for p in c['products']:
            key = (c['category_id'], p['name'])
            seen_keys.add(key)
            old = old_map.get(key)

            if old is None:
                # 新品項
                prods.append({
                    'name': p['name'],
                    'spec': p['spec'],
                    'value': p['value'],
                    'current_price': p['price'],
                    'history': [{'date': today, 'price': p['price']}],
                })
                changed = True
            else:
                hist = list(old.get('history', []))
                last_price = hist[-1]['price'] if hist else None

                if last_price != p['price']:
                    # 價格有變動 → 追加一筆歷史
                    hist.append({'date': today, 'price': p['price']})
                    prods.append({
                        'name': p['name'],
                        'spec': p['spec'],
                        'value': p['value'],
                        'current_price': p['price'],
                        'history': hist,
                    })
                    changed = True
                else:
                    # 價格沒變 → 保留舊資料，完全不動
                    prods.append(old)

        if prods:
            new_cats.append({
                'category': c['category'],
                'category_id': c['category_id'],
                'products': prods,
            })

    # 舊資料中本次未出現的品項：保留（避免歷史遺失）
    cat_map = {c['category_id']: c for c in new_cats}
    for old_cat in existing.get('categories', []):
        cid = old_cat['category_id']
        for old_p in old_cat['products']:
            if (cid, old_p['name']) in seen_keys:
                continue
            if cid in cat_map:
                cat_map[cid]['products'].append(old_p)
            else:
                new_cats.append({
                    'category': old_cat['category'],
                    'category_id': cid,
                    'products': list(old_cat['products']),
                })

    if not changed:
        return existing, False

    return {'last_updated': today, 'categories': new_cats}, True


# ---------- 主程式 ----------
def main():
    today = datetime.now(GMT8).strftime('%Y-%m-%d')

    html = fetch_html(URL)
    scraped = parse_products(html)
    print(f'[scrape] 抓取到 {len(scraped)} 個分類', file=sys.stderr)

    existing = load_existing()
    merged, changed = merge(existing, scraped, today)

    if changed:
        save_json(merged)
        print(f'[scrape] 價格有變動，已寫入 {JSON_PATH}', file=sys.stderr)
    else:
        print('[scrape] 價格無變動，不寫檔', file=sys.stderr)


if __name__ == '__main__':
    main()