# =====================================================
# 헤트라스 셀메이트 자동 동기화 스크립트
# GitHub Actions에서 매시간 자동 실행
# =====================================================
import os, requests, gspread, json
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

# ── 매장명 정규화 ─────────────────────────────────────
def norm(s):
    return str(s).strip().rstrip('점').rstrip('店')

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
        }
    )
    if res.status_code != 200:
        raise Exception(f'로그인 실패: {res.status_code} {res.text[:200]}')
    
    # 토큰 추출 (쿠키에서)
    token_info = res.cookies.get('tokenInfo')
    if token_info:
        import urllib.parse
        token_data = json.loads(urllib.parse.unquote(token_info))
        token = token_data.get('access_token')
    else:
        # 응답 body에서 시도
        data = res.json()
        token = data.get('access_token') or data.get('token')
    
    if not token:
        raise Exception('토큰 추출 실패')
    
    print(f'✅ 로그인 성공')
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
    res = requests.get(f'{BASE_URL}/store?mode=list', headers=headers)
    if res.status_code != 200:
        print(f'⚠️ 매장 목록 조회 실패: {res.status_code}')
        return {}
    
    raw = res.json()
    # 응답이 리스트일 수도, {"data": [...]} 형태일 수도 있음
    if isinstance(raw, list):
        store_items = raw
    elif isinstance(raw, dict):
        store_items = raw.get('data', [])
    else:
        store_items = []
    
    stores = {}
    for s in store_items:
        if isinstance(s, dict):
            stores[norm(s.get('name',''))] = s.get('idx')
    print(f'📍 매장 {len(stores)}개 조회됨: {list(stores.keys())}')
    return stores

# ── 4. 재고 전체 조회 ─────────────────────────────────
def get_all_stock(headers, store_list):
    print('📦 재고 데이터 조회 중...')
    idx_to_store = {v: k for k, v in store_list.items()}
    all_stock = []
    page = 1
    per_page = 100

    while True:
        res = requests.get(
            f'{BASE_URL}/product/variant/stock',
            params={'page': page, 'perPage': per_page},
            headers=headers
        )
        if res.status_code != 200:
            print(f'⚠️ 재고 조회 실패 (page {page}): {res.status_code}')
            break

        data = res.json()
        if isinstance(data, list):
            items = data
            last_page = 1
        else:
            items = data.get('data', [])
            last_page = data.get('meta', {}).get('last_page', 1)

        if not items:
            break

        for item in items:
            variant = item or {}
            # 바코드 추출
            barcode = str((variant.get('barcode') or {}).get('code1', '') or '').strip()
            if not barcode or barcode == 'None':
                barcode = str(variant.get('code1', '') or '').strip()
            if not barcode:
                continue

            product_name = ((variant.get('product') or {}).get('name', '') or
                           (variant.get('product_class') or {}).get('name', '') or
                           variant.get('original_name', '') or '')
            option_name = variant.get('origin_option_name', '') or variant.get('option_name', '') or ''

            # stocks 배열에서 매장별 재고 추출
            stocks = variant.get('stocks') or []
            if stocks:
                for s in stocks:
                    store_idx = (s.get('store_idx') or
                                (s.get('warehouse') or {}).get('store_idx'))
                    store_name = idx_to_store.get(store_idx, '')
                    if not store_name:
                        store_name = norm((s.get('store_name') or
                                          (s.get('warehouse') or {}).get('store', {}).get('name', '') or ''))
                    qty = int(s.get('stock', 0) or s.get('qty', 0) or 0)
                    all_stock.append({
                        'store': store_name,
                        'barcode': barcode,
                        'name': product_name,
                        'option': option_name,
                        'stock': qty
                    })
            else:
                total = int(variant.get('total_stock', 0) or 0)
                all_stock.append({
                    'store': 'ALL',
                    'barcode': barcode,
                    'name': product_name,
                    'option': option_name,
                    'stock': total
                })

        print(f'  재고 page {page}/{last_page} ({len(all_stock)}건)')
        if page >= last_page:
            break
        page += 1

    print(f'✅ 재고 총 {len(all_stock)}건 조회 완료')
    if all_stock:
        print(f'  샘플: {all_stock[0]}')
    return all_stock

# ── 5. 매출 조회 (최근 14일) ──────────────────────────
def get_sales(headers, store_list):
    print('💰 매출 데이터 조회 중...')
    today = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    all_sales = []

    for store_name, store_idx in store_list.items():
        page = 1
        store_sales_count = 0
        while True:
            res = requests.get(
                f'{BASE_URL}/order',
                params={
                    'page': page,
                    'perPage': 100,
                    'startDate': start,
                    'endDate': today,
                    'storeIdx': store_idx,   # 매장 idx 파라미터로 전달
                },
                headers=headers
            )
            if res.status_code != 200:
                print(f'  ⚠️ {store_name} 매출 조회 실패: {res.status_code}')
                break

            data = res.json()
            # 응답이 리스트 또는 딕셔너리 처리
            if isinstance(data, list):
                orders = data
                last_page = 1
            else:
                orders = data.get('data', [])
                last_page = data.get('meta', {}).get('last_page', 1)

            if not orders:
                break

            for order in orders:
                if order.get('status') != 'normal':
                    continue
                order_date = order.get('transaction', {}).get('datetime', '')[:10]
                for unit in (order.get('ordered_unit') or []):
                    if not unit:
                        continue
                    su = unit.get('sales_unit') or {}
                    barcode = (su.get('barcode') or {}).get('code1', '')
                    qty = unit.get('qty', 0)
                    name = (su.get('product_class') or {}).get('name', '')
                    if barcode and qty and int(qty) > 0:
                        all_sales.append({
                            'date': order_date,
                            'store': store_name,
                            'barcode': str(barcode),
                            'name': name,
                            'qty': int(qty)
                        })
                        store_sales_count += 1

            if page >= last_page:
                break
            page += 1

        print(f'  {store_name} 매출 {store_sales_count}건 조회 완료')

    print(f'✅ 매출 총 {len(all_sales)}건 조회 완료')
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
    today = datetime.now().strftime('%Y-%m-%d')
    
    # ── 재고 시트 저장 ──────────────────────────────
    try:
        ws = sh.worksheet('재고데이터')
    except:
        ws = sh.add_worksheet('재고데이터', 10000, 6)
        ws.append_row(['날짜', '매장', '바코드', '상품명', '옵션명', '현재고'])

    existing = ws.get_all_values()
    rows_to_keep = [r for r in existing if r and r[0] != today]

    stock_rows = []
    for item in stock_data:
        store_name = item.get('store', '')
        barcode = str(item.get('barcode', '') or '').strip()
        product_name = item.get('name', '')
        option_name = item.get('option', '')
        stock_qty = int(item.get('stock', 0) or 0)

        if not barcode or barcode == 'None' or not store_name or store_name == 'ALL':
            continue

        stock_rows.append([today, store_name, barcode, product_name, option_name, stock_qty])

    print(f'  📦 저장할 재고 행수: {len(stock_rows)}')
    if stock_rows:
        print(f'  📋 재고 샘플행: {stock_rows[0]}')

    ws.clear()
    all_rows = [['날짜', '매장', '바코드', '상품명', '옵션명', '현재고']] + rows_to_keep[1:] + stock_rows
    ws.update(values=all_rows, range_name='A1')
    print(f'  ✅ 재고 {len(stock_rows)}건 저장')
    
    # ── 매출 시트 저장 ──────────────────────────────
    try:
        ws2 = sh.worksheet('매출데이터')
    except:
        ws2 = sh.add_worksheet('매출데이터', 1000, 5)
        ws2.append_row(['날짜', '매장', '바코드', '상품명', '판매수량'])
    
    existing2 = ws2.get_all_values()
    # 오늘 날짜 데이터만 교체
    rows_to_keep2 = [r for r in existing2 if r and r[0] != today]
    
    sales_rows = [[s['date'], s['store'], s['barcode'], s['name'], s['qty']] for s in sales_data if s['date'] == today]
    
    ws2.clear()
    all_rows2 = [['날짜', '매장', '바코드', '상품명', '판매수량']] + rows_to_keep2[1:] + sales_rows
    ws2.update(values=all_rows2, range_name='A1')
    print(f'  ✅ 매출 {len(sales_rows)}건 저장')

# ── 메인 실행 ─────────────────────────────────────────
if __name__ == '__main__':
    print(f'🚀 동기화 시작: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    
    try:
        token, cookies = login()
        headers = get_headers(token, cookies)
        store_list = get_store_list(headers)
        stock_data = get_all_stock(headers, store_list)
        sales_data = get_sales(headers, store_list)
        save_to_sheets(stock_data, sales_data)
        print('🎉 동기화 완료!')
    except Exception as e:
        print(f'❌ 오류 발생: {e}')
        raise
