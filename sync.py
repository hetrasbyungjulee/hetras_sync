import os
import json
import urllib.parse
import re
import time
import requests
import gspread

from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials


# =====================================================
# 헤트라스 셀메이트 자동 동기화
#
# 1. 셀메이트 로그인
# 2. 매장 목록 조회
# 3. 전체 매장 현재 재고 조회
# 4. 전체 매장 2026-07-01 이후 매출 조회
# 5. 판매 / 반품 구분
# 6. 주문번호 + 상품순번 기준 중복 방지
# 7. 최근 7일 판매속도 계산
# =====================================================


# =====================================================
# 환경변수
# =====================================================

SELLMATE_ID = os.environ["SELLMATE_ID"]
SELLMATE_PW = os.environ["SELLMATE_PW"]

SELLMATE_DOMAIN = os.environ.get(
    "SELLMATE_DOMAIN",
    "hetras"
)

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

GOOGLE_CREDS = json.loads(
    os.environ["GOOGLE_CREDENTIALS"]
)


# =====================================================
# 셀메이트 API
# =====================================================

BASE_URL = "https://sellmatepos.com/json"

EXTERNAL_BASE_URL = os.environ.get(
    "SELLMATE_EXTERNAL_BASE_URL",
    BASE_URL,
).rstrip("/")

SELLMATE_JS_VERSION = os.environ.get("SELLMATE_JS_VERSION", "2.8.5")

PER_PAGE = 100

# =====================================================
# 수정 설정
# =====================================================

#전체 데이터 저장
SALES_START_DATE = None

# 최근 7일 판매속도
SALES_AVERAGE_DAYS = 7


# 저장 단위
SHEET_CHUNK_SIZE = 5000

# API 재시도 횟수
API_RETRY_COUNT = 3
VERSION_RETRY_COUNT = 3
CURSOR_SHEET = "매출동기화상태"
CURSOR_HEADER = ["매장", "store_idx", "last_page", "last_page_saved_at", "status"]
FULL_RESCAN = os.environ.get("FULL_RESCAN", "false").lower() == "true"



# =====================================================
# 날짜 변환
# =====================================================

def get_order_date(order):

    value = str(
        order.get(
            "datetime",
            ""
        )
        or ""
    )

    if not value:
        return None

    try:

        return datetime.strptime(
            value[:10],
            "%Y-%m-%d"
        ).date()

    except:

        return None
# =====================================================
# 하루 1회 동기화 체크
# =====================================================

SYNC_LOG_SHEET = "동기화로그"


FORCE_SYNC = os.environ.get(
    "FORCE_SYNC",
    "false"
).lower() == "true"



def check_daily_sync():

    # 수동 강제 실행이면 통과
    if FORCE_SYNC:

        print(
            "⚡ 강제 실행 모드"
        )

        return False


    gc = get_google_client()

    sh = gc.open_by_key(
        SPREADSHEET_ID
    )


    try:

        ws = sh.worksheet(
            SYNC_LOG_SHEET
        )


    except gspread.WorksheetNotFound:


        ws = sh.add_worksheet(

            title=SYNC_LOG_SHEET,

            rows=100,

            cols=5
        )


        ws.append_row(
            [
                "날짜",
                "재고",
                "매출",
                "완료시간"
            ]
        )


        return False



    records = ws.get_all_values()


    today = get_today().strftime(
        "%Y-%m-%d"
    )


    for row in records[1:]:


        if row and row[0] == today:


            print(
                "⏭️ 오늘 이미 동기화 완료"
            )


            return True



    return False



def save_daily_sync():


    gc = get_google_client()

    sh = gc.open_by_key(
        SPREADSHEET_ID
    )


    ws = sh.worksheet(
        SYNC_LOG_SHEET
    )


    ws.append_row(

        [

            get_today().strftime(
                "%Y-%m-%d"
            ),

            "완료",

            "완료",

            datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        ]

    )

# =====================================================
# 공통
# =====================================================

def norm(value):

    return (
        str(value)
        .strip()
        .rstrip("점")
        .rstrip("店")
    )


def get_today():

    return datetime.now(
        timezone.utc
    ).astimezone().date()


def get_google_client():

    creds = Credentials.from_service_account_info(

        GOOGLE_CREDS,

        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    return gspread.authorize(creds)


# =====================================================
# Sellmate JS 버전 자동 탐색
# =====================================================
def detect_js_version(session):
    """Sellmate HTML/CSS에서 현재 JS/CSS 버전을 찾아 API 헤더에 적용."""
    candidates = []
    urls = [
        "https://sellmatepos.com/",
        "https://sellmatepos.com/pos",
        "https://sellmatepos.com/product/variant/stock",
    ]
    patterns = [
        r'sellmate-pos-js-version[^0-9]{0,30}(2\.\d+\.\d+)',
        r'sellmate-pos-js-version[\"\']?\s*[:=]\s*[\"\'](2\.\d+\.\d+)' ,
        r'\?v=(2\.\d+\.\d+)',
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=20)
            if r.status_code != 200:
                continue
            text = r.text or ""
            for pat in patterns:
                for v in re.findall(pat, text, flags=re.I):
                    if isinstance(v, tuple):
                        v = next((x for x in v if x), "")
                    if re.fullmatch(r"2\.\d+\.\d+", str(v)):
                        candidates.append(str(v))
        except Exception:
            continue
    if candidates:
        def verkey(v):
            return tuple(int(x) for x in v.split("."))
        version = max(set(candidates), key=verkey)
        print(f"  🔎 Sellmate 현재 JS 버전 자동 탐색: {version}")
        return version
    print(f"  ℹ️ 버전 자동 탐색 실패 → 기존 버전 사용: {SELLMATE_JS_VERSION}")
    return SELLMATE_JS_VERSION


def apply_js_version(session, version):
    global SELLMATE_JS_VERSION
    SELLMATE_JS_VERSION = version
    session.headers.update({"sellmate-pos-js-version": version})
    print(f"  🔄 JS 버전 적용: {version}")


# =====================================================
# 로그인
# =====================================================

def login():

    print("🔐 셀메이트 로그인 중...")

    session = requests.Session()

    session.headers.update({

        "Content-Type": "application/json",

        "Accept": "application/json",

        "x-pos-domain":
            SELLMATE_DOMAIN,

        "x-api-version":
            "2.2",

        "sellmate-pos-js-version":
            SELLMATE_JS_VERSION,

        "pos-locale":
            "kr",

        "Referer":
            "https://sellmatepos.com/",

        "Origin":
            "https://sellmatepos.com/",

        "User-Agent":
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
    })

    try:

        res = session.post(

            f"{BASE_URL}/auth/login",

            json={

                "domain":
                    SELLMATE_DOMAIN,

                "id":
                    SELLMATE_ID,

                "pw":
                    SELLMATE_PW,

                "isSellmateAdmin":
                    0,
            },

            timeout=30,
        )

    except requests.RequestException as e:

        raise Exception(
            f"셀메이트 로그인 요청 실패: {e}"
        )

    if res.status_code != 200:

        raise Exception(
            f"로그인 실패: "
            f"{res.status_code} "
            f"{res.text[:500]}"
        )

    token = None

    token_info = session.cookies.get(
        "tokenInfo"
    )

    if token_info:

        try:

            token_data = json.loads(
                urllib.parse.unquote(
                    token_info
                )
            )

            token = token_data.get(
                "access_token"
            )

        except Exception as e:

            print(
                f"⚠️ tokenInfo 파싱 실패: {e}"
            )

    if not token:

        try:

            data = res.json()

            token = (
                data.get("access_token")
                or data.get("token")
            )

        except Exception:
            pass

    if not token:

        raise Exception(
            "토큰 추출 실패"
        )

    session.headers.update({

        "Authorization":
            f"Bearer {token}",

        "origin_useridx":
            "9",

        "pos-locale":
            "kr",

        "sellmate-pos-js-version":
            SELLMATE_JS_VERSION,

        "x-api-version":
            "2.2",

        "x-pos-domain":
            SELLMATE_DOMAIN,
    })

    print(
        f"✅ 로그인 성공 "
        f"(쿠키 {len(session.cookies)}개)"
    )

    print(
        f"  셀메이트 JS 버전: "
        f"{SELLMATE_JS_VERSION}"
    )

    return session


# =====================================================
# 매장 목록
# =====================================================

def get_store_list(session):

    print("🏪 매장 목록 조회 중...")

    res = session.get(

        f"{BASE_URL}/store?mode=list",

        timeout=30,
    )

    print(
        f"  매장 API 응답: "
        f"{res.status_code}"
    )

    if res.status_code != 200:

        raise Exception(
            f"매장 목록 조회 실패: "
            f"{res.status_code} "
            f"{res.text[:500]}"
        )

    try:

        raw = res.json()

    except Exception:

        raise Exception(
            "매장 API 응답이 JSON이 아닙니다."
        )

    if isinstance(raw, list):

        items = raw

    elif isinstance(raw, dict):

        items = raw.get(
            "data",
            []
        )

    else:

        items = []

    stores = {}

    for store in items:

        if not isinstance(
            store,
            dict
        ):
            continue

        name = norm(
            store.get(
                "name",
                ""
            )
        )

        idx = (
            store.get("idx")
            or store.get("store_idx")
            or store.get("storeIdx")
        )

        if name and idx is not None:

            stores[name] = idx

    if not stores:

        raise Exception(
            "매장 목록이 비어 있습니다."
        )

    print(
        f"📍 매장 {len(stores)}개: "
        f"{list(stores.keys())}"
    )

    for name, idx in stores.items():

        print(
            f"  • {name}: store_idx={idx}"
        )

    return stores


# =====================================================
# 재고
# =====================================================

def get_all_stock(
    session,
    store_list
):

    print("📦 재고 데이터 조회 중...")

    idx_to_store = {
        value: key
        for key, value in store_list.items()
    }

    all_stock = []
    page = 1
    fallback_to_legacy = False

    while True:

        last_error = None
        data = None

        for attempt in range(1, API_RETRY_COUNT + 1):

            try:
                if fallback_to_legacy:
                    url = f"{BASE_URL}/product/variant/stock"
                else:
                    url = (
                        f"{EXTERNAL_BASE_URL}/external/"
                        f"{SELLMATE_DOMAIN}/stock"
                    )

                res = session.get(
                    url,
                    params={
                        "page": page,
                        "perPage": PER_PAGE,
                    },
                    timeout=60,
                )

                print(
                    f"  재고 API 응답: {res.status_code} "
                    f"(page {page})"
                )

                # 외부 API 경로가 서버에서 지원되지 않는 경우
                # 기존 /json 엔드포인트로 자동 fallback
                if (
                    not fallback_to_legacy
                    and res.status_code in (404, 405)
                ):
                    print(
                        "  ℹ️ External 재고 API 경로 미지원 → "
                        "기존 재고 API로 전환"
                    )
                    fallback_to_legacy = True
                    continue

                if res.status_code == 200:
                    try:
                        data = res.json()
                    except Exception:
                        raise Exception("재고 API JSON 파싱 실패")
                    break

                last_error = (
                    f"{res.status_code} {res.text[:300]}"
                )

                # 412 등 일시적인 응답은 재시도
                if attempt < API_RETRY_COUNT:
                    time.sleep(attempt * 3)

            except requests.RequestException as e:
                last_error = str(e)
                if attempt < API_RETRY_COUNT:
                    time.sleep(attempt * 3)

        if data is None:
            raise Exception(
                f"재고 API 조회 실패 (page {page}): {last_error}"
            )

        if isinstance(data, list):
            items = data
            last_page = 1
        else:
            items = data.get("data", []) or []
            meta = data.get("meta", {}) or {}
            last_page = (
                data.get("last_page")
                or meta.get("last_page")
                or 1
            )

        if not items:
            break

        for item in items:

            if not isinstance(item, dict):
                continue

            barcode_data = item.get("barcode") or {}
            variant = item.get("variant") or {}
            variant_barcode = variant.get("barcode") or {}

            barcode = str(
                barcode_data.get("code1", "")
                or barcode_data.get("code", "")
                or item.get("code1", "")
                or variant_barcode.get("code", "")
                or variant_barcode.get("code1", "")
                or ""
            ).strip()

            if not barcode:
                continue

            product = item.get("product") or {}
            product_class = item.get("product_class") or {}
            variant_product = variant.get("productClass") or {}

            product_name = (
                item.get("product_name", "")
                or item.get("name", "")
                or product.get("name", "")
                or product_class.get("name", "")
                or variant_product.get("name", "")
                or item.get("original_name", "")
                or ""
            )

            option_name = (
                item.get("variant_option_name", "")
                or item.get("origin_option_name", "")
                or item.get("option_name", "")
                or item.get("option", "")
                or ""
            )

            stocks = item.get("stocks") or []

            # External /stock 응답이 stock 단위 객체로 바로 오는 경우
            if not stocks and (
                "qty" in item
                or "stock" in item
                or "warehouse" in item
            ):
                stocks = [item]

            for stock in stocks:

                if not isinstance(stock, dict):
                    continue

                warehouse = stock.get("warehouse") or {}
                warehouse_store = warehouse.get("store") or {}

                store_idx = (
                    stock.get("store_idx")
                    or warehouse.get("store_idx")
                    or warehouse_store.get("idx")
                )

                store_name = idx_to_store.get(
                    store_idx,
                    ""
                )

                if not store_name:
                    store_name = norm(
                        stock.get("store_name", "")
                        or warehouse_store.get("name", "")
                        or item.get("store_name", "")
                        or item.get("store", "")
                        or ""
                    )

                if not store_name:
                    continue

                try:
                    qty = int(
                        stock.get("stock", 0)
                        or stock.get("qty", 0)
                        or item.get("stock", 0)
                        or item.get("qty", 0)
                        or 0
                    )
                except (ValueError, TypeError):
                    qty = 0

                all_stock.append({
                    "store": store_name,
                    "barcode": barcode,
                    "name": product_name,
                    "option": option_name,
                    "stock": qty,
                })

        print(
            f"  재고 page {page}/{last_page} "
            f"({len(all_stock)}건)"
        )

        if page >= int(last_page):
            break

        page += 1

    if not all_stock:
        raise Exception("재고 데이터를 가져오지 못했습니다.")

    print(f"✅ 재고 총 {len(all_stock):,}건")

    return all_stock


# =====================================================
# 재고 저장
# =====================================================

def save_stock_to_sheets(stock_data):

    print(
        "📊 재고 데이터를 "
        "Google Sheets에 저장 중..."
    )

    gc = get_google_client()

    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:

        ws = sh.worksheet(
            "재고데이터"
        )

    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(

            title="재고데이터",

            rows=10000,

            cols=6,
        )

    today = get_today().strftime(
        "%Y-%m-%d"
    )

    header = [

        "날짜",
        "매장",
        "바코드",
        "상품명",
        "옵션명",
        "현재고",
    ]

    existing = ws.get_all_values()

    rows_to_keep = []

    if existing:

        for row in existing[1:]:

            if row and row[0] != today:

                rows_to_keep.append(
                    row
                )

    stock_rows = []

    for item in stock_data:

        store = item.get(
            "store",
            ""
        )

        barcode = str(
            item.get(
                "barcode",
                ""
            )
            or ""
        ).strip()

        if (
            not store
            or not barcode
            or store == "ALL"
        ):
            continue

        try:

            qty = int(
                item.get(
                    "stock",
                    0
                )
                or 0
            )

        except (
            ValueError,
            TypeError
        ):

            qty = 0

        stock_rows.append([

            today,

            store,

            barcode,

            item.get(
                "name",
                ""
            ),

            item.get(
                "option",
                ""
            ),

            qty,
        ])

    if not stock_rows:

        raise Exception(
            "저장 가능한 재고 데이터가 없습니다."
        )

    all_rows = [

        header,

        *rows_to_keep,

        *stock_rows,
    ]

    ws.clear()

    ws.update(

        range_name="A1",

        values=all_rows,
    )

    print(
        f"  ✅ 재고 "
        f"{len(stock_rows)}건 저장 완료"
    )


# =====================================================
# 주문 날짜
# =====================================================

def get_order_date(order):

    datetime_text = str(

        order.get(
            "datetime",
            ""
        )
        or ""
    )

    if not datetime_text:
        return None

    try:

        return datetime.strptime(

            datetime_text[:10],

            "%Y-%m-%d"
        ).date()

    except ValueError:

        return None


# =====================================================
# 주문 → 판매 / 반품
# =====================================================

def convert_orders_to_sales(
    orders,
    forced_store_name=""
):

    sales = []

    for order in orders:

        if not isinstance(
            order,
            dict
        ):
            continue

        sale_date = get_order_date(
            order
        )

        if not sale_date:
            continue

        if SALES_START_DATE and sale_date < SALES_START_DATE:
            continue
        order_type = str(

            order.get(
                "order_type",
                ""
            )
            or ""
        ).strip()

        # ---------------------------------------------
        # 판매 / 반품 판별
        # ---------------------------------------------

        is_return = (

            order_type in (
                "반품",
                "환불",
                "return",
                "refund",
                "cancel",
                "취소",
            )

            or "반품" in order_type
            or "환불" in order_type
            or "취소" in order_type
        )

        order_store = order.get("store") or {}
        if not isinstance(order_store, dict):
            order_store = {}

        store_name = norm(
            order.get("store_name", "")
            or order.get("storeName", "")
            or order_store.get("name", "")
            or order_store.get("store_name", "")
            or forced_store_name
            or ""
        )

        receipt = str(

            order.get(
                "receipt",
                ""
            )
            or ""
        ).strip()

        order_idx = str(

            order.get(
                "idx",
                ""
            )
            or ""
        ).strip()

        items = (

            order.get(
                "items"
            )
            or []
        )

        for item_index, item in enumerate(
            items,
            start=1
        ):

            if not isinstance(
                item,
                dict
            ):
                continue

            barcode = str(

                item.get(
                    "barcode",
                    ""
                )

                or item.get(
                    "code1",
                    ""
                )

                or ""
            ).strip()

            if not barcode:
                continue

            try:

                qty = int(

                    item.get(
                        "qty",
                        0
                    )
                    or 0
                )

            except (
                ValueError,
                TypeError
            ):

                qty = 0

            if qty <= 0:
                continue

            # -----------------------------------------
            # 상품순번
            # -----------------------------------------

            item_idx = (

                item.get(
                    "idx"
                )

                or item.get(
                    "item_idx"
                )

                or item.get(
                    "order_item_idx"
                )

                or item_index
            )

            item_idx = str(
                item_idx
            )

            # -----------------------------------------
            # 주문번호
            # -----------------------------------------

            order_number = (

                order.get(
                    "order_no"
                )

                or order.get(
                    "order_number"
                )

                or order.get(
                    "order_idx"
                )

                or order_idx
            )

            order_number = str(
                order_number
            )

            sales.append({

                "date":
                    sale_date.strftime(
                        "%Y-%m-%d"
                    ),

                "store":
                    store_name,

                "barcode":
                    barcode,

                "name":
                    (
                        item.get(
                            "product_name",
                            ""
                        )

                        or item.get(
                            "name",
                            ""
                        )

                        or ""
                    ),

                "option":
                    (
                        item.get(
                            "option_name",
                            ""
                        )

                        or item.get(
                            "option",
                            ""
                        )

                        or ""
                    ),

                "qty":
                    qty,

                "receipt":
                    receipt,

                "order_idx":
                    order_number,

                "item_idx":
                    item_idx,

                "order_type":
                    "반품"
                    if is_return
                    else "판매",

                "datetime":
                    str(
                        order.get(
                            "datetime",
                            ""
                        )
                        or ""
                    ),
            })

    return sales


# =====================================================
# 매출 API + 자동 재탐색 + 매장별 커서
# =====================================================
def get_sales_page(session, page, store_idx=None, start_date=None, end_date=None):
    params = {"page": page, "perPage": PER_PAGE}
    if store_idx is not None:
        params["store_idx"] = store_idx
    if start_date:
        params["startDate"] = start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else str(start_date)
    if end_date:
        params["endDate"] = end_date.strftime("%Y-%m-%d") if hasattr(end_date, "strftime") else str(end_date)

    last_error = ""
    for attempt in range(1, API_RETRY_COUNT + 1):
        try:
            res = session.get(f"{BASE_URL}/order", params=params, timeout=90)
            print(f"  📡 매출 API store_idx={store_idx} page={page} 응답: {res.status_code}")
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    return data, 1
                orders = data.get("data", []) or []
                meta = data.get("meta", {}) or {}
                last_page = int(data.get("last_page") or meta.get("last_page") or 1)
                return orders, last_page

            last_error = f"{res.status_code} {res.text[:300]}"

            if res.status_code == 412:
                print(f"  ⚠️ 412 Need JS Update ({attempt}/{API_RETRY_COUNT})")
                version = detect_js_version(session)
                apply_js_version(session, version)
                time.sleep(1)
                continue

            if res.status_code == 404:
                # 간헐적인 404는 바로 다음 재시도로 복구되는 경우가 있어 짧게 재시도
                if attempt < API_RETRY_COUNT:
                    time.sleep(1 + attempt)
                    continue

            if attempt < API_RETRY_COUNT:
                time.sleep(attempt * 2)
        except requests.RequestException as e:
            last_error = str(e)
            if attempt < API_RETRY_COUNT:
                time.sleep(attempt * 2)
        except Exception as e:
            last_error = str(e)
            if attempt < API_RETRY_COUNT:
                time.sleep(attempt * 2)

    raise Exception(f"매출 API store_idx={store_idx} page={page} 조회 실패: {last_error}")


def prepare_cursor_sheet():
    gc = get_google_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(CURSOR_SHEET)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=CURSOR_SHEET, rows=100, cols=5)
        ws.update(range_name="A1:E1", values=[CURSOR_HEADER])
    return ws


def load_cursors():
    ws = prepare_cursor_sheet()
    rows = ws.get_all_values()
    result = {}
    for row in rows[1:]:
        if len(row) >= 3 and row[0]:
            try:
                result[norm(row[0])] = int(row[2] or 0)
            except Exception:
                result[norm(row[0])] = 0
    return result


def save_cursor(store_name, store_idx, last_page, status="RUNNING"):
    ws = prepare_cursor_sheet()
    rows = ws.get_all_values()
    target = None
    for i, row in enumerate(rows[1:], start=2):
        if row and norm(row[0]) == norm(store_name):
            target = i
            break
    values = [[store_name, str(store_idx), str(last_page), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status]]
    if target is None:
        ws.append_rows(values, value_input_option="RAW")
    else:
        ws.update(range_name=f"A{target}:E{target}", values=values)


def append_sales_to_sheet(ws, sales, seen_keys):
    rows = []
    new_sales = []
    for sale in sales:
        key = make_sale_key(sale)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append([
            sale.get("date", ""), sale.get("store", ""), sale.get("barcode", ""),
            sale.get("name", ""), sale.get("option", ""), sale.get("qty", 0),
            sale.get("receipt", ""), sale.get("order_idx", ""), sale.get("item_idx", ""),
            sale.get("order_type", "판매"),
        ])
        new_sales.append(sale)
    for i in range(0, len(rows), SHEET_CHUNK_SIZE):
        ws.append_rows(rows[i:i+SHEET_CHUNK_SIZE], value_input_option="RAW")
    return len(rows), new_sales


def get_sales(session, store_list, existing_keys):
    print("💰 전체 매출 조회 시작...")
    ws = prepare_sales_sheet()
    seen_keys = set(existing_keys)
    all_sales = []
    cursors = load_cursors()

    for store_name, store_idx in store_list.items():
        print("\n================================")
        print(f"🏪 [{store_name}] 매출 조회 시작")
        print(f"  store_idx={store_idx}")

        # 첫 호출로 전체 페이지 수 확인
        probe_orders, last_page = get_sales_page(session, 1, store_idx=store_idx)
        cursor = 0 if FULL_RESCAN else cursors.get(norm(store_name), 0)
        start_page = max(1, cursor + 1)
        if cursor >= last_page:
            # 매일 최신 페이지를 다시 확인해 신규 주문/반품을 놓치지 않음
            start_page = max(1, last_page - 14)
        print(f"  📄 전체 페이지: {last_page:,}")
        print(f"  ▶️ 시작 페이지: {start_page:,} (이전 커서 {cursor:,})")

        page = start_page
        store_new = 0
        while page <= last_page:
            orders, actual_last_page = get_sales_page(session, page, store_idx=store_idx)
            last_page = max(last_page, actual_last_page)
            if not orders:
                save_cursor(store_name, store_idx, page, "DONE")
                break

            sales = convert_orders_to_sales(orders, forced_store_name=store_name)
            new_count, new_sales = append_sales_to_sheet(ws, sales, seen_keys)
            all_sales.extend(new_sales)
            store_new += new_count

            dates = [get_order_date(o) for o in orders if get_order_date(o)]
            date_info = ""
            if dates:
                date_info = f" / {min(dates)} ~ {max(dates)}"
            print(f"  📄 page {page}/{last_page} 주문 {len(orders)}건 신규 {new_count}건{date_info}")

            # 페이지 저장 직후 커서 기록: GitHub Actions가 중단돼도 다음 실행에서 이어감
            save_cursor(store_name, store_idx, page, "RUNNING")
            if page % 10 == 0:
                print(f"  💾 중간 커서 저장: {store_name} page {page}")
            page += 1

        save_cursor(store_name, store_idx, min(page-1, last_page), "DONE")
        print(f"  ✅ [{store_name}] 완료 / 신규 {store_new:,}건")

    print("\n================================")
    print(f"📊 전체 신규 저장 데이터 {len(all_sales):,}건")
    return all_sales


# =====================================================
# 기존 호출 호환용
# 이미 get_sales 단계에서 페이지별 저장하므로
# 여기서는 추가 저장을 하지 않는다.
# =====================================================

def save_sales_to_sheets(sales_data):
    print(
        "  ℹ️ 매출은 API 페이지 조회 시 "
        "Google Sheets에 즉시 저장되었습니다."
    )

# =====================================================
# 최근 7일 판매속도
# =====================================================

def calculate_7day_average():

    print(
        "📈 최근 7일 "
        "일평균 판매량 계산 중..."
    )

    gc = get_google_client()

    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:

        sales_ws = sh.worksheet(
            "매출데이터"
        )

    except gspread.WorksheetNotFound:

        print(
            "⚠️ 매출데이터 시트가 없습니다."
        )

        return

    try:

        ws = sh.worksheet(
            "판매속도"
        )

    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(

            title="판매속도",

            rows=10000,

            cols=10,
        )

    records = sales_ws.get_all_values()

    if len(records) <= 1:

        print(
            "⚠️ 판매내역이 없습니다."
        )

        return

    header = records[0]

    required = [

        "날짜",
        "매장",
        "바코드",
        "상품명",
        "옵션명",
        "판매수량",
        "판매구분",
    ]

    missing = [

        x

        for x in required

        if x not in header
    ]

    if missing:

        print(
            f"⚠️ 판매속도 계산에 필요한 "
            f"헤더 누락: {missing}"
        )

        return

    date_idx = header.index(
        "날짜"
    )

    store_idx = header.index(
        "매장"
    )

    barcode_idx = header.index(
        "바코드"
    )

    name_idx = header.index(
        "상품명"
    )

    option_idx = header.index(
        "옵션명"
    )

    qty_idx = header.index(
        "판매수량"
    )

    type_idx = header.index(
        "판매구분"
    )

    today = get_today()

    start_date = (

        today

        - timedelta(
            days=SALES_AVERAGE_DAYS - 1
        )
    )

    summary = {}

    for row in records[1:]:

        if len(row) <= max(

            date_idx,

            store_idx,

            barcode_idx,

            name_idx,

            option_idx,

            qty_idx,

            type_idx
        ):

            continue

        date_text = row[
            date_idx
        ]

        try:

            sale_date = datetime.strptime(

                date_text,

                "%Y-%m-%d"
            ).date()

        except ValueError:

            continue

        if not (

            start_date

            <= sale_date

            <= today
        ):

            continue

        store = row[
            store_idx
        ]

        barcode = row[
            barcode_idx
        ]

        if not store or not barcode:
            continue

        try:

            qty = int(

                row[
                    qty_idx
                ]
                or 0
            )

        except (
            ValueError,
            TypeError
        ):

            qty = 0

        sale_type = row[
            type_idx
        ]

        key = (

            store,

            barcode
        )

        if key not in summary:

            summary[key] = {

                "store":
                    store,

                "barcode":
                    barcode,

                "name":
                    row[name_idx],

                "option":
                    row[option_idx],

                "total_qty":
                    0,
            }

        # ---------------------------------------------
        # 반품은 판매량에서 차감
        # ---------------------------------------------

        if sale_type == "반품":

            summary[key][
                "total_qty"
            ] -= qty

        else:

            summary[key][
                "total_qty"
            ] += qty

    output = [

        [

            "기준일",

            "조회기간",

            "매장",

            "바코드",

            "상품명",

            "옵션명",

            "7일 순판매수량",

            "일평균 판매수량",

            "계산일수",
        ]
    ]

    for item in sorted(

        summary.values(),

        key=lambda x: (

            x["store"],

            x["barcode"]
        )
    ):

        total_qty = int(

            item[
                "total_qty"
            ]
        )

        average = (

            total_qty

            / SALES_AVERAGE_DAYS
        )

        output.append([

            today.strftime(
                "%Y-%m-%d"
            ),

            (

                start_date.strftime(
                    "%Y-%m-%d"
                )

                + " ~ "

                + today.strftime(
                    "%Y-%m-%d"
                )
            ),

            item["store"],

            item["barcode"],

            item["name"],

            item["option"],

            total_qty,

            round(
                average,
                2
            ),

            SALES_AVERAGE_DAYS,
        ])

    ws.clear()

    ws.update(

        range_name="A1",

        values=output,
    )

    print(

        f"✅ 판매속도 "
        f"{len(output) - 1:,}개 상품 저장"
    )

    print(

        f"  📅 계산기간: "
        f"{start_date} ~ {today}"
    )


# =====================================================
# 최종 요약
# =====================================================

def print_sales_summary(
    sales_data
):

    sale_count = 0
    sale_qty = 0

    return_count = 0
    return_qty = 0

    for sale in sales_data:

        qty = int(
            sale.get(
                "qty",
                0
            )
            or 0
        )

        if sale.get(
            "order_type"
        ) == "반품":

            return_count += 1
            return_qty += qty

        else:

            sale_count += 1
            sale_qty += qty

    print("")
    print(
        "----------------------------------------"
    )

    print(
        "📊 이번 실행 매출 요약"
    )

    print(
        f"  판매 건수: "
        f"{sale_count:,}건"
    )

    print(
        f"  판매 수량: "
        f"{sale_qty:,}개"
    )

    print(
        f"  반품 건수: "
        f"{return_count:,}건"
    )

    print(
        f"  반품 수량: "
        f"{return_qty:,}개"
    )

    print(
        f"  순판매수량: "
        f"{sale_qty - return_qty:,}개"
    )

    print(
        "----------------------------------------"
    )


# =====================================================
# MAIN
# =====================================================

def main():

    print(
        "========================================"
    )

    print(
        "🚀 헤트라스 셀메이트 동기화 시작"
    )


    try:

        # =================================================
        # 로그인
        # =================================================

        if check_daily_sync():

            print(
                "오늘 작업은 이미 완료되었습니다."
            )

            return


        session = login()


        # =================================================
        # 매장
        # =================================================

        store_list = get_store_list(
            session
        )

        # =================================================
        # 재고
        # =================================================

        stock_data = get_all_stock(

            session,

            store_list
        )

        save_stock_to_sheets(
            stock_data
        )

        print(
            "========================================"
        )

        print(
            "📦 재고 동기화 완료!"
        )

        print(
            "========================================"
        )

        sales_success = False

        # =================================================
        # 매출
        # =================================================

        try:

            gc = get_google_client()

            sh = gc.open_by_key(
                SPREADSHEET_ID
            )

            try:

                sales_ws = sh.worksheet(
                    "매출데이터"
                )

                existing_keys = (
                    get_existing_sales_state(
                        sales_ws
                    )
                )

            except gspread.WorksheetNotFound:

                print(
                    "  기존 매출 데이터: 0건"
                )

                existing_keys = set()

            # ---------------------------------------------
            # 전체 매장 조회
            # ---------------------------------------------

            sales_data = get_sales(

                session,

                store_list,

                existing_keys
            )

            print_sales_summary(
                sales_data
            )

            print(
                f"✅ 이번 실행 신규 "
                f"판매/반품 내역: "
                f"{len(sales_data):,}건"
            )

            # ---------------------------------------------
            # 저장
            # ---------------------------------------------

            save_sales_to_sheets(
                sales_data
            )

            # ---------------------------------------------
            # 판매속도
            # ---------------------------------------------

            calculate_7day_average()

            print(
                "💰 매출 동기화 완료!"
            )
            sales_success = True

        except Exception as e:

            print(
                f"⚠️ 매출 동기화 실패: {e}"
            )

            print(
                "ℹ️ 매출 오류와 관계없이 "
                "재고 데이터는 정상 저장되었습니다."
            )

        print(
            "========================================"
        )


        if sales_success:
            save_daily_sync()
            print(
                "🎉 동기화 완료!"
            )
        else:
            print(
                "⚠️ 매출 동기화가 완료되지 않아 "
                "오늘 완료 로그를 기록하지 않습니다."
            )


        print(
            "========================================"
        )

    except Exception as e:

        print(
            "========================================"
        )

        print(
            f"❌ 동기화 실패: {e}"
        )

        print(
            "========================================"
        )

        raise


# =====================================================
# 실행
# =====================================================

if __name__ == "__main__":

    main()
