# =====================================================
# 헤트라스 셀메이트 자동 동기화 스크립트 (완전체)
# =====================================================
import os, requests, gspread, json
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

SELLMATE_ID     = os.environ['SELLMATE_ID']
SELLMATE_PW     = os.environ['SELLMATE_PW']
SELLMATE_DOMAIN = os.environ.get('SELLMATE_DOMAIN', 'hetras')
SPREADSHEET_ID  = os.environ['SPREADSHEET_ID']
GOOGLE_CREDS    = json.loads(os.environ['GOOGLE_CREDENTIALS'])
BASE_URL        = 'https://sellmatepos.com/json'
POS_BASE_URL = 'https://sellmatepos.com'

def norm(s):
    return str(s).strip().rstrip('점').rstrip('店')

# ── 1. 로그인 (Session 방식) ──────────────────────────
def login():
    print('🔐 셀메이트 로그인 중...')

    session = requests.Session()

    session.headers.update({
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'x-pos-domain': SELLMATE_DOMAIN,
        'x-api-version': '2.2',
        'sellmate-pos-js-version': '2.8.4',
        'pos-locale': 'kr',
        'Referer': 'https://sellmatepos.com/',
        'Origin': 'https://sellmatepos.com',
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/151.0.0.0 Safari/537.36'
        ),
    })

    res = session.post(
        f'{BASE_URL}/auth/login',
        json={
            'domain': SELLMATE_DOMAIN,
            'id': SELLMATE_ID,
            'pw': SELLMATE_PW,
            'isSellmateAdmin': 0
        }
    )

    if res.status_code != 200:
        raise Exception(
            f'로그인 실패: {res.status_code} {res.text[:500]}'
        )

    token_info = session.cookies.get('tokenInfo')

    if token_info:
        import urllib.parse
        token_data = json.loads(
            urllib.parse.unquote(token_info)
        )
        token = token_data.get('access_token')
    else:
        data = res.json()
        token = (
            data.get('access_token')
            or data.get('token')
        )

    if not token:
        raise Exception('토큰 추출 실패')

    session.headers.update({
        'Authorization': f'Bearer {token}',
        'origin_useridx': '9',
        'pos-locale': 'kr',
        'sellmate-pos-js-version': '2.8.4',
        'x-api-version': '2.2',
        'x-pos-domain': SELLMATE_DOMAIN,
    })

    print(f'✅ 로그인 성공 (쿠키 {len(session.cookies)}개)')

    return session
    
# ── 2. 매장 목록 ──────────────────────────────────────
def get_store_list(session):
    res = session.get(f'{BASE_URL}/store?mode=list')
    if res.status_code != 200:
        print(f'⚠️ 매장 목록 조회 실패: {res.status_code}')
        return {}
    raw = res.json()
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get('data', [])
    else:
        items = []
    stores = {}
    for s in items:
        if isinstance(s, dict):
            stores[norm(s.get('name', ''))] = s.get('idx')
    print(f'📍 매장 {len(stores)}개: {list(stores.keys())}')
    return stores

# ── 3. 재고 조회 ──────────────────────────────────────
def get_store_list(session):
    print('🏪 매장 목록 조회 중...')

    res = session.get(
        f'{BASE_URL}/store?mode=list'
    )

    print(f'  매장 API 응답: {res.status_code}')
    print(f'  Content-Type: {res.headers.get("Content-Type")}')

    if res.status_code != 200:
        print(f'⚠️ 매장 목록 조회 실패: {res.status_code}')
        print(res.text[:1000])
        return {}

    try:
        raw = res.json()
    except Exception:
        print('❌ 매장 API가 JSON이 아닌 응답을 반환했습니다.')
        print(f'응답 내용: {res.text[:1000]}')
        return {}

    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get('data', [])
    else:
        items = []

    stores = {}

    for s in items:
        if isinstance(s, dict):
            stores[norm(s.get('name', ''))] = s.get('idx')

    print(f'📍 매장 {len(stores)}개: {list(stores.keys())}')

    return stores
    
# ── 4. 매출 조회 ──────────────────────────────────────
    def get_sales(session, store_list):
        print('💰 매출 데이터 조회 중...')
    today = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    start_dt = f'{start} 00:00:00'
    end_dt   = f'{today} 23:59:59'

    all_sales = []
    page = 1

    while True:
        res = session.get(f'{BASE_URL}/order', params=[
            ('page', page), ('perPage', 100),
            ('filters[0][field]', 'datetime'), ('filters[0][operator]', '>='), ('filters[0][value]', start_dt),
            ('filters[1][field]', 'datetime'), ('filters[1][operator]', '<='), ('filters[1][value]', end_dt),
            ('timeflag', 'true'),
            ('sort[0][field]', 'datetime'), ('sort[0][direction]', 'DESC'),
        ])
        if res.status_code != 200:
            print(f'  ⚠️ 매출 조회 실패 (page {page}): {res.status_code}')
            print(f'  응답 헤더: {dict(res.headers)}')
            print(f'  응답 내용: {res.text[:2000]}')
        break

        data = res.json()
        orders = data if isinstance(data, list) else data.get('data', [])
        last_page = 1 if isinstance(data, list) else data.get('last_page', 1)

        if not orders:
            break

        for order in orders:
            if order.get('order_type', '') not in ('판매', 'sale', 'normal', ''):
                continue
            store_name = norm(order.get('store_name', ''))
            order_date = str(order.get('datetime', ''))[:10]
            for item in (order.get('items') or []):
                barcode = str(item.get('barcode', '') or '').strip()
                if not barcode or barcode == 'None':
                    continue
                qty = int(item.get('qty', 0) or 0)
                if qty <= 0:
                    continue
                all_sales.append({
                    'date': order_date, 'store': store_name, 'barcode': barcode,
                    'name': item.get('product_name', '') or '',
                    'option': item.get('option_name', '') or '', 'qty': qty
                })

        print(f'  매출 page {page}/{last_page} (누적 {len(all_sales)}건)')
        if page >= last_page:
            break
        page += 1

    print(f'✅ 매출 총 {len(all_sales)}건')
    if all_sales:
        print(f'  샘플: {all_sales[0]}')
    return all_sales

# ── 5. 구글 시트 저장 ─────────────────────────────────
def save_to_sheets(stock_data, sales_data):
    print('📊 구글 시트에 저장 중...')
    creds = Credentials.from_service_account_info(
        GOOGLE_CREDS, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    today = datetime.now().strftime('%Y-%m-%d')

    # ── 재고 저장 ────────────────────────────────────
    try:
        ws = sh.worksheet('재고데이터')
    except:
        ws = sh.add_worksheet('재고데이터', 10000, 6)

    existing = ws.get_all_values()
    rows_to_keep = [r for r in existing if r and r[0] != today]

    stock_rows = []
    for item in stock_data:
        store_name = item.get('store', '')
        barcode = str(item.get('barcode', '') or '').strip()
        if not barcode or barcode == 'None' or not store_name or store_name == 'ALL':
            continue
        stock_rows.append([today, store_name, barcode,
                           item.get('name', ''), item.get('option', ''),
                           int(item.get('stock', 0) or 0)])

    print(f'  📦 저장할 재고: {len(stock_rows)}건')
    if stock_rows:
        print(f'  샘플: {stock_rows[0]}')

    ws.clear()
    all_rows = [['날짜', '매장', '바코드', '상품명', '옵션명', '현재고']] + rows_to_keep[1:] + stock_rows
    ws.update(values=all_rows, range_name='A1')
    print(f'  ✅ 재고 {len(stock_rows)}건 저장')

    # ── 매출 저장 ────────────────────────────────────
    try:
        ws2 = sh.worksheet('매출데이터')
    except:
        ws2 = sh.add_worksheet('매출데이터', 100000, 6)

    existing2 = ws2.get_all_values()
    rows_to_keep2 = [r for r in existing2 if r and r[0] != today]

    sales_rows = [[s['date'], s['store'], s['barcode'],
                   s['name'], s['option'], s['qty']] for s in sales_data if s['date'] == today]

    ws2.clear()
    all_rows2 = [['날짜', '매장', '바코드', '상품명', '옵션명', '판매수량']] + rows_to_keep2[1:] + sales_rows
    ws2.update(values=all_rows2, range_name='A1')
    print(f'  ✅ 매출 {len(sales_rows)}건 저장')

# ── 메인 ──────────────────────────────────────────────
if __name__ == '__main__':
    print(f'🚀 동기화 시작: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    try:
        session    = login()
        store_list = get_store_list(session)
        stock_data = get_all_stock(session, store_list)
        sales_data = get_sales(session, store_list)
        save_to_sheets(stock_data, sales_data)
        print('🎉 동기화 완료!')
    except Exception as e:
        print(f'❌ 오류 발생: {e}')
        raise

