# =====================================================
# 헤트라스 셀메이트 자동 동기화 스크립트
# - P코드 우선
# - 최근 14일 매출 P코드 기준 재저장
# - 판매속도 최근 7일 P코드 기준 자동 재생성
# =====================================================
import os, requests, gspread, json, re
from datetime import datetime, timedelta
from collections import defaultdict
from google.oauth2.service_account import Credentials

SELLMATE_ID     = os.environ['SELLMATE_ID']
SELLMATE_PW     = os.environ['SELLMATE_PW']
SELLMATE_DOMAIN = os.environ.get('SELLMATE_DOMAIN', 'hetras')
SPREADSHEET_ID  = os.environ['SPREADSHEET_ID']
GOOGLE_CREDS    = json.loads(os.environ['GOOGLE_CREDENTIALS'])

BASE_URL = 'https://sellmatepos.com/json'
SALES_LOOKBACK_DAYS = 14
VELOCITY_DAYS = 7

def norm(s):
    return str(s).strip().rstrip('점').rstrip('店')

def norm_name(s):
    return re.sub(r'[\s_\-()/\[\]]+', '', str(s or '')).lower()

def scalar(v):
    if v is None:
        return ''
    if isinstance(v, (str, int, float)):
        return str(v).strip()
    return ''

def walk_dicts(data):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from walk_dicts(v)
    elif isinstance(data, list):
        for v in data:
            yield from walk_dicts(v)

def is_p_code(v):
    s = scalar(v)
    return bool(re.match(r'^P[A-Z0-9_-]+$', s, re.I))

def find_p_code(data):
    preferred = (
        'itemCode','item_code','variantCode','variant_code',
        'sku','productCode','product_code','goodsCode','goods_code',
        'code'
    )
    for obj in walk_dicts(data):
        for k in preferred:
            if k in obj and is_p_code(obj.get(k)):
                return scalar(obj.get(k))

    # 최후 보조: 객체 전체에서 P로 시작하는 코드형 값 탐색
    for obj in walk_dicts(data):
        for v in obj.values():
            if is_p_code(v):
                return scalar(v)
    return ''

def find_legacy_barcode(data):
    keys = (
        'barcode','barcode1','barcode2','barcode3','barcodeNo',
        'barcode_number','productBarcode','product_barcode',
        'code1','code2','code3','globalBarcode','global_barcode'
    )
    for obj in walk_dicts(data):
        for k in keys:
            if k not in obj:
                continue
            v = obj[k]
            if isinstance(v, dict):
                for sk in ('code','code1','code2','code3'):
                    s = scalar(v.get(sk))
                    if s:
                        return s
            else:
                s = scalar(v)
                if s:
                    return s
    return ''

def get_product_code(data, product_name='', product_name_map=None):
    # 1순위: API 안의 실제 P코드
    p = find_p_code(data)
    if p:
        return p

    # 2순위: 상품마스터 상품명 → P코드
    if product_name_map and product_name:
        p = product_name_map.get(norm_name(product_name), '')
        if p:
            return p

    # 3순위: P코드를 못 찾은 경우에만 구형코드 반환
    return find_legacy_barcode(data)

def login():
    print('🔐 셀메이트 로그인 중...')
    res = requests.post(
        f'{BASE_URL}/auth/login',
        json={
            'domain': SELLMATE_DOMAIN,
            'id': SELLMATE_ID,
            'pw': SELLMATE_PW,
            'isSellmateAdmin': 0
        },
        headers={
            'Content-Type':'application/json',
            'x-pos-domain':SELLMATE_DOMAIN,
            'x-api-version':'2.2',
            'sellmate-pos-js-version':'2.8.2',
            'User-Agent':'Mozilla/5.0'
        },
        timeout=30
    )
    if res.status_code != 200:
        raise Exception(f'로그인 실패: {res.status_code} {res.text[:200]}')

    token_info = res.cookies.get('tokenInfo')
    if token_info:
        import urllib.parse
        token_data = json.loads(urllib.parse.unquote(token_info))
        token = token_data.get('access_token')
    else:
        data = res.json()
        token = data.get('access_token') or data.get('token')

    if not token:
        raise Exception('토큰 추출 실패')

    print('✅ 로그인 성공')
    return token, res.cookies

def get_headers(token, cookies):
    return {
        'Authorization':f'Bearer {token}',
        'x-pos-domain':SELLMATE_DOMAIN,
        'x-api-version':'2.2',
        'sellmate-pos-js-version':'2.8.2',
        'Accept':'application/json',
        'User-Agent':'Mozilla/5.0',
        'Cookie':'; '.join([f'{k}={v}' for k,v in cookies.items()])
    }

def google_open():
    creds = Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)

def load_product_name_map(sh):
    """
    상품마스터 B열 바코드(P코드), C열 상품명 기준 매핑.
    API가 구형 88코드만 내려줘도 상품명이 같으면 P코드로 보정.
    """
    result = {}
    try:
        ws = sh.worksheet('상품마스터')
        vals = ws.get_all_values()
    except Exception as e:
        print(f'⚠️ 상품마스터 읽기 실패: {e}')
        return result

    if not vals:
        return result

    hdr = [str(x).strip() for x in vals[0]]
    try:
        bi = hdr.index('바코드')
        ni = hdr.index('상품명')
    except ValueError:
        print('⚠️ 상품마스터에 바코드/상품명 헤더가 없습니다.')
        return result

    for r in vals[1:]:
        if len(r) <= max(bi, ni):
            continue
        bc = str(r[bi]).strip()
        nm = str(r[ni]).strip()
        if bc and nm and is_p_code(bc):
            result[norm_name(nm)] = bc

    print(f'🧭 상품마스터 P코드 매핑 {len(result)}건 로드')
    return result

def get_store_list(headers):
    res = requests.get(f'{BASE_URL}/store?mode=list', headers=headers, timeout=30)
    if res.status_code != 200:
        raise Exception(f'매장 목록 조회 실패: {res.status_code}')

    stores = {}
    for s in res.json().get('data', []):
        stores[norm(s.get('name',''))] = s.get('idx')

    print(f'📍 매장 {len(stores)}개 조회됨')
    return stores

def get_all_stock(headers, product_name_map):
    print('📦 재고 데이터 조회 중...')
    all_stock = []
    page = 1

    while True:
        res = requests.get(
            f'{BASE_URL}/product/variant/stock',
            params={'page':page,'perPage':100},
            headers=headers,
            timeout=30
        )
        if res.status_code != 200:
            raise Exception(f'재고 조회 실패 page {page}: {res.status_code}')

        data = res.json()
        items = data.get('data', [])
        if not items:
            break

        all_stock.extend(items)

        last_page = data.get('meta', {}).get('last_page', 1)
        print(f'  재고 page {page}/{last_page}')
        if page >= last_page:
            break
        page += 1

    rows = []
    p_count = 0
    legacy_count = 0

    for item in all_stock:
        store_name = norm(item.get('store_name',''))
        variant = item.get('variant', {}) or {}
        product_name = ''
        if isinstance(variant, dict):
            product_name = (variant.get('product', {}) or {}).get('name','')

        product_code = get_product_code(item, product_name, product_name_map)
        if not product_code:
            continue

        qty = int(item.get('stock',0) or 0)

        if is_p_code(product_code):
            p_count += 1
        else:
            legacy_count += 1

        rows.append({
            'store':store_name,
            'barcode':product_code,
            'name':product_name,
            'qty':qty
        })

    print(f'✅ 재고 {len(rows)}건 / P코드 {p_count} / 구형코드 {legacy_count}')
    return rows

def get_sales(headers, store_list, product_name_map):
    print('💰 매출 데이터 조회 중...')
    today_dt = datetime.now()
    today = today_dt.strftime('%Y-%m-%d')
    start = (today_dt - timedelta(days=SALES_LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    sales = []
    p_count = 0
    legacy_count = 0

    for store_name, store_idx in store_list.items():
        page = 1
        while True:
            res = requests.get(
                f'{BASE_URL}/order',
                params={
                    'page':page,
                    'perPage':100,
                    'startDate':start,
                    'endDate':today
                },
                headers={**headers,'origin_useridx':str(store_idx)},
                timeout=30
            )
            if res.status_code != 200:
                print(f'  ⚠️ {store_name} 매출 조회 실패: {res.status_code}')
                break

            data = res.json()
            orders = data.get('data', [])
            if not orders:
                break

            for order in orders:
                if order.get('status') != 'normal':
                    continue

                order_date = order.get('transaction',{}).get('datetime','')[:10]

                for unit in order.get('ordered_unit', []):
                    sales_unit = unit.get('sales_unit', {}) or {}
                    product_class = sales_unit.get('product_class', {}) or {}
                    product_name = product_class.get('name','')

                    product_code = get_product_code(
                        unit,
                        product_name,
                        product_name_map
                    )
                    qty = unit.get('qty',0)

                    if not product_code or not qty:
                        continue

                    if is_p_code(product_code):
                        p_count += 1
                    else:
                        legacy_count += 1

                    sales.append({
                        'date':order_date,
                        'store':store_name,
                        'barcode':product_code,
                        'name':product_name,
                        'qty':qty
                    })

            last_page = data.get('meta', {}).get('last_page', 1)
            if page >= last_page:
                break
            page += 1

        print(f'  {store_name} 매출 조회 완료')

    print(f'✅ 매출 {len(sales)}건 / P코드 {p_count} / 구형코드 fallback {legacy_count}')
    return sales

def save_stock(sh, stock_data):
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        ws = sh.worksheet('재고데이터')
    except:
        ws = sh.add_worksheet('재고데이터', 1000, 5)

    existing = ws.get_all_values()
    old = existing[1:] if len(existing) > 1 else []
    keep = [r for r in old if r and r[0] != today]

    new_rows = [
        [today,x['store'],x['barcode'],x['name'],x['qty']]
        for x in stock_data
    ]

    ws.clear()
    ws.update(
        [['날짜','매장','바코드','상품명','현재고']] + keep + new_rows,
        'A1'
    )
    print(f'  ✅ 재고데이터 {len(new_rows)}건 저장')

def save_sales(sh, sales_data):
    today_dt = datetime.now()
    today = today_dt.strftime('%Y-%m-%d')
    replace_start = (
        today_dt - timedelta(days=SALES_LOOKBACK_DAYS)
    ).strftime('%Y-%m-%d')

    try:
        ws = sh.worksheet('매출데이터')
    except:
        ws = sh.add_worksheet('매출데이터', 1000, 5)

    existing = ws.get_all_values()
    old = existing[1:] if len(existing) > 1 else []

    # 최근 14일 구간은 통째로 새 API 데이터로 교체
    keep = [
        r for r in old
        if not r or not r[0] or r[0] < replace_start or r[0] > today
    ]

    new_rows = [
        [x['date'],x['store'],x['barcode'],x['name'],x['qty']]
        for x in sales_data
        if replace_start <= x['date'] <= today
    ]

    ws.clear()
    ws.update(
        [['날짜','매장','바코드','상품명','판매수량']] + keep + new_rows,
        'A1'
    )
    print(f'  ✅ 매출데이터 최근 {SALES_LOOKBACK_DAYS}일 {len(new_rows)}건 재저장')

def rebuild_velocity(sh, sales_data, product_name_map):
    """
    판매속도를 최근 7일 기준으로 완전히 재생성.
    최종 바코드는 무조건:
      API P코드 → 상품마스터 P코드 매핑 → (정말 없으면 구형코드)
    """
    today_dt = datetime.now()
    today = today_dt.date()
    start = today - timedelta(days=VELOCITY_DAYS - 1)

    summary = defaultdict(lambda: {
        'qty':0,
        'name':''
    })

    for s in sales_data:
        try:
            d = datetime.strptime(s['date'], '%Y-%m-%d').date()
        except Exception:
            continue

        if d < start or d > today:
            continue

        bc = str(s.get('barcode','')).strip()
        nm = str(s.get('name','')).strip()

        # 혹시 sales_data에 구형코드가 남아도 상품마스터 이름으로 다시 P코드 보정
        if not is_p_code(bc):
            mapped = product_name_map.get(norm_name(nm), '')
            if mapped:
                bc = mapped

        if not bc:
            continue

        key = (s['store'], bc, nm)
        summary[key]['qty'] += int(float(s.get('qty',0) or 0))
        summary[key]['name'] = nm

    rows = []
    legacy_rows = []

    period = f'{start.isoformat()} ~ {today.isoformat()}'

    for (store, bc, nm), data in sorted(summary.items()):
        qty = data['qty']

        if not is_p_code(bc):
            legacy_rows.append((store, bc, nm))

        rows.append([
            today.isoformat(),
            period,
            store,
            bc,
            nm,
            '',                 # 옵션명
            qty,
            round(qty / VELOCITY_DAYS, 2),
            VELOCITY_DAYS
        ])

    try:
        ws = sh.worksheet('판매속도')
    except:
        ws = sh.add_worksheet('판매속도', 1000, 9)

    ws.clear()
    header = [
        '기준일','조회기간','매장','바코드','상품명','옵션명',
        '7일 순판매수량','일평균 판매수량','계산일수'
    ]
    ws.update([header] + rows, 'A1')

    print(f'📈 판매속도 {len(rows)}건 P코드 기준 재생성 완료')

    if legacy_rows:
        print(f'⚠️ 판매속도에 P코드로 변환하지 못한 행 {len(legacy_rows)}건')
        for x in legacy_rows[:20]:
            print('   fallback:', x)
    else:
        print('✅ 판매속도 바코드 100% P코드 확인')

def main():
    print(f'🚀 동기화 시작: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    sh = google_open()
    product_name_map = load_product_name_map(sh)

    token, cookies = login()
    headers = get_headers(token, cookies)

    store_list = get_store_list(headers)
    stock_data = get_all_stock(headers, product_name_map)
    sales_data = get_sales(headers, store_list, product_name_map)

    save_stock(sh, stock_data)
    save_sales(sh, sales_data)

    # 중요: 판매속도까지 이 실행에서 직접 재생성
    rebuild_velocity(sh, sales_data, product_name_map)

    print('🎉 동기화 완료!')

if __name__ == '__main__':
    main()
