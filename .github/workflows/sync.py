# =====================================================
# 헤트라스 셀메이트 자동 동기화 스크립트
# GitHub Actions 자동 실행
# 수정: P코드 우선 사용 / 최근 14일 매출 P코드 재정리
# =====================================================
import os, requests, gspread, json, re
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

# ── 설정 ─────────────────────────────────────────────
SELLMATE_ID     = os.environ['SELLMATE_ID']
SELLMATE_PW     = os.environ['SELLMATE_PW']
SELLMATE_DOMAIN = os.environ.get('SELLMATE_DOMAIN', 'hetras')
SPREADSHEET_ID  = os.environ['SPREADSHEET_ID']
GOOGLE_CREDS    = json.loads(os.environ['GOOGLE_CREDENTIALS'])

STORES = ['안국', '명동', '성수', '제주', '한남', '홍대', '해운대']
BASE_URL = 'https://sellmatepos.com/json'
SALES_LOOKBACK_DAYS = 14

# ── 공통 유틸 ────────────────────────────────────────
def norm(s):
    return str(s).strip().rstrip('점').rstrip('店')

def scalar(v):
    if v is None:
        return ''
    if isinstance(v, (str, int, float)):
        return str(v).strip()
    return ''

def walk_dicts(data):
    """중첩 JSON 전체를 재귀 탐색."""
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from walk_dicts(v)
    elif isinstance(data, list):
        for v in data:
            yield from walk_dicts(v)

def is_p_code(v):
    s = scalar(v)
    return bool(s and re.match(r'^P[A-Z0-9_-]*$', s, re.I))

def find_p_code(data):
    """
    셀메이트에서 P코드를 최우선으로 찾는다.
    itemCode / variantCode / sku 등 후보 키 중 실제 값이 P로 시작하는 것만 채택.
    """
    preferred_keys = (
        'itemCode', 'item_code',
        'variantCode', 'variant_code',
        'sku',
        'productCode', 'product_code',
        'goodsCode', 'goods_code',
        'code'
    )

    # 1차: 명확한 코드성 키에서 P코드 탐색
    for obj in walk_dicts(data):
        for key in preferred_keys:
            if key in obj and is_p_code(obj.get(key)):
                return scalar(obj.get(key))

    # 2차: 키 이름과 무관하게 중첩 전체에서 P코드 형태 값 탐색
    for obj in walk_dicts(data):
        for v in obj.values():
            if is_p_code(v):
                return scalar(v)

    return ''

def find_legacy_barcode(data):
    """
    P코드가 없을 때만 사용하는 기존 바코드 fallback.
    880... / code1 등.
    """
    barcode_keys = (
        'barcode', 'barcode1', 'barcode2', 'barcode3', 'barcodeNo',
        'barcode_number', 'productBarcode', 'product_barcode',
        'code1', 'code2', 'code3',
        'globalBarcode', 'global_barcode',
    )

    for obj in walk_dicts(data):
        for key in barcode_keys:
            if key not in obj:
                continue
            v = obj.get(key)

            # barcode가 객체인 경우 code/code1 등을 우선 확인
            if isinstance(v, dict):
                for subkey in ('code', 'code1', 'code2', 'code3'):
                    s = scalar(v.get(subkey))
                    if s:
                        return s
            else:
                s = scalar(v)
                if s:
                    return s

    return ''

def get_product_code(data):
    """
    최종 상품코드 결정:
    1) P코드
    2) P코드가 정말 없을 때만 기존 88/바코드
    """
    pcode = find_p_code(data)
    if pcode:
        return pcode
    return find_legacy_barcode(data)

# ── 1. 셀메이트 로그인 ────────────────────────────────
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
            'Content-Type': 'application/json',
            'x-pos-domain': SELLMATE_DOMAIN,
            'x-api-version': '2.2',
            'sellmate-pos-js-version': '2.8.2',
            'User-Agent': 'Mozilla/5.0'
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

# ── 2. 공통 헤더 ──────────────────────────────────────
def get_headers(token, cookies):
    return {
        'Authorization': f'Bearer {token}',
        'x-pos-domain': SELLMATE_DOMAIN,
        'x-api-version': '2.2',
        'sellmate-pos-js-version': '2.8.2',
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0',
        'Cookie': '; '.join([f'{k}={v}' for k, v in cookies.items()])
    }

# ── 3. 매장 목록 조회 ─────────────────────────────────
def get_store_list(headers):
    res = requests.get(f'{BASE_URL}/store?mode=list', headers=headers, timeout=30)
    if res.status_code != 200:
        print(f'⚠️ 매장 목록 조회 실패: {res.status_code}')
        return {}

    stores = {}
    for s in res.json().get('data', []):
        stores[norm(s.get('name',''))] = s.get('idx')

    print(f'📍 매장 {len(stores)}개 조회됨: {list(stores.keys())}')
    return stores

# ── 4. 재고 전체 조회 ─────────────────────────────────
def get_all_stock(headers):
    print('📦 재고 데이터 조회 중...')
    all_stock = []
    page = 1
    per_page = 100

    while True:
        res = requests.get(
            f'{BASE_URL}/product/variant/stock',
            params={'page': page, 'perPage': per_page},
            headers=headers,
            timeout=30
        )
        if res.status_code != 200:
            print(f'⚠️ 재고 조회 실패 (page {page}): {res.status_code}')
            break

        data = res.json()
        items = data.get('data', [])
        if not items:
            break

        all_stock.extend(items)

        meta = data.get('meta', {})
        last_page = meta.get('last_page', 1)
        print(f'  재고 page {page}/{last_page} ({len(all_stock)}건)')

        if page >= last_page:
            break
        page += 1

    print(f'✅ 재고 총 {len(all_stock)}건 조회 완료')
    return all_stock

# ── 5. 매출 조회 (최근 14일) ──────────────────────────
def get_sales(headers, store_list):
    print('💰 매출 데이터 조회 중...')
    today_dt = datetime.now()
    today = today_dt.strftime('%Y-%m-%d')
    start = (today_dt - timedelta(days=SALES_LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    all_sales = []
    pcode_count = 0
    fallback_count = 0

    for store_name, store_idx in store_list.items():
        page = 1

        while True:
            res = requests.get(
                f'{BASE_URL}/order',
                params={
                    'page': page,
                    'perPage': 100,
                    'startDate': start,
                    'endDate': today,
                },
                headers={**headers, 'origin_useridx': str(store_idx)},
                timeout=30
            )

            if res.status_code != 200:
                print(f'  ⚠️ {store_name} 매출 조회 실패')
                break

            data = res.json()
            orders = data.get('data', [])
            if not orders:
                break

            for order in orders:
                if order.get('status') != 'normal':
                    continue

                order_date = order.get('transaction', {}).get('datetime', '')[:10]

                for unit in order.get('ordered_unit', []):
                    # 핵심: 기존 code1 고정 대신 P코드 최우선
                    product_code = get_product_code(unit)
                    qty = unit.get('qty', 0)

                    if not product_code or not qty:
                        continue

                    if is_p_code(product_code):
                        pcode_count += 1
                    else:
                        fallback_count += 1

                    all_sales.append({
                        'date': order_date,
                        'store': store_name,
                        'barcode': product_code,
                        'name': unit.get('sales_unit', {}).get('product_class', {}).get('name', ''),
                        'qty': qty
                    })

            meta = data.get('meta', {})
            if page >= meta.get('last_page', 1):
                break
            page += 1

        print(f'  {store_name} 매출 조회 완료')

    print(f'✅ 매출 총 {len(all_sales)}건 조회 완료')
    print(f'   P코드 사용: {pcode_count}건')
    print(f'   기존 바코드 fallback: {fallback_count}건')

    if fallback_count:
        print('⚠️ 일부 상품은 API 응답에서 P코드를 찾지 못해 기존 바코드를 사용했습니다.')

    return all_sales

# ── 6. 구글 시트 저장 ─────────────────────────────────
def save_to_sheets(stock_data, sales_data):
    print('📊 구글 시트에 저장 중...')

    creds = Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    today_dt = datetime.now()
    today = today_dt.strftime('%Y-%m-%d')
    sales_replace_start = (today_dt - timedelta(days=SALES_LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    # ── 재고 시트 저장 ──────────────────────────────
    try:
        ws = sh.worksheet('재고데이터')
    except:
        ws = sh.add_worksheet('재고데이터', 1000, 5)
        ws.append_row(['날짜', '매장', '바코드', '상품명', '현재고'])

    existing = ws.get_all_values()
    rows_to_keep = [r for r in existing if r and r[0] != today]

    stock_rows = []
    stock_pcode_count = 0
    stock_fallback_count = 0

    for item in stock_data:
        store_name = norm(item.get('store_name', ''))

        # 핵심: 재고도 P코드 우선
        product_code = get_product_code(item)

        variant = item.get('variant', {}) or {}
        product_name = (
            variant.get('product', {}).get('name', '')
            if isinstance(variant, dict) else ''
        )

        stock_qty = int(item.get('stock', 0) or 0)

        if not product_code:
            continue

        if is_p_code(product_code):
            stock_pcode_count += 1
        else:
            stock_fallback_count += 1

        stock_rows.append([today, store_name, product_code, product_name, stock_qty])

    ws.clear()
    all_rows = [['날짜', '매장', '바코드', '상품명', '현재고']] + rows_to_keep[1:] + stock_rows
    ws.update('A1', all_rows)

    print(f'  ✅ 재고 {len(stock_rows)}건 저장')
    print(f'     P코드 {stock_pcode_count}건 / fallback {stock_fallback_count}건')

    # ── 매출 시트 저장 ──────────────────────────────
    try:
        ws2 = sh.worksheet('매출데이터')
    except:
        ws2 = sh.add_worksheet('매출데이터', 1000, 5)
        ws2.append_row(['날짜', '매장', '바코드', '상품명', '판매수량'])

    existing2 = ws2.get_all_values()

    # 중요:
    # 오늘만 교체하면 과거 88코드가 최근 7일 판매속도에 계속 남는다.
    # 따라서 API에서 다시 받은 최근 14일 구간 전체를 P코드 기준으로 교체한다.
    header2 = existing2[0] if existing2 else ['날짜', '매장', '바코드', '상품명', '판매수량']
    old_rows = existing2[1:] if len(existing2) > 1 else []

    rows_to_keep2 = [
        r for r in old_rows
        if not r or not r[0] or r[0] < sales_replace_start or r[0] > today
    ]

    sales_rows = [
        [s['date'], s['store'], s['barcode'], s['name'], s['qty']]
        for s in sales_data
        if sales_replace_start <= s['date'] <= today
    ]

    ws2.clear()
    all_rows2 = [['날짜', '매장', '바코드', '상품명', '판매수량']] + rows_to_keep2 + sales_rows
    ws2.update('A1', all_rows2)

    print(
        f'  ✅ 최근 {SALES_LOOKBACK_DAYS}일 매출 {len(sales_rows)}건 '
        f'P코드 우선 기준으로 재저장'
    )

# ── 메인 실행 ─────────────────────────────────────────
if __name__ == '__main__':
    print(f'🚀 동기화 시작: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

    try:
        token, cookies = login()
        headers = get_headers(token, cookies)
        store_list = get_store_list(headers)
        stock_data = get_all_stock(headers)
        sales_data = get_sales(headers, store_list)
        save_to_sheets(stock_data, sales_data)
        print('🎉 동기화 완료!')
    except Exception as e:
        print(f'❌ 오류 발생: {e}')
        raise
