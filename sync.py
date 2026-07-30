
# =====================================================
# 헤트라스 셀메이트 자동 동기화
#
# 기능
# 1. 셀메이트 로그인
# 2. 매장 목록 조회
# 3. 현재 재고 전체 조회 → Google Sheets
# 4. 판매내역 전체/신규분 조회 → Google Sheets
# 5. 판매내역 중복 저장 방지
# 6. 최근 14일 판매량 집계
# 7. 최근 14일 일평균 판매량 계산
#
# ※ GitHub Actions에서 6시간마다 실행하도록 설정
# =====================================================

import os
import json
import urllib.parse
import requests
import gspread

from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials


# =====================================================
# 환경변수
# =====================================================

SELLMATE_ID = os.environ["SELLMATE_ID"]
SELLMATE_PW = os.environ["SELLMATE_PW"]
SELLMATE_DOMAIN = os.environ.get("SELLMATE_DOMAIN", "hetras")

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

GOOGLE_CREDS = json.loads(
    os.environ["GOOGLE_CREDENTIALS"]
)


# =====================================================
# 셀메이트 API
# =====================================================

BASE_URL = "https://sellmatepos.com/json"

# 현재 확인된 셀메이트 JS 버전
SELLMATE_JS_VERSION = "2.8.4"


# =====================================================
# 기본 설정
# =====================================================

PER_PAGE = 100

# 최근 14일 계산
SALES_AVERAGE_DAYS = 14

# 신규 매출 조회 시 안전하게 확인할 최대 페이지
# 최근 판매가 계속 첫 페이지 근처에 있으므로
# 일반적인 경우 몇 페이지 안에서 종료됨
MAX_INCREMENTAL_PAGES = 500

# 최초 전체 데이터 구축 시 한 번에 Google Sheets에
# 너무 많은 데이터를 넣지 않기 위한 chunk
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
# 1. 로그인
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

    token_info = (
        session.cookies.get(
            "tokenInfo"
        )
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
# 2. 매장 목록
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

        idx = store.get(
            "idx"
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

    return stores


# =====================================================
# 3. 재고 조회
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

        data = res.json()

        if isinstance(
            data,
            list
        ):

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

                store_name = (
                    idx_to_store.get(
                        store_idx,
                        ""
                    )
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
# 4. 재고 Google Sheets 저장
# =====================================================

def save_stock_to_sheets(stock_data):

    print(
        "📊 재고 데이터를 "
        "Google Sheets에 저장 중..."
    )

    if not stock_data:

        raise Exception(
            "재고 데이터가 0건입니다."
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

    today = datetime.now(
        timezone.utc
    ).astimezone().strftime(
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
# 5. 매출 데이터 API 한 페이지
# =====================================================

def get_sales_page(
    session,
    page
):

    try:

        res = session.get(

            f"{BASE_URL}/order",

            params={

                "page":
                    page,

                "perPage":
                    PER_PAGE,

                "sort[0][field]":
                    "datetime",

                "sort[0][direction]":
                    "DESC",
            },

            timeout=30,
        )

    except requests.RequestException as e:

        raise Exception(
            f"매출 API 요청 실패 "
            f"(page {page}): {e}"
        )

    if res.status_code != 200:

        raise Exception(
            f"매출 API 조회 실패 "
            f"(page {page}): "
            f"{res.status_code} "
            f"{res.text[:500]}"
        )

    data = res.json()

    if isinstance(
        data,
        list
    ):

        return data, 1

    orders = data.get(
        "data",
        []
    )

    last_page = (

        data.get(
            "last_page"
        )

        or data.get(
            "meta",
            {}
        ).get(
            "last_page",
            1
        )
    )

    return orders, int(
        last_page
    )


# =====================================================
# 6. 기존 매출 마지막 IDX 확인
# =====================================================

def get_existing_sales_state(ws):

    print(
        "🔎 기존 매출 데이터 확인 중..."
    )

    try:

        # idx를 저장하는 구조가 아니라
        # receipt + barcode + datetime으로
        # 중복을 판단할 수 있도록 사용
        records = ws.get_all_values()

    except Exception as e:

        raise Exception(
            f"기존 매출 데이터 조회 실패: {e}"
        )

    if not records:

        return set()

    header = records[0]

    try:

        receipt_idx = header.index(
            "영수증번호"
        )

    except ValueError:

        receipt_idx = None

    try:

        date_idx = header.index(
            "날짜"
        )

        store_idx = header.index(
            "매장"
        )

        barcode_idx = header.index(
            "바코드"
        )

        qty_idx = header.index(
            "판매수량"
        )

    except ValueError:

        return set()

    existing_keys = set()

    for row in records[1:]:

        if len(row) <= max(
            date_idx,
            store_idx,
            barcode_idx,
            qty_idx
        ):
            continue

        receipt = ""

        if (
            receipt_idx is not None
            and len(row) > receipt_idx
        ):

            receipt = row[
                receipt_idx
            ]

        key = (

            row[date_idx],

            row[store_idx],

            row[barcode_idx],

            receipt,

            row[qty_idx],
        )

        existing_keys.add(key)

    print(
        f"  기존 매출 중복체크 키: "
        f"{len(existing_keys):,}개"
    )

    return existing_keys


# =====================================================
# 7. 주문 → 판매내역 변환
# =====================================================

def convert_orders_to_sales(
    orders
):

    sales = []

    for order in orders:

        if not isinstance(
            order,
            dict
        ):
            continue

        order_type = str(
            order.get(
                "order_type",
                ""
            )
            or ""
        )

        if order_type not in (
            "",
            "판매",
            "sale",
            "normal",
        ):
            continue

        order_date = str(
            order.get(
                "datetime",
                ""
            )
            or ""
        )

        if not order_date:
            continue

        order_date = order_date[:10]

        store_name = norm(
            order.get(
                "store_name",
                ""
            )
        )

        receipt = str(
            order.get(
                "receipt",
                ""
            )
            or ""
        )

        items = (
            order.get(
                "items"
            )
            or []
        )

        for item in items:

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

            sales.append({

                "date":
                    order_date,

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
                        or ""
                    ),

                "option":
                    (
                        item.get(
                            "option_name",
                            ""
                        )
                        or ""
                    ),

                "qty":
                    qty,

                "receipt":
                    receipt,

                "order_idx":
                    order.get(
                        "idx"
                    ),

                "datetime":
                    order.get(
                        "datetime",
                        ""
                    ),
            })

    return sales


# =====================================================
# 8. 매출 데이터 가져오기
# =====================================================

def get_sales(
    session,
    existing_keys
):

    print(
        "💰 판매내역 동기화 시작..."
    )

    first_orders, last_page = (
        get_sales_page(
            session,
            1
        )
    )

    print(
        f"  📄 전체 매출 페이지: "
        f"{last_page:,}"
    )

    if not first_orders:

        return []

    first_sales = (
        convert_orders_to_sales(
            first_orders
        )
    )

    # 기존 데이터가 없으면 최초 구축
    initial_load = (
        len(existing_keys) == 0
    )

    if initial_load:

        print(
            "🆕 기존 매출 데이터가 없습니다."
        )

        print(
            "📥 최초 전체 판매내역 구축을 시작합니다."
        )

        all_sales = []

        for sale in first_sales:

            all_sales.append(
                sale
            )

        # 최신 → 과거 방향으로 전부 조회
        for page in range(
            2,
            last_page + 1
        ):

            if page % 100 == 0:

                print(
                    f"  📥 전체 매출 "
                    f"{page:,}/{last_page:,}페이지"
                )

            orders, _ = get_sales_page(
                session,
                page
            )

            if not orders:
                continue

            sales = convert_orders_to_sales(
                orders
            )

            all_sales.extend(
                sales
            )

        print(
            f"✅ 최초 판매내역 "
            f"{len(all_sales):,}건 확보"
        )

        return all_sales

    # =================================================
    # 기존 데이터가 있는 경우
    # 신규 데이터만 가져오기
    # =================================================

    print(
        "🔄 기존 매출 데이터가 있습니다."
    )

    print(
        "📥 신규 판매내역만 확인합니다."
    )

    new_sales = []

    page = 1

    while page <= MAX_INCREMENTAL_PAGES:

        orders, _ = get_sales_page(
            session,
            page
        )

        if not orders:
            break

        sales = convert_orders_to_sales(
            orders
        )

        new_count = 0

        for sale in sales:

            key = (

                sale["date"],

                sale["store"],

                sale["barcode"],

                sale["receipt"],

                str(
                    sale["qty"]
                ),
            )

            if key in existing_keys:

                continue

            new_sales.append(
                sale
            )

            new_count += 1

        print(
            f"  🔎 page {page}: "
            f"{len(orders)}건 주문 / "
            f"{new_count}건 신규"
        )

        # 현재 페이지의 판매가 전부
        # 기존 데이터에 존재하면
        # 다음 페이지부터는 과거 데이터일 가능성이
        # 높으므로 종료
        if (
            sales
            and new_count == 0
        ):

            print(
                "  🛑 기존 데이터 구간 도달"
            )

            break

        page += 1

    print(
        f"✅ 신규 매출 "
        f"{len(new_sales):,}건"
    )

    return new_sales


# =====================================================
# 9. 매출 Google Sheets 저장
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

            cols=8,
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
    ]

    existing = ws.get_all_values()

    if not existing:

        ws.update(
            range_name="A1",
            values=[header],
        )

        existing = [header]

    existing_keys = set()

    # 기존 데이터 중복 체크
    for row in existing[1:]:

        if len(row) < 8:
            continue

        key = (

            row[0],

            row[1],

            row[2],

            row[6],

            row[5],
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
                "receipt",
                ""
            ),

            str(
                sale.get(
                    "qty",
                    0
                )
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
        ])

    if not rows:

        print(
            "  ℹ️ 새로 저장할 매출이 없습니다."
        )

        return

    print(
        f"  📦 신규 저장 매출: "
        f"{len(rows):,}건"
    )

    # 기존 마지막 행
    start_row = (
        len(existing) + 1
    )

    # chunk 단위로 저장
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
                f"H{current_end}"
            ),

            values=chunk,
        )

        print(
            f"  ✅ 매출 "
            f"{current_start:,}~"
            f"{current_end:,}행 저장"
        )

    print(
        f"🎉 신규 매출 "
        f"{len(rows):,}건 저장 완료"
    )


# =====================================================
# 10. 최근 14일 일평균 판매량 계산
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

    try:

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

    except ValueError as e:

        raise Exception(
            f"매출데이터 헤더 오류: {e}"
        )

    today = datetime.now(
        timezone.utc
    ).astimezone().date()

    start_date = (
        today
        - timedelta(
            days=SALES_AVERAGE_DAYS - 1
        )
    )

    # =================================================
    # 매장 + 바코드별 판매량 집계
    # =================================================

    summary = {}

    for row in records[1:]:

        if len(row) <= max(
            date_idx,
            store_idx,
            barcode_idx,
            name_idx,
            option_idx,
            qty_idx
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

        summary[key][
            "total_qty"
        ] += qty

    # =================================================
    # 결과 생성
    # =================================================

    output = [

        [
            "기준일",

            "조회기간",

            "매장",

            "바코드",

            "상품명",

            "옵션명",

            "14일 판매수량",

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
            item["total_qty"]
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
# 11. 메인
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

        # ---------------------------------------------
        # 로그인
        # ---------------------------------------------

        session = login()

        # ---------------------------------------------
        # 매장
        # ---------------------------------------------

        store_list = get_store_list(
            session
        )

        # ---------------------------------------------
        # 재고
        # ---------------------------------------------

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

        # ---------------------------------------------
        # 매출
        # ---------------------------------------------

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

                existing_keys = set()

            sales_data = get_sales(

                session,

                existing_keys
            )

            save_sales_to_sheets(
                sales_data
            )

            # -----------------------------------------
            # 최근 14일 평균 계산
            # -----------------------------------------

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
