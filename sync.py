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
        print('❌ 매장 API 응답이 JSON이 아닙니다.')
        print(res.text[:1000])
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
            name = norm(s.get('name', ''))
            idx = s.get('idx')

            if name and idx is not None:
                stores[name] = idx

    print(
        f'📍 매장 {len(stores)}개: '
        f'{list(stores.keys())}'
    )

    return stores
# ── 3. 재고 조회 ──────────────────────────────────────
def get_all_stock(session, store_list):
    print('📦 재고 데이터 조회 중...')

    if not isinstance(store_list, dict):
        raise Exception(
            f'매장 목록 데이터가 올바르지 않습니다: {store_list}'
        )
    
    idx_to_store = {v: k for k, v in store_list.items()}
    all_stock = []
    page = 1

    while True:
        res = session.get(
            f'{POS_BASE_URL}/product/variant/stock',
            params={
                'page': page,
                'perPage': 15
            }
        )

        print(
            f'  재고 API 응답: {res.status_code} '
            f'({res.headers.get("Content-Type", "")})'
        )

        if res.status_code != 200:
            print(f'⚠️ 재고 조회 실패 (page {page}): {res.status_code}')
            print(f'응답 내용: {res.text[:1000]}')
            break

        try:
            data = res.json()
        except Exception:
            print('❌ 재고 API 응답이 JSON이 아닙니다.')
            print(res.text[:1000])
            break

        if isinstance(data, list):
            items = data
            last_page = 1
        elif isinstance(data, dict):
            items = data.get('data', [])
            meta = data.get('meta', {})
            last_page = meta.get('last_page', 1)
        else:
            items = []
            last_page = 1

        if not items:
            print(f'  재고 데이터 없음 (page {page})')
            break

        for item in items:

            barcode = str(
                (item.get('barcode') or {}).get('code1', '')
                or item.get('code1', '')
                or ''
            ).strip()

            if not barcode or barcode == 'None':
                continue

            product_name = (
                (item.get('product') or {}).get('name', '')
                or (item.get('product_class') or {}).get('name', '')
                or item.get('original_name', '')
                or ''
            )

            option_name = (
                item.get('origin_option_name', '')
                or item.get('option_name', '')
                or ''
            )

            stocks = item.get('stocks') or []

            if stocks:

                for s in stocks:

                    store_idx = (
                        s.get('store_idx')
                        or (s.get('warehouse') or {}).get('store_idx')
                    )

                    store_name = idx_to_store.get(
                        store_idx,
                        ''
                    )

                    if not store_name:
                        store_name = norm(
                            s.get('store_name', '')
                            or (s.get('warehouse') or {})
                                .get('store', {})
                                .get('name', '')
                            or ''
                        )

                    qty = int(
                        s.get('stock', 0)
                        or s.get('qty', 0)
                        or 0
                    )

                    all_stock.append({
                        'store': store_name,
                        'barcode': barcode,
                        'name': product_name,
                        'option': option_name,
                        'stock': qty
                    })

            else:

                total = int(
                    item.get('total_stock', 0)
                    or 0
                )

                all_stock.append({
                    'store': 'ALL',
                    'barcode': barcode,
                    'name': product_name,
                    'option': option_name,
                    'stock': total
                })

        print(
            f'  재고 page {page}/{last_page} '
            f'({len(all_stock)}건)'
        )

        if page >= last_page:
            break

        page += 1

    print(f'✅ 재고 총 {len(all_stock)}건')

    if all_stock:
        print(f'  샘플: {all_stock[0]}')

    return all_stock
    
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
    
if __name__ == '__main__':
    print(
        f'🚀 동기화 시작: '
        f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
    )

    try:
        session = login()

        store_list = get_store_list(session)

        stock_data = get_all_stock(
            session,
            store_list
        )

        sales_data = get_sales(
            session,
            store_list
        )

        save_to_sheets(
            stock_data,
            sales_data
        )

        print('🎉 동기화 완료!')

    except Exception as e:
        print(f'❌ 오류 발생: {e}')
        raise
