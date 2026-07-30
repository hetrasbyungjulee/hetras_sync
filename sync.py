# =====================================================
# 헤트라스 셀메이트 자동 동기화
#
# 기능
# 1. 셀메이트 로그인
# 2. 매장 목록 조회
# 3. 현재 재고 전체 조회 → Google Sheets
# 4. 2026-07-01 이후 판매내역 누적 저장
# 5. 판매내역 중복 저장 방지
# 6. 최근 14일 판매량 집계
# 7. 최근 14일 일평균 판매량 계산
# 8. 현재고 / 일평균 판매량 = 재고 소진 예상일
#
# ※ GitHub Actions에서 6시간마다 실행
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

# 판매내역 저장 시작일
SALES_START_DATE = datetime.strptime(
    "2026-07-01",
    "%Y-%m-%d"
).date()

# 최근 14일 평균
SALES_AVERAGE_DAYS = 14

# 기존 데이터가 있는 경우 신규 조회 최대 페이지
MAX_INCREMENTAL_PAGES = 500

# Google Sheets 저장 chunk
SHEET_CHUNK_SIZE = 5000


# =====================================================
# 공통
# =====================================================

def norm(value):

    return (
        str(value or "")
        .strip()
        .rstrip("점")
        .rstrip("店")
    )


def get_korea_today():

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
# 1. 로그인
# =====================================================

def login():

    print("🔐 셀메이트 로그인 중...")

    session = requests.Session()

    session.headers.update({

        "Content-Type":
            "application/json",

        "Accept":
            "application/json",

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

                timeout=60,
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
                f"{res.status_code} "
                f"{res.text[:500]}"
            )

        try:

            data = res.json()

        except Exception:

            raise Exception(
                f"재고 API JSON 오류 "
                f"(page {page})"
            )

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

            last_page = (

                data.get(
                    "last_page"
                )

                or meta.get(
                    "last_page",
                    1
                )
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

            if isinstance(
                barcode_data,
                dict
            ):

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

            else:

                barcode = str(
                    barcode_data or ""
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

        if page >= int(last_page):
            break

        page += 1

    if not all_stock:

        raise Exception(
            "재고 데이터를 가져오지 못했습니다."
        )

    print(
        f"✅ 재고 총 "
        f"{len(all_stock):,}건"
    )

    return all_stock


# =====================================================
# 4. 재고 저장
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

    today = get_korea_today().strftime(
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

                rows_to_keep.append(row)

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

    for i in range(
        0,
        len(all_rows),
        SHEET_CHUNK_SIZE
    ):

        chunk = all_rows[
            i:i + SHEET_CHUNK_SIZE
        ]

        start = i + 1
        end = i + len(chunk)

        ws.update(
            range_name=f"A{start}:F{end}",
            values=chunk,
        )

    print(
        f"  ✅ 재고 "
        f"{len(stock_rows):,}건 저장 완료"
    )

# =====================================================
# 매출 설정
# =====================================================

# 2026년 7월 1일부터 판매내역 누적
SALES_START_DATE = datetime(2026, 7, 1).date()

# API 한 페이지당 조회 수
PER_PAGE = 100

# 너무 많은 API 요청을 방지하는 안전장치
MAX_SALES_PAGES = 3000


# =====================================================
# 매출 API 한 페이지 조회
# =====================================================

def get_sales_page(session, page):

    try:

        res = session.get(

            f"{BASE_URL}/order",

            params={
                "page": page,
                "perPage": PER_PAGE,

                # 최신 주문부터 요청
                "sort[0][field]": "datetime",
                "sort[0][direction]": "DESC",
            },

            timeout=60,
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

    try:

        data = res.json()

    except Exception:

        raise Exception(
            f"매출 API JSON 변환 실패 "
            f"(page {page})"
        )

    if isinstance(data, list):

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

        data.get("last_page")

        or meta.get("last_page")

        or 1
    )

    try:

        last_page = int(last_page)

    except (
        ValueError,
        TypeError
    ):

        last_page = 1

    return orders, last_page


# =====================================================
# 주문 → 판매내역 변환
# =====================================================

def convert_orders_to_sales(orders):

    sales = []

    for order in orders:

        if not isinstance(
            order,
            dict
        ):
            continue

        # ---------------------------------------------
        # 주문일시
        # ---------------------------------------------

        datetime_text = str(
            order.get(
                "datetime",
                ""
            )
            or ""
        ).strip()

        if len(datetime_text) < 10:
            continue

        date_text = datetime_text[:10]

        try:

            sale_date = datetime.strptime(
                date_text,
                "%Y-%m-%d"
            ).date()

        except ValueError:

            continue

        # 2026-07-01 이전 데이터는 제외
        if sale_date < SALES_START_DATE:
            continue

        # ---------------------------------------------
        # 판매 주문만
        # ---------------------------------------------

        order_type = str(
            order.get(
                "order_type",
                ""
            )
            or ""
        ).strip()

        # 판매가 아닌 반품/취소 등은 제외
        if order_type not in (
            "",
            "판매",
            "sale",
            "normal",
        ):
            continue

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
                    date_text,

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
                    order_idx,

                "datetime":
                    datetime_text,
            })

    return sales


# =====================================================
# 기존 매출 중복키
# =====================================================

def get_existing_sales_state(ws):

    print(
        "🔎 기존 매출 데이터 확인 중..."
    )

    try:

        records = ws.get_all_values()

    except Exception as e:

        raise Exception(
            f"기존 매출 데이터 조회 실패: {e}"
        )

    if len(records) <= 1:

        print(
            "  기존 매출 데이터: 0건"
        )

        return set()

    header = records[0]

    try:

        date_idx = header.index("날짜")
        store_idx = header.index("매장")
        barcode_idx = header.index("바코드")
        qty_idx = header.index("판매수량")
        receipt_idx = header.index("영수증번호")
        order_idx = header.index("주문번호")

    except ValueError:

        print(
            "  ⚠️ 기존 매출 헤더를 확인할 수 없습니다."
        )

        return set()

    existing_keys = set()

    for row in records[1:]:

        if len(row) <= max(
            date_idx,
            store_idx,
            barcode_idx,
            qty_idx,
            receipt_idx,
            order_idx
        ):
            continue

        key = (

            row[order_idx],

            row[barcode_idx],

            row[store_idx],

            row[receipt_idx],

            row[date_idx],

            row[qty_idx],
        )

        existing_keys.add(key)

    print(
        f"  기존 매출 중복체크 키: "
        f"{len(existing_keys):,}개"
    )

    return existing_keys


# =====================================================
# 7월부터 판매내역 가져오기
# =====================================================

def get_sales(
    session,
    existing_keys
):

    print(
        "💰 판매내역 동기화 시작..."
    )

    print(
        f"  📅 저장 시작일: "
        f"{SALES_START_DATE}"
    )

    # ---------------------------------------------
    # 첫 페이지
    # ---------------------------------------------

    first_orders, last_page = get_sales_page(
        session,
        1
    )

    print(
        f"  📄 전체 매출 페이지: "
        f"{last_page:,}"
    )

    if not first_orders:

        print(
            "⚠️ 매출 주문 데이터가 없습니다."
        )

        return []

    new_sales = []

    # ---------------------------------------------
    # 모든 페이지 순회
    #
    # 중요:
    # 페이지의 순서만 믿고 중간에 중단하지 않는다.
    # 실제 datetime을 확인해서 7월 데이터만 추출한다.
    # ---------------------------------------------

    for page in range(
        1,
        min(
            last_page,
            MAX_SALES_PAGES
        ) + 1
    ):

        if page == 1:

            orders = first_orders

        else:

            orders, _ = get_sales_page(
                session,
                page
            )

        if not orders:

            print(
                f"  ⚠️ page {page}: "
                "주문 데이터 없음"
            )

            continue

        sales = convert_orders_to_sales(
            orders
        )

        page_new = 0

        for sale in sales:

            key = (

                sale["order_idx"],

                sale["barcode"],

                sale["store"],

                sale["receipt"],

                sale["date"],

                str(
                    sale["qty"]
                ),
            )

            if key in existing_keys:

                continue

            # 같은 실행 안에서 중복 방지
            if key in existing_keys:

                continue

            existing_keys.add(key)

            new_sales.append(
                sale
            )

            page_new += 1

        if (
            page == 1
            or page % 10 == 0
        ):

            print(
                f"  🔎 page {page:,}/"
                f"{last_page:,}: "
                f"주문 {len(orders):,}건 / "
                f"7월 이후 판매 {len(sales):,}건 / "
                f"신규 {page_new:,}건"
            )

        # -----------------------------------------
        # API가 마지막 페이지보다 일찍 끝나는 경우
        # -----------------------------------------

        if len(orders) < PER_PAGE:

            print(
                f"  ℹ️ page {page}: "
                "마지막 데이터 페이지로 판단"
            )

            break

    print(
        f"✅ 이번 실행 신규 판매내역: "
        f"{len(new_sales):,}건"
    )

    return new_sales


# =====================================================
# 판매내역 Google Sheets 저장
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
            values=[header]
        )

        existing = [header]

    # ---------------------------------------------
    # 기존 데이터 중복키
    # ---------------------------------------------

    existing_keys = set()

    for row in existing[1:]:

        if len(row) < 8:
            continue

        key = (

            row[7],  # 주문번호
            row[2],  # 바코드
            row[1],  # 매장
            row[6],  # 영수증
            row[0],  # 날짜
            row[5],  # 수량
        )

        existing_keys.add(key)

    rows = []

    for sale in sales_data:

        key = (

            str(
                sale.get(
                    "order_idx",
                    ""
                )
            ),

            sale.get(
                "barcode",
                ""
            ),

            sale.get(
                "store",
                ""
            ),

            sale.get(
                "receipt",
                ""
            ),

            sale.get(
                "date",
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

        existing_keys.add(key)

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

    start_row = len(existing) + 1

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
# 최근 14일 일평균 판매량
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

    date_idx = header.index("날짜")
    store_idx = header.index("매장")
    barcode_idx = header.index("바코드")
    name_idx = header.index("상품명")
    option_idx = header.index("옵션명")
    qty_idx = header.index("판매수량")

    today = datetime.now(
        timezone.utc
    ).astimezone().date()

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
            qty_idx
        ):
            continue

        try:

            sale_date = datetime.strptime(
                row[date_idx],
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
                row[qty_idx]
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

    output = [[

        "기준일",
        "조회기간",
        "매장",
        "바코드",
        "상품명",
        "옵션명",
        "14일 판매수량",
        "일평균 판매수량",
        "계산일수",

    ]]

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
        values=output
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
# 12. 메인
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
        f"📅 매출 저장 시작일: "
        f"{SALES_START_DATE}"
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
            # 최근 14일 판매속도
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
