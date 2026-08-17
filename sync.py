import os
import json
import urllib.parse
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
WEB_API_BASE_URL = "https://sellmatepos.com/webApi"
SELLMATE_SITE_URL = "https://sellmatepos.com"

SELLMATE_JS_VERSION = os.environ.get(
    "SELLMATE_JS_VERSION",
    "2.8.4"
)

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
# Sellmate 프론트엔드 버전 자동 탐색 / API 공통 처리
# =====================================================


def _version_tuple(value):
    try:
        parts = str(value).strip().split(".")
        if len(parts) < 2:
            return None
        return tuple(int(x) for x in parts[:3])
    except Exception:
        return None


def discover_sellmate_js_version(session, silent=False):
    global SELLMATE_JS_VERSION

    candidates = []
    urls = [
        f"{SELLMATE_SITE_URL}/product/variant/stock",
        f"{SELLMATE_SITE_URL}/",
    ]

    import re

    for url in urls:
        try:
            res = session.get(
                url,
                timeout=30,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                },
            )
            if res.status_code != 200:
                continue

            html = res.text or ""

            patterns = [
                r"(?:css|js)/[^\"']+\?[^\"']*?[?&]v=(\d+\.\d+(?:\.\d+)?)",
                r"[?&]v=(\d+\.\d+(?:\.\d+)?)",
                r"sellmate-pos-js-version[^0-9]*(\d+\.\d+(?:\.\d+)?)",
            ]

            for pattern in patterns:
                candidates.extend(re.findall(pattern, html, flags=re.I))

            if candidates:
                break
        except requests.RequestException:
            continue

    valid = []
    for value in candidates:
        vt = _version_tuple(value)
        if vt and vt[0] in (2, 3, 4):
            valid.append((vt, value))

    if valid:
        valid.sort(key=lambda x: x[0], reverse=True)
        SELLMATE_JS_VERSION = valid[0][1]
        if not silent:
            print(f"  🔎 Sellmate 현재 JS 버전 자동 탐색: {SELLMATE_JS_VERSION}")
    elif not silent:
        print(f"  ⚠️ JS 버전 자동 탐색 실패 → fallback {SELLMATE_JS_VERSION}")

    return SELLMATE_JS_VERSION


def apply_sellmate_headers(session):
    session.headers.update({
        "sellmate-pos-js-version": SELLMATE_JS_VERSION,
        "x-api-version": "2.2",
        "x-pos-domain": SELLMATE_DOMAIN,
        "pos-locale": "kr",
    })


def sellmate_get(session, path, params=None, label="API", timeout=60):
    """/webApi 우선 → 404 시 /json fallback → 412 시 JS 버전 자동 재탐색."""
    params = params or {}
    last_error = None
    bases = [WEB_API_BASE_URL, BASE_URL]

    for attempt in range(1, API_RETRY_COUNT + 1):
        refresh_needed = False

        for base in bases:
            url = f"{base}/{path.lstrip('/')}"
            try:
                res = session.get(url, params=params, timeout=timeout)
                print(f"  📡 {label} 응답: {res.status_code}")

                if res.status_code == 200:
                    return res

                if res.status_code == 412:
                    last_error = f"412 {res.text[:500]}"
                    print(f"  ⚠️ 412 Need JS Update ({attempt}/{API_RETRY_COUNT})")
                    refresh_needed = True
                    break

                if res.status_code == 404:
                    last_error = f"404 {res.text[:300]}"
                    continue

                last_error = f"{res.status_code} {res.text[:500]}"

            except requests.RequestException as e:
                last_error = str(e)
                print(f"  ⚠️ {label} 요청 오류 ({attempt}/{API_RETRY_COUNT}): {e}")

        if refresh_needed:
            old_version = SELLMATE_JS_VERSION
            discover_sellmate_js_version(session, silent=False)
            apply_sellmate_headers(session)
            print(f"  🔄 JS 버전 재적용: {old_version} → {SELLMATE_JS_VERSION}")

        if attempt < API_RETRY_COUNT:
            time.sleep(attempt * 2)

    raise Exception(f"{label} 조회 실패: {last_error}")


# =====================================================
# 로그인
# =====================================================

def login():

    print("🔐 셀메이트 로그인 중...")

    session = requests.Session()

    discover_sellmate_js_version(session, silent=False)

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
    })

    apply_sellmate_headers(session)

    # 인증된 실제 화면에서도 한 번 더 확인
    discover_sellmate_js_version(session, silent=False)
    apply_sellmate_headers(session)

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

    idx_to_store = {value: key for key, value in store_list.items()}
    all_stock = []
    page = 1

    while True:
        try:
            res = sellmate_get(
                session,
                "product/variant/stock",
                params={"page": page, "perPage": 15},
                label=f"재고 API page={page}",
                timeout=60,
            )
        except Exception as e:
            raise Exception(f"재고 API 조회 실패 (page {page}): {e}")

        try:
            data = res.json()
        except Exception:
            raise Exception(f"재고 API JSON 파싱 실패 (page {page})")

        if isinstance(data, list):
            items, last_page = data, 1
        else:
            items = data.get("data", [])
            meta = data.get("meta", {}) or {}
            last_page = data.get("last_page") or meta.get("last_page") or 1

        if not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue

            barcode_data = item.get("barcode") or {}
            barcode = str(
                barcode_data.get("code1", "")
                or item.get("code1", "")
                or ""
            ).strip()
            if not barcode:
                continue

            product = item.get("product") or {}
            product_class = item.get("product_class") or {}
            product_name = (
                product.get("name", "")
                or product_class.get("name", "")
                or item.get("original_name", "")
                or ""
            )
            option_name = (
                item.get("origin_option_name", "")
                or item.get("option_name", "")
                or ""
            )

            stocks = item.get("stocks") or []
            if not stocks and any(k in item for k in ("stock", "total_stock", "store_idx")):
                stocks = [item]

            for stock in stocks:
                if not isinstance(stock, dict):
                    continue

                warehouse = stock.get("warehouse") or {}
                store_idx = stock.get("store_idx") or warehouse.get("store_idx")
                store_name = idx_to_store.get(store_idx, "")

                if not store_name:
                    warehouse_store = warehouse.get("store") or {}
                    store_name = norm(
                        stock.get("store_name", "")
                        or warehouse_store.get("name", "")
                        or ""
                    )

                if not store_name or store_name == "ALL":
                    continue

                qty = stock.get("stock")
                if qty is None:
                    qty = stock.get("total_stock")
                try:
                    qty = int(qty or 0)
                except (ValueError, TypeError):
                    qty = 0

                all_stock.append({
                    "store": store_name,
                    "barcode": barcode,
                    "name": product_name,
                    "option": option_name,
                    "stock": qty,
                })

        print(f"  📄 재고 page {page}/{last_page} ({len(all_stock):,}건)")

        if page >= int(last_page):
            break
        page += 1

    if not all_stock:
        raise Exception("저장 가능한 재고 데이터가 없습니다.")

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

        store_name = norm(

            order.get(
                "store_name",
                ""
            )

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
# 매출 API
#
# 핵심:
# store_idx를 명시해서 매장별로 조회
# =====================================================

def get_sales_page(
    session,
    page,
    store_idx
):

    params = {
        "page": page,
        "perPage": PER_PAGE,
        "store_idx": store_idx,
    }

    res = sellmate_get(
        session,
        "order",
        params=params,
        label=f"매출 API store_idx={store_idx} page={page}",
        timeout=90,
    )

    try:
        data = res.json()
    except Exception:
        raise Exception("매출 API JSON 파싱 실패")

    if isinstance(data, list):
        return data, 1

    orders = data.get("data", [])
    meta = data.get("meta", {}) or {}
    last_page = data.get("last_page") or meta.get("last_page") or 1
    return orders, int(last_page)


# =====================================================
# 매출 기존 데이터 확인
# =====================================================


def get_existing_sales_state(ws):

    print(
        "🔎 기존 매출 데이터 확인 중..."
    )

    records = ws.get_all_values()

    if not records:
        print("  기존 매출 데이터: 0건")
        return set()

    header = records[0]

    # 새 구조의 필수 컬럼
    required = [
        "날짜",
        "매장",
        "바코드",
        "판매수량",
        "영수증번호",
        "주문번호",
        "상품순번",
    ]

    missing = [
        field
        for field in required
        if field not in header
    ]

    if missing:
        print(
            "  ⚠️ 기존 매출 시트에서 "
            f"필수 헤더 누락: {missing}"
        )
        return set()

    indexes = {
        field: header.index(field)
        for field in required
    }

    # 판매구분이 없던 구버전 데이터는
    # 기존 행을 삭제하지 않고 '판매'로 간주한다.
    type_idx = (
        header.index("판매구분")
        if "판매구분" in header
        else None
    )

    existing_keys = set()

    for row in records[1:]:

        try:
            key = (
                str(row[indexes["날짜"]]).strip(),
                str(row[indexes["매장"]]).strip(),
                str(row[indexes["바코드"]]).strip(),
                str(row[indexes["주문번호"]]).strip(),
                str(row[indexes["상품순번"]]).strip(),
                (
                    str(row[type_idx]).strip()
                    if type_idx is not None
                    and type_idx < len(row)
                    and row[type_idx]
                    else "판매"
                ),
            )

            existing_keys.add(key)

        except (IndexError, KeyError):
            continue

    print(
        f"  기존 매출 데이터: "
        f"{len(existing_keys):,}건"
    )

    return existing_keys



# =====================================================
# 매출 동기화 상태
#
# 매번 2,600페이지 이상을 다시 조회하지 않기 위해
# 매장별 마지막 매출 페이지를 Google Sheets에 기록한다.
# =====================================================

SALES_STATE_SHEET = "매출동기화상태"

# 첫 실행에서 최근 영역을 찾기 위한 기본 탐색 범위
INITIAL_LOOKBACK_PAGES = 320

# 최초 시작점을 찾지 못하면 뒤로 확장
INITIAL_EXPAND_STEP = 150

# 다음 실행에서는 이전 커서보다 약간 앞에서 시작
SALES_CURSOR_LOOKBACK = 15

# API의 비정상적인 페이지 구간 때문에
# 오래된 페이지가 연속으로 나와도 이 정도는 확인한다.
SALES_STALE_PAGE_LIMIT = 35

# 초기 데이터가 아직 충분히 쌓이지 않은 경우에는 저장 상태가 있더라도
# 1페이지부터 전체 백필하여 누락된 과거 매출을 채웁니다.
BACKFILL_EXISTING_KEY_THRESHOLD = 1000


def get_sales_state_sheet():

    gc = get_google_client()

    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:
        ws = sh.worksheet(
            SALES_STATE_SHEET
        )

    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(
            title=SALES_STATE_SHEET,
            rows=100,
            cols=5
        )

        ws.update(
            "A1:E1",
            [[
                "매장",
                "store_idx",
                "마지막페이지",
                "마지막확인일",
                "업데이트시간",
            ]]
        )

    return ws


def load_sales_sync_state():

    ws = get_sales_state_sheet()

    records = ws.get_all_values()

    state = {}

    for row in records[1:]:

        if len(row) < 3:
            continue

        store = str(
            row[0]
        ).strip()

        if not store:
            continue

        try:
            store_idx = int(
                row[1]
            )
        except (ValueError, TypeError):
            store_idx = 0

        try:
            last_page = int(
                row[2]
            )
        except (ValueError, TypeError):
            last_page = 0

        state[store] = {
            "store_idx": store_idx,
            "last_page": last_page,
            "last_date": (
                row[3].strip()
                if len(row) > 3
                else ""
            ),
        }

    return ws, state


def save_sales_sync_state(
    ws,
    store_name,
    store_idx,
    last_page,
    last_date
):

    records = ws.get_all_values()

    target_row = None

    for row_number, row in enumerate(
        records[1:],
        start=2
    ):

        if (
            row
            and str(row[0]).strip()
            == str(store_name).strip()
        ):

            target_row = row_number
            break

    values = [[
        store_name,
        store_idx,
        last_page,
        last_date or "",
        datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    ]]

    if target_row is None:

        ws.append_rows(
            values,
            value_input_option="RAW"
        )

    else:

        ws.update(
        range_name=f"A{target_row}:E{target_row}",
        values=values
        )


def get_page_date_range(orders):

    dates = []

    for order in orders:

        d = get_order_date(
            order
        )

        if d:
            dates.append(d)

    if not dates:
        return None, None

    return min(dates), max(dates)


def make_sale_key(sale):

    return "|".join([

        str(sale.get("date", "")).strip(),

        str(sale.get("store", "")).strip(),

        str(sale.get("barcode", "")).strip(),

        str(sale.get("receipt", "")).strip(),

        str(sale.get("order_idx", "")).strip(),

        str(sale.get("item_idx", "")).strip(),

        str(sale.get("order_type", "판매")).strip(),

    ])


def find_sales_start_page(
    session,
    store_idx,
    last_page
):

    print(
        "  🔍 최초 7월 매출 "
        "시작 페이지 탐색..."
    )

    # 최근 영역에서 50페이지 간격으로 몇 곳만 먼저 확인한다.
    # 2,600페이지를 처음부터 읽는 방식은 사용하지 않는다.
    search_start = max(
        1,
        last_page - INITIAL_LOOKBACK_PAGES
    )

    step = 50
    probe_pages = list(
        range(
            search_start,
            last_page + 1,
            step
        )
    )

    if last_page not in probe_pages:
        probe_pages.append(last_page)

    candidate = None

    for page in probe_pages:

        orders, _ = get_sales_page(
            session,
            page,
            store_idx
        )

        if not orders:
            continue

        oldest, newest = (
            get_page_date_range(
                orders
            )
        )

        print(
            f"  🔎 probe page {page}: "
            f"{oldest} ~ {newest}"
        )

        if (
            newest is not None
            and newest >= SALES_START_DATE
        ):

            candidate = page
            break

    # probe에서 못 찾으면 앞쪽으로 한 번 확장
    if candidate is None:

        expanded_start = max(
            1,
            search_start
            - INITIAL_EXPAND_STEP
        )

        for page in range(
            expanded_start,
            search_start
        ):

            orders, _ = get_sales_page(
                session,
                page,
                store_idx
            )

            if not orders:
                continue

            oldest, newest = (
                get_page_date_range(
                    orders
                )
            )

            if (
                newest is not None
                and newest >= SALES_START_DATE
            ):

                candidate = page
                break

    if candidate is None:

        raise Exception(
            f"store_idx={store_idx}의 "
            f"{SALES_START_DATE} 이후 "
            "매출 시작 페이지를 찾지 못했습니다."
        )

    # 후보보다 앞쪽 최대 60페이지만 역방향으로 확인한다.
    # 날짜가 섞여 있는 API라 전체 이진탐색은 사용하지 않는다.
    back_start = max(
        1,
        candidate - 60
    )

    first_valid = candidate

    for page in range(
        back_start,
        candidate + 1
    ):

        orders, _ = get_sales_page(
            session,
            page,
            store_idx
        )

        if not orders:
            continue

        oldest, newest = (
            get_page_date_range(
                orders
            )
        )

        if (
            newest is not None
            and newest >= SALES_START_DATE
        ):

            first_valid = page
            break

    result = max(
        1,
        first_valid - 1
    )

    print(
        f"  🎯 7월 데이터 시작 근처: "
        f"page {result}"
    )

    return result


# =====================================================
# 매출 전체 조회
#
# ★ 핵심 변경
# 매장별로 API를 각각 조회
# =====================================================


def append_sales_chunk_to_sheets(sales_data):
    """조회 중간에 즉시 Google Sheets에 append하여 작업 중단 시 유실을 방지합니다."""
    if not sales_data:
        return

    gc = get_google_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    try:
        ws = sh.worksheet("매출데이터")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="매출데이터", rows=100000, cols=10)

    header = [
        "날짜", "매장", "바코드", "상품명", "옵션명",
        "판매수량", "영수증번호", "주문번호", "상품순번", "판매구분",
    ]

    current_header = ws.row_values(1)
    if not current_header:
        ws.update("A1", [header])
    elif current_header != header:
        # 구버전 헤더는 최초 한 번만 마이그레이션
        save_sales_to_sheets([])

    rows = []
    for sale in sales_data:
        rows.append([
            sale.get("date", ""),
            sale.get("store", ""),
            sale.get("barcode", ""),
            sale.get("name", ""),
            sale.get("option", ""),
            sale.get("qty", 0),
            sale.get("receipt", ""),
            sale.get("order_idx", ""),
            sale.get("item_idx", ""),
            sale.get("order_type", "판매"),
        ])

    if not rows:
        return

    # 시트가 부족하면 append 실패 시 자동 확장 후 한 번 더 시도
    try:
        ws.append_rows(rows, value_input_option="RAW")
    except Exception as first_error:
        new_size = max(ws.row_count * 2, ws.row_count + len(rows) + 5000)
        print(f"  📈 매출 시트 행 확장 {ws.row_count:,} → {new_size:,}")
        ws.resize(rows=new_size)
        try:
            ws.append_rows(rows, value_input_option="RAW")
        except Exception:
            raise first_error

    print(f"  💾 중간 저장 완료: {len(rows):,}건")


def get_sales(
    session,
    store_list,
    existing_keys
):

    print("💰 전체 매출 조회 시작...")

    run_sale_count = 0
    run_sale_qty = 0
    run_return_count = 0
    run_return_qty = 0

    seen_keys = set(existing_keys)
    state_ws, state = load_sales_sync_state()
    initial_backfill = len(existing_keys) < BACKFILL_EXISTING_KEY_THRESHOLD

    for store_name, store_idx in store_list.items():

        print("")
        print("================================")
        print(f"🏪 [{store_name}] 조회 시작")

        store_state = state.get(store_name, {})
        saved_last_page = int(store_state.get("last_page", 0) or 0)

        if initial_backfill or saved_last_page <= 0:
            start_page = 1
            mode_text = "전체 백필"
        else:
            start_page = max(1, saved_last_page - SALES_CURSOR_LOOKBACK)
            mode_text = f"증분 조회 (page {start_page}~)"

        print(f"  🔄 {mode_text}")

        page = start_page
        last_page = 1
        store_buffer = []
        last_orders = []
        stale_pages = 0

        while True:
            orders, last_page = get_sales_page(
                session,
                page,
                store_idx,
            )
            last_orders = orders

            if not orders:
                break

            sales = convert_orders_to_sales(
                orders,
                forced_store_name=store_name,
            )

            new_count = 0
            for sale in sales:
                key = make_sale_key(sale)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                store_buffer.append(sale)
                new_count += 1

                qty = int(sale.get("qty", 0) or 0)
                if sale.get("order_type") == "반품":
                    run_return_count += 1
                    run_return_qty += qty
                else:
                    run_sale_count += 1
                    run_sale_qty += qty

            print(
                f"  📄 page {page}/{last_page} "
                f"주문 {len(orders)}건 신규 {new_count}건"
            )

            if not initial_backfill:
                oldest, newest = get_page_date_range(orders)
                if newest is None or newest < get_today() - timedelta(days=3):
                    stale_pages += 1
                else:
                    stale_pages = 0
                if stale_pages >= SALES_STALE_PAGE_LIMIT:
                    print(
                        f"  🛑 오래된 페이지 {SALES_STALE_PAGE_LIMIT}개 연속 → 증분 조회 종료"
                    )
                    break

            # 1,000건 단위 중간 저장
            if len(store_buffer) >= SHEET_CHUNK_SIZE:
                append_sales_chunk_to_sheets(store_buffer)
                store_buffer.clear()

            # 5페이지마다 상태 저장. 데이터 먼저 저장하고 상태를 기록합니다.
            if page % 5 == 0:
                if store_buffer:
                    append_sales_chunk_to_sheets(store_buffer)
                    store_buffer.clear()

                _, newest = get_page_date_range(orders)
                save_sales_sync_state(
                    state_ws,
                    store_name,
                    store_idx,
                    page,
                    newest.strftime("%Y-%m-%d") if newest else "",
                )

            if page >= int(last_page):
                break
            page += 1

        if store_buffer:
            append_sales_chunk_to_sheets(store_buffer)
            store_buffer.clear()

        _, newest = get_page_date_range(last_orders) if last_orders else (None, None)
        save_sales_sync_state(
            state_ws,
            store_name,
            store_idx,
            page,
            newest.strftime("%Y-%m-%d") if newest else "",
        )

    print("")
    print("================================")
    print(
        f"📊 이번 실행 신규 데이터 "
        f"{run_sale_count + run_return_count:,}건"
    )

    return {
        "sale_count": run_sale_count,
        "sale_qty": run_sale_qty,
        "return_count": run_return_count,
        "return_qty": run_return_qty,
        "total_count": run_sale_count + run_return_count,
    }


# =====================================================
# 매출 저장
# =====================================================


def save_sales_to_sheets(
    sales_data
):

    print(
        "📊 판매내역을 "
        "Google Sheets에 저장 중..."
    )

    gc = get_google_client()

    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:

        ws = sh.worksheet(
            "매출데이터"
        )

    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(
            title="매출데이터",
            rows=100000,
            cols=10
        )

    header = [
        "날짜",
        "매장",
        "바코드",
        "상품명",
        "옵션명",
        "판매수량",
        "영수증번호",
        "주문번호",
        "상품순번",
        "판매구분",
    ]

    existing = ws.get_all_values()

    print("=" * 50)
    print(f"기존 읽은 행수 : {len(existing):,}")

    if existing:
        print(f"헤더 : {existing[0]}")

    print(existing[:5])

    print("=" * 50)

    
    # -------------------------------------------------
    # 기존 데이터 보존
    #
    # 이전 버전에서 '판매구분'이 없더라도
    # 기존 매출을 삭제하지 않는다.
    # -------------------------------------------------

    if not existing:

        ws.update(
            "A1",
            [header]
        )

        existing = [header]

    else:

        old_header = existing[0]

        if old_header != header:

            print(
                "  ⚠️ 기존 매출 시트 "
                "헤더가 새 구조와 다릅니다."
            )

            old_index = {
                name: idx
                for idx, name
                in enumerate(old_header)
            }

            missing = [
                x
                for x in header
                if x not in old_index
            ]

            if missing:

                print(
                    f"  ℹ️ 누락 헤더: "
                    f"{missing}"
                )

            migrated_rows = []

            for row in existing[1:]:

                def old_value(
                    field,
                    default=""
                ):

                    idx = old_index.get(
                        field
                    )

                    if (
                        idx is None
                        or idx >= len(row)
                    ):
                        return default

                    return row[idx]

                migrated_rows.append([
                    old_value("날짜"),
                    old_value("매장"),
                    old_value("바코드"),
                    old_value("상품명"),
                    old_value("옵션명"),
                    old_value("판매수량", 0),
                    old_value("영수증번호"),
                    old_value("주문번호"),
                    old_value("상품순번"),
                    old_value(
                        "판매구분",
                        "판매"
                    ) or "판매",
                ])

            print(
                f"  🔄 기존 "
                f"{len(migrated_rows):,}건을 "
                "새 구조로 보존 변환합니다."
            )

            ws.clear()

            ws.update(
                "A1",
                [header]
            )

            for i in range(
                0,
                len(migrated_rows),
                SHEET_CHUNK_SIZE
            ):

                chunk = migrated_rows[
                    i:i + SHEET_CHUNK_SIZE
                ]

                start = (
                    2 + i
                )

                end = (
                    start
                    + len(chunk)
                    - 1
                )

                ws.update(
                    f"A{start}:J{end}",
                    chunk
                )

            existing = [
                header,
                *migrated_rows
            ]
    # -------------------------------------------------
    # 기존 중복키
    # -------------------------------------------------

    existing_keys = set()

    for row in existing[1:]:

        if len(row) < 10:
            continue

        key = (
            str(row[0]).strip(),
            str(row[1]).strip(),
            str(row[2]).strip(),
            str(row[7]).strip(),
            str(row[8]).strip(),
            str(row[9]).strip()
            if row[9]
            else "판매",
        )

        existing_keys.add(key)


    print(
        f"기존 KEY 개수 : {len(existing_keys):,}"
    )
    
    # -------------------------------------------------
    # 신규 데이터 필터링
    # -------------------------------------------------

    rows = []


    for sale in sales_data:

        key = make_sale_key(
            sale
        )


        if key in existing_keys:
            continue


        rows.append([
            sale.get("date", ""),
            sale.get("store", ""),
            sale.get("barcode", ""),
            sale.get("name", ""),
            sale.get("option", ""),
            sale.get("qty", 0),
            sale.get("receipt", ""),
            sale.get("order_idx", ""),
            sale.get("item_idx", ""),
            sale.get("order_type", "판매"),
        ])


        existing_keys.add(key)


    if not rows:

        print(
            "  ℹ️ 새로 저장할 "
            "매출/반품이 없습니다."
        )

        return
    # -----------------------------------------
    # 시트 행 자동 확장
    # -----------------------------------------

    required_rows = len(existing) + len(rows) + 10

    if ws.row_count < required_rows:

        new_size = required_rows + 5000

        print(
            f"  📈 시트 행 확장 "
            f"{ws.row_count:,} → {new_size:,}"
        )

        ws.resize(
            rows=new_size
        )


    print(
        f"  📦 신규 저장 "
        f"매출/반품: "
        f"{len(rows):,}건"
    )


    # -----------------------------------------
    # 데이터 저장 (append 방식)
    # -----------------------------------------

    print(
        "  📥 Google Sheets append 저장 시작"
    )


    for i in range(
        0,
        len(rows),
        SHEET_CHUNK_SIZE
    ):

        chunk = rows[
            i:i + SHEET_CHUNK_SIZE
        ]


        ws.append_rows(
            chunk,
            value_input_option="RAW"
        )


    print(
        f"🎉 신규 매출/반품 "
        f"{len(rows):,}건 저장 완료"
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

def print_sales_summary(sales_data):

    if isinstance(sales_data, dict):
        sale_count = int(sales_data.get("sale_count", 0))
        sale_qty = int(sales_data.get("sale_qty", 0))
        return_count = int(sales_data.get("return_count", 0))
        return_qty = int(sales_data.get("return_qty", 0))
    else:
        sale_count = 0
        sale_qty = 0
        return_count = 0
        return_qty = 0

        for sale in sales_data:
            qty = int(sale.get("qty", 0) or 0)
            if sale.get("order_type") == "반품":
                return_count += 1
                return_qty += qty
            else:
                sale_count += 1
                sale_qty += qty

    print("")
    print("----------------------------------------")
    print("📊 이번 실행 매출 요약")
    print(f"  판매 건수: {sale_count:,}건")
    print(f"  판매 수량: {sale_qty:,}개")
    print(f"  반품 건수: {return_count:,}건")
    print(f"  반품 수량: {return_qty:,}개")
    print(f"  순판매수량: {sale_qty - return_qty:,}개")
    print("----------------------------------------")


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

        stock_success = False

        try:
            stock_data = get_all_stock(
                session,
                store_list
            )
            save_stock_to_sheets(stock_data)
            stock_success = True
            print("========================================")
            print("📦 재고 동기화 완료!")
            print("========================================")
        except Exception as e:
            print(f"⚠️ 재고 동기화 실패: {e}")
            print("ℹ️ 재고는 건너뛰고 매출 동기화를 계속합니다.")

        # =================================================
        # 매출
        # =================================================

        sales_success = False

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
                f"{sales_data.get('total_count', 0):,}건"
            )

            # 매출은 조회 중 SHEET_CHUNK_SIZE 단위로 이미 저장되었습니다.

            # ---------------------------------------------
            # 판매속도
            # ---------------------------------------------

            calculate_7day_average()

            sales_success = True

            print(
                "💰 매출 동기화 완료!"
            )

        except Exception as e:

            print(
                f"⚠️ 매출 동기화 실패: {e}"
            )

            print(
                "ℹ️ 매출 저장은 완료되지 않았으며, "
                "재고는 별도로 처리되었습니다."
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
