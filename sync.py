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
# 7. 최근 14일 판매속도 계산
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

SELLMATE_JS_VERSION = "2.8.4"

PER_PAGE = 100

SALES_START_DATE = datetime.strptime(
    "2026-07-01",
    "%Y-%m-%d"
).date()

SALES_AVERAGE_DAYS = 14

API_RETRY_COUNT = 3

SHEET_CHUNK_SIZE = 5000


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

    while True:

        try:

            res = session.get(

                f"{BASE_URL}/product/variant/stock",

                params={

                    "page":
                        page,

                    "perPage":
                        PER_PAGE,
                },

                timeout=30,
            )

        except requests.RequestException as e:

            raise Exception(
                f"재고 API 요청 실패 "
                f"(page {page}): {e}"
            )

        print(
            f"  재고 API 응답: "
            f"{res.status_code}"
        )

        if res.status_code != 200:

            raise Exception(
                f"재고 API 조회 실패 "
                f"(page {page}): "
                f"{res.status_code}"
            )

        try:

            data = res.json()

        except Exception:

            raise Exception(
                f"재고 API JSON 파싱 실패 "
                f"(page {page})"
            )

        if isinstance(data, list):

            items = data

            last_page = 1

        else:

            items = data.get(
                "data",
                []
            )

            meta = data.get(
                "meta",
                {}
            )

            last_page = meta.get(
                "last_page",
                1
            )

        if not items:
            break

        for item in items:

            if not isinstance(
                item,
                dict
            ):
                continue

            barcode_data = (
                item.get(
                    "barcode"
                )
                or {}
            )

            barcode = str(

                barcode_data.get(
                    "code1",
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

            product = (
                item.get(
                    "product"
                )
                or {}
            )

            product_class = (
                item.get(
                    "product_class"
                )
                or {}
            )

            product_name = (

                product.get(
                    "name",
                    ""
                )

                or product_class.get(
                    "name",
                    ""
                )

                or item.get(
                    "original_name",
                    ""
                )

                or ""
            )

            option_name = (

                item.get(
                    "origin_option_name",
                    ""
                )

                or item.get(
                    "option_name",
                    ""
                )

                or ""
            )

            stocks = (
                item.get(
                    "stocks"
                )
                or []
            )

            for stock in stocks:

                if not isinstance(
                    stock,
                    dict
                ):
                    continue

                warehouse = (
                    stock.get(
                        "warehouse"
                    )
                    or {}
                )

                store_idx = (

                    stock.get(
                        "store_idx"
                    )

                    or warehouse.get(
                        "store_idx"
                    )
                )

                store_name = idx_to_store.get(
                    store_idx,
                    ""
                )

                if not store_name:

                    warehouse_store = (
                        warehouse.get(
                            "store"
                        )
                        or {}
                    )

                    store_name = norm(

                        stock.get(
                            "store_name",
                            ""
                        )

                        or warehouse_store.get(
                            "name",
                            ""
                        )

                        or ""
                    )

                try:

                    qty = int(

                        stock.get(
                            "stock",
                            0
                        )

                        or stock.get(
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

                if not store_name:
                    continue

                all_stock.append({

                    "store":
                        store_name,

                    "barcode":
                        barcode,

                    "name":
                        product_name,

                    "option":
                        option_name,

                    "stock":
                        qty,
                })

        print(
            f"  재고 page "
            f"{page}/{last_page} "
            f"({len(all_stock)}건)"
        )

        if page >= last_page:
            break

        page += 1

    if not all_stock:

        raise Exception(
            "재고 데이터를 가져오지 못했습니다."
        )

    print(
        f"✅ 재고 총 "
        f"{len(all_stock)}건"
    )

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

        if sale_date < SALES_START_DATE:
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

        "page":
            page,

        "perPage":
            PER_PAGE,

        # 핵심
        "store_idx":
            store_idx,
    }

    last_error = None

    for attempt in range(
        1,
        API_RETRY_COUNT + 1
    ):

        try:

            res = session.get(

                f"{BASE_URL}/order",

                params=params,

                timeout=60,
            )

            print(

                f"  📡 매출 API "
                f"store_idx={store_idx} "
                f"page={page} "
                f"응답: {res.status_code}"
            )

            if res.status_code == 200:

                try:

                    data = res.json()

                except Exception:

                    raise Exception(
                        "매출 API JSON 파싱 실패"
                    )

                if isinstance(
                    data,
                    list
                ):

                    return data, 1

                orders = data.get(
                    "data",
                    []
                )

                meta = data.get(
                    "meta",
                    {}
                )

                last_page = (

                    data.get(
                        "last_page"
                    )

                    or meta.get(
                        "last_page",
                        1
                    )
                )

                return orders, int(
                    last_page
                )

            last_error = (

                f"{res.status_code} "

                f"{res.text[:500]}"
            )

            print(

                f"  ⚠️ 매출 API 오류 "

                f"{attempt}/"
                f"{API_RETRY_COUNT}: "

                f"{last_error}"
            )

        except requests.RequestException as e:

            last_error = str(e)

            print(

                f"  ⚠️ 매출 API 요청 오류 "

                f"{attempt}/"
                f"{API_RETRY_COUNT}: "

                f"{e}"
            )

        except Exception as e:

            last_error = str(e)

            print(

                f"  ⚠️ 매출 처리 오류 "

                f"{attempt}/"
                f"{API_RETRY_COUNT}: "

                f"{e}"
            )

        if attempt < API_RETRY_COUNT:

            time.sleep(
                attempt * 3
            )

    raise Exception(

        f"매출 API 조회 실패 "

        f"(store_idx={store_idx}, "
        f"page={page}): "

        f"{last_error}"
    )


# =====================================================
# 매출 기존 데이터 확인
# =====================================================

def get_existing_sales_state(ws):

    print(
        "🔎 기존 매출 데이터 확인 중..."
    )

    records = ws.get_all_values()

    if not records:

        print(
            "  기존 매출 데이터: 0건"
        )

        return set()

    header = records[0]

    required = [

        "날짜",
        "매장",
        "바코드",
        "판매수량",
        "영수증번호",
        "주문번호",
        "상품순번",
        "판매구분",
    ]

    missing = [

        field

        for field in required

        if field not in header
    ]

    if missing:

        print(
            "  ⚠️ 기존 매출 시트 "
            "헤더가 새 구조와 다릅니다."
        )

        print(
            f"  누락 헤더: {missing}"
        )

        return set()

    indexes = {

        field:
            header.index(field)

        for field in required
    }

    existing_keys = set()

    for row in records[1:]:

        try:

            key = (

                row[indexes["날짜"]],

                row[indexes["매장"]],

                row[indexes["바코드"]],

                row[indexes["주문번호"]],

                row[indexes["상품순번"]],

                row[indexes["판매구분"]],
            )

            existing_keys.add(
                key
            )

        except (
            IndexError,
            KeyError
        ):

            continue

    print(
        f"  기존 매출 데이터: "
        f"{len(existing_keys):,}건"
    )

    return existing_keys


# =====================================================
# 매출 전체 조회
#
# ★ 핵심 변경
# 매장별로 API를 각각 조회
# =====================================================

def get_sales(
    session,
    store_list,
    existing_keys
):

    print(
        "💰 전체 매장 판매내역 "
        "동기화 시작..."
    )

    print(
        f"  📅 저장 시작일: "
        f"{SALES_START_DATE}"
    )

    all_sales = []
    total_pages_checked = 0

    for store_name, store_idx in store_list.items():

        print("")
        print("========================================")
        print(
            f"🏪 [{store_name}] 매출 조회 시작"
        )
        print(
            f"  store_idx={store_idx}"
        )

        # ---------------------------------------------
        # 첫 페이지 조회
        # ---------------------------------------------

        first_orders, last_page = get_sales_page(
            session,
            1,
            store_idx
        )

        total_pages_checked += 1

        print(
            f"  📄 [{store_name}] "
            f"전체 페이지: {last_page:,}"
        )

        if not first_orders:

            print(
                f"  ⚠️ [{store_name}] "
                "주문 데이터 없음"
            )

            continue

        # ---------------------------------------------
        # 첫 페이지 날짜 확인
        # ---------------------------------------------

        first_dates = []

        for order in first_orders:

            d = get_order_date(order)

            if d:
                first_dates.append(d)

        if not first_dates:

            print(
                f"  ⚠️ [{store_name}] "
                "날짜가 있는 주문 데이터가 없습니다."
            )

            continue

        first_oldest = min(first_dates)
        first_newest = max(first_dates)

        print(
            f"  📅 page 1: "
            f"{first_oldest} ~ {first_newest}"
        )

        # ---------------------------------------------
        # page 2 확인
        # 페이지 방향 판단
        # ---------------------------------------------

        direction = "unknown"

        if last_page > 1:

            second_orders, _ = get_sales_page(
                session,
                2,
                store_idx
            )

            total_pages_checked += 1

            second_dates = []

            for order in second_orders:

                d = get_order_date(order)

                if d:
                    second_dates.append(d)

            if second_dates:

                second_oldest = min(second_dates)
                second_newest = max(second_dates)

                print(
                    f"  📅 page 2: "
                    f"{second_oldest} ~ {second_newest}"
                )

                # page 번호가 올라갈수록 날짜가 최신이면
                # 과거 → 최신 방향
                if second_oldest > first_oldest:

                    direction = "old_to_new"

                # page 번호가 올라갈수록 날짜가 과거면
                # 최신 → 과거 방향
                elif second_newest < first_newest:

                    direction = "new_to_old"

        print(
            f"  🧭 [{store_name}] "
            f"페이지 방향: {direction}"
        )

        # =================================================
        # 과거 → 최신
        #
        # 현재 로그는 이 경우에 해당할 가능성이 높음.
        #
        # 2,603페이지를 전부 읽지 않고
        # 7월 1일 근처 페이지를 이진탐색
        # =================================================

        if direction == "old_to_new":

            low = 1
            high = last_page
            target_page = last_page

            while low <= high:

                mid = (low + high) // 2

                if mid == 1:

                    orders = first_orders

                else:

                    orders, _ = get_sales_page(
                        session,
                        mid,
                        store_idx
                    )

                    total_pages_checked += 1

                if not orders:

                    low = mid + 1
                    continue

                dates = []

                for order in orders:

                    d = get_order_date(order)

                    if d:
                        dates.append(d)

                if not dates:

                    low = mid + 1
                    continue

                oldest_date = min(dates)
                newest_date = max(dates)

                print(
                    f"  🔎 [{store_name}] "
                    f"탐색 page {mid}: "
                    f"{oldest_date} ~ {newest_date}"
                )

                # 아직 7월 1일 이전이면
                # 더 뒤쪽 페이지로 이동
                if newest_date < SALES_START_DATE:

                    low = mid + 1

                else:

                    # 7월 1일 이후 데이터가 나오는
                    # 가장 앞쪽 페이지를 찾음
                    target_page = mid
                    high = mid - 1

            # 경계에서 누락될 가능성을 줄이기 위해
            # 앞쪽 2페이지부터 조회
            start_page = max(
                1,
                target_page - 2
            )

            print(
                f"  🎯 [{store_name}] "
                f"7월 1일 근처 시작 페이지: "
                f"{start_page}"
            )

            for page in range(
                start_page,
                last_page + 1
            ):

                if page == 1:

                    orders = first_orders

                else:

                    orders, _ = get_sales_page(
                        session,
                        page,
                        store_idx
                    )

                    total_pages_checked += 1

                if not orders:
                    continue

                sales = convert_orders_to_sales(
                    orders,
                    forced_store_name=store_name
                )

                new_count = 0

                for sale in sales:

                    key = (
                        sale["date"],
                        sale["store"],
                        sale["barcode"],
                        sale["order_idx"],
                        sale["item_idx"],
                        sale["order_type"],
                    )

                    if key in existing_keys:
                        continue

                    if any(
                        key == (
                            x["date"],
                            x["store"],
                            x["barcode"],
                            x["order_idx"],
                            x["item_idx"],
                            x["order_type"],
                        )
                        for x in all_sales
                    ):
                        continue

                    all_sales.append(sale)
                    new_count += 1

                dates = []

                for order in orders:

                    d = get_order_date(order)

                    if d:
                        dates.append(d)

                if dates:

                    print(
                        f"  🔎 [{store_name}] "
                        f"page {page}: "
                        f"주문 {len(orders)}건 / "
                        f"신규 {new_count}건"
                    )

                    print(
                        f"     📅 "
                        f"{min(dates)} ~ "
                        f"{max(dates)}"
                    )

            print(
                f"✅ [{store_name}] "
                f"매장 조회 완료"
            )

        # =================================================
        # 최신 → 과거
        #
        # 이 경우에는 기존 방식대로 최신부터 조회
        # =================================================

        elif direction == "new_to_old":

            for page in range(
                1,
                last_page + 1
            ):

                if page == 1:

                    orders = first_orders

                else:

                    orders, _ = get_sales_page(
                        session,
                        page,
                        store_idx
                    )

                    total_pages_checked += 1

                if not orders:
                    continue

                sales = convert_orders_to_sales(
                    orders,
                    forced_store_name=store_name
                )

                new_count = 0

                for sale in sales:

                    key = (
                        sale["date"],
                        sale["store"],
                        sale["barcode"],
                        sale["order_idx"],
                        sale["item_idx"],
                        sale["order_type"],
                    )

                    if key in existing_keys:
                        continue

                    if any(
                        key == (
                            x["date"],
                            x["store"],
                            x["barcode"],
                            x["order_idx"],
                            x["item_idx"],
                            x["order_type"],
                        )
                        for x in all_sales
                    ):
                        continue

                    all_sales.append(sale)
                    new_count += 1

                dates = []

                for order in orders:

                    d = get_order_date(order)

                    if d:
                        dates.append(d)

                if dates:

                    oldest_date = min(dates)
                    newest_date = max(dates)

                    print(
                        f"  🔎 [{store_name}] "
                        f"page {page}: "
                        f"주문 {len(orders)}건 / "
                        f"신규 {new_count}건"
                    )

                    print(
                        f"     📅 "
                        f"{oldest_date} ~ "
                        f"{newest_date}"
                    )

                    if oldest_date < SALES_START_DATE:

                        print(
                            f"  🛑 [{store_name}] "
                            f"{SALES_START_DATE} "
                            "이전 데이터 도달"
                        )

                        break

            print(
                f"✅ [{store_name}] "
                f"매장 조회 완료"
            )

        # =================================================
        # 방향 판단 실패
        # 안전하게 전체 페이지 조회
        # =================================================

        else:

            print(
                f"  ⚠️ [{store_name}] "
                "페이지 방향 판단 실패"
            )

            print(
                "  🔄 안전 모드로 전체 페이지 조회"
            )

            for page in range(
                1,
                last_page + 1
            ):

                if page == 1:

                    orders = first_orders

                else:

                    orders, _ = get_sales_page(
                        session,
                        page,
                        store_idx
                    )

                    total_pages_checked += 1

                if not orders:
                    continue

                sales = convert_orders_to_sales(
                    orders,
                    forced_store_name=store_name
                )

                for sale in sales:

                    key = (
                        sale["date"],
                        sale["store"],
                        sale["barcode"],
                        sale["order_idx"],
                        sale["item_idx"],
                        sale["order_type"],
                    )

                    if key in existing_keys:
                        continue

                    if any(
                        key == (
                            x["date"],
                            x["store"],
                            x["barcode"],
                            x["order_idx"],
                            x["item_idx"],
                            x["order_type"],
                        )
                        for x in all_sales
                    ):
                        continue

                    all_sales.append(sale)

            print(
                f"✅ [{store_name}] "
                f"매장 조회 완료"
            )

    # =================================================
    # 최종 결과
    # =================================================

    print("")
    print("========================================")

    print(
        f"📊 전체 매장 신규 데이터: "
        f"{len(all_sales):,}건"
    )

    print(
        f"  🔎 확인 페이지: "
        f"{total_pages_checked:,}개"
    )

    # -------------------------------------------------
    # 매장별 요약
    # -------------------------------------------------

    store_summary = {}

    for sale in all_sales:

        store = sale["store"]

        if store not in store_summary:

            store_summary[store] = {
                "count": 0,
                "qty": 0,
                "return_count": 0,
                "return_qty": 0,
            }

        if sale["order_type"] == "반품":

            store_summary[store]["return_count"] += 1
            store_summary[store]["return_qty"] += sale["qty"]

        else:

            store_summary[store]["count"] += 1
            store_summary[store]["qty"] += sale["qty"]

    print("")
    print("📊 매장별 신규 매출 요약")

    for store_name in store_list.keys():

        summary = store_summary.get(
            store_name,
            {
                "count": 0,
                "qty": 0,
                "return_count": 0,
                "return_qty": 0,
            }
        )

        net_qty = (
            summary["qty"]
            - summary["return_qty"]
        )

        print(
            f"  • {store_name}: "
            f"판매 {summary['count']:,}건 / "
            f"{summary['qty']:,}개 / "
            f"반품 {summary['return_count']:,}건 / "
            f"{summary['return_qty']:,}개 / "
            f"순판매 {net_qty:,}개"
        )

    print("========================================")

    return all_sales


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

            cols=10,
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

    # -------------------------------------------------
    # 기존 구조가 다르면
    # 기존 데이터는 유지하지 않고
    # 새 구조로 시작
    # -------------------------------------------------

    if existing:

        old_header = existing[0]

        if old_header != header:

            print(
                "  ⚠️ 기존 매출 시트 "
                "헤더가 새 구조와 다릅니다."
            )

            print(
                "  🔄 매출데이터 시트를 "
                "새 구조로 초기화합니다."
            )

            ws.clear()

            existing = []

    if not existing:

        ws.update(

            range_name="A1",

            values=[header],
        )

        existing = [header]

    # -------------------------------------------------
    # 기존 데이터 중복키
    # -------------------------------------------------

    existing_keys = set()

    for row in existing[1:]:

        if len(row) < 10:
            continue

        key = (

            row[0],   # 날짜
            row[1],   # 매장
            row[2],   # 바코드
            row[7],   # 주문번호
            row[8],   # 상품순번
            row[9],   # 판매구분
        )

        existing_keys.add(
            key
        )

    rows = []

    for sale in sales_data:

        key = (

            sale.get(
                "date",
                ""
            ),

            sale.get(
                "store",
                ""
            ),

            sale.get(
                "barcode",
                ""
            ),

            sale.get(
                "order_idx",
                ""
            ),

            sale.get(
                "item_idx",
                ""
            ),

            sale.get(
                "order_type",
                "판매"
            ),
        )

        if key in existing_keys:
            continue

        rows.append([

            sale.get(
                "date",
                ""
            ),

            sale.get(
                "store",
                ""
            ),

            sale.get(
                "barcode",
                ""
            ),

            sale.get(
                "name",
                ""
            ),

            sale.get(
                "option",
                ""
            ),

            sale.get(
                "qty",
                0
            ),

            sale.get(
                "receipt",
                ""
            ),

            sale.get(
                "order_idx",
                ""
            ),

            sale.get(
                "item_idx",
                ""
            ),

            sale.get(
                "order_type",
                "판매"
            ),
        ])

        existing_keys.add(
            key
        )

    if not rows:

        print(
            "  ℹ️ 새로 저장할 "
            "매출/반품이 없습니다."
        )

        return

    print(
        f"  📦 신규 저장 "
        f"매출/반품: "
        f"{len(rows):,}건"
    )

    start_row = (
        len(existing) + 1
    )

    for i in range(
        0,
        len(rows),
        SHEET_CHUNK_SIZE
    ):

        chunk = rows[
            i:i + SHEET_CHUNK_SIZE
        ]

        current_start = (
            start_row + i
        )

        current_end = (

            current_start

            + len(chunk)

            - 1
        )

        ws.update(

            range_name=(

                f"A{current_start}:"
                f"J{current_end}"
            ),

            values=chunk,
        )

        print(

            f"  ✅ 매출 "
            f"{current_start:,}~"
            f"{current_end:,}행 저장"
        )

    print(

        f"🎉 신규 매출/반품 "
        f"{len(rows):,}건 저장 완료"
    )


# =====================================================
# 최근 14일 판매속도
# =====================================================

def calculate_14day_average():

    print(
        "📈 최근 14일 "
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

            "14일 순판매수량",

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

    print(

        f"🕐 "

        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    print(

        f"🔧 셀메이트 JS 버전: "
        f"{SELLMATE_JS_VERSION}"
    )

    print(
        "========================================"
    )

    try:

        # =================================================
        # 로그인
        # =================================================

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

            calculate_14day_average()

            print(
                "💰 매출 동기화 완료!"
            )

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

        print(
            "🎉 동기화 완료!"
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

