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
# 3. 현재 재고 전체 조회
# 4. 2026-07-01 이후 판매/반품 전체 누적
# 5. 판매/반품 중복 저장 방지
# 6. 최근 14일 판매량 계산
# 7. 최근 14일 일평균 판매량 계산
#
# 매출데이터 구조
# 날짜 | 매장 | 바코드 | 상품명 | 옵션명 | 판매수량 |
# 영수증번호 | 주문번호 | 상품순번
#
# 중요:
# - 반품은 판매수량을 음수로 저장
# - 주문번호 + 상품순번을 이용해 같은 상품의 중복 저장 방지
# - 매 실행 시 7/1 이후 구간을 다시 확인하여 누락/신규 데이터를 보완
# - Google Sheets의 기존 데이터는 새 구조에 맞게 유지
# =====================================================


# =====================================================
# 환경변수
# =====================================================

SELLMATE_ID = os.environ["SELLMATE_ID"]
SELLMATE_PW = os.environ["SELLMATE_PW"]

SELLMATE_DOMAIN = os.environ.get(
    "SELLMATE_DOMAIN",
    "hetras",
)

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

GOOGLE_CREDS = json.loads(
    os.environ["GOOGLE_CREDENTIALS"]
)


# =====================================================
# 셀메이트 API 설정
# =====================================================

BASE_URL = "https://sellmatepos.com/json"

SELLMATE_JS_VERSION = "2.8.4"

PER_PAGE = 100

# 매출/반품 저장 시작일
SALES_START_DATE = datetime.strptime(
    "2026-07-01",
    "%Y-%m-%d",
).date()

# 최근 14일 평균
SALES_AVERAGE_DAYS = 14

# API 재시도
API_RETRY_COUNT = 3

# Google Sheets 저장 단위
SHEET_CHUNK_SIZE = 5000


# =====================================================
# 공통 함수
# =====================================================

def norm(value):
    """
    매장명에서 마지막 '점' 또는 '店'을 제거한다.
    예: 안국점 -> 안국
    """
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


def safe_int(value, default=0):
    try:
        return int(float(value or 0))
    except (ValueError, TypeError):
        return default


def parse_order_date(order):
    datetime_text = str(
        order.get("datetime", "") or ""
    )

    if len(datetime_text) < 10:
        return None

    try:
        return datetime.strptime(
            datetime_text[:10],
            "%Y-%m-%d",
        ).date()
    except ValueError:
        return None


# =====================================================
# 1. 셀메이트 로그인
# =====================================================

def login():

    print("🔐 셀메이트 로그인 중...")

    session = requests.Session()

    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-pos-domain": SELLMATE_DOMAIN,
        "x-api-version": "2.2",
        "sellmate-pos-js-version": SELLMATE_JS_VERSION,
        "pos-locale": "kr",
        "Referer": "https://sellmatepos.com/",
        "Origin": "https://sellmatepos.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
        ),
    })

    try:
        res = session.post(
            f"{BASE_URL}/auth/login",
            json={
                "domain": SELLMATE_DOMAIN,
                "id": SELLMATE_ID,
                "pw": SELLMATE_PW,
                "isSellmateAdmin": 0,
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

    token_info = session.cookies.get("tokenInfo")

    if token_info:
        try:
            token_data = json.loads(
                urllib.parse.unquote(token_info)
            )

            token = token_data.get("access_token")

        except Exception as e:
            print(
                f"⚠️ tokenInfo 파싱 실패: {e}"
            )

    if not token:
        try:
            data = res.json()

            if isinstance(data, dict):
                token = (
                    data.get("access_token")
                    or data.get("token")
                    or (
                        data.get("data", {}).get("access_token")
                        if isinstance(data.get("data"), dict)
                        else None
                    )
                )

        except Exception:
            pass

    if not token:
        raise Exception("토큰 추출 실패")

    session.headers.update({
        "Authorization": f"Bearer {token}",
        "origin_useridx": "9",
        "pos-locale": "kr",
        "sellmate-pos-js-version": SELLMATE_JS_VERSION,
        "x-api-version": "2.2",
        "x-pos-domain": SELLMATE_DOMAIN,
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
        items = raw.get("data", [])

        if not items and isinstance(
            raw.get("stores"),
            list,
        ):
            items = raw["stores"]

    else:
        items = []

    stores = {}

    for store in items:

        if not isinstance(store, dict):
            continue

        name = norm(
            store.get("name", "")
        )

        idx = store.get("idx")

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

def get_all_stock(session, store_list):

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
                    "page": page,
                    "perPage": PER_PAGE,
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
            items = data.get("data", [])

            meta = data.get("meta", {})

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

            barcode_data = (
                item.get("barcode")
                or {}
            )

            if not isinstance(
                barcode_data,
                dict,
            ):
                barcode_data = {}

            barcode = str(
                barcode_data.get("code1", "")
                or item.get("code1", "")
                or ""
            ).strip()

            if not barcode:
                continue

            product = (
                item.get("product")
                or {}
            )

            if not isinstance(
                product,
                dict,
            ):
                product = {}

            product_class = (
                item.get("product_class")
                or {}
            )

            if not isinstance(
                product_class,
                dict,
            ):
                product_class = {}

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

            stocks = (
                item.get("stocks")
                or []
            )

            for stock in stocks:

                if not isinstance(
                    stock,
                    dict,
                ):
                    continue

                warehouse = (
                    stock.get("warehouse")
                    or {}
                )

                if not isinstance(
                    warehouse,
                    dict,
                ):
                    warehouse = {}

                store_idx = (
                    stock.get("store_idx")
                    or warehouse.get("store_idx")
                )

                store_name = idx_to_store.get(
                    store_idx,
                    "",
                )

                if not store_name:

                    warehouse_store = (
                        warehouse.get("store")
                        or {}
                    )

                    if not isinstance(
                        warehouse_store,
                        dict,
                    ):
                        warehouse_store = {}

                    store_name = norm(
                        stock.get("store_name", "")
                        or warehouse_store.get("name", "")
                        or ""
                    )

                qty = safe_int(
                    stock.get("stock", 0)
                    or stock.get("qty", 0)
                    or 0
                )

                if not store_name:
                    continue

                if store_name == "ALL":
                    continue

                all_stock.append({
                    "store": store_name,
                    "barcode": barcode,
                    "name": product_name,
                    "option": option_name,
                    "stock": qty,
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
        ws = sh.worksheet("재고데이터")

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
                rows_to_keep.append(row)

    stock_rows = []

    for item in stock_data:

        store = item.get("store", "")

        barcode = str(
            item.get("barcode", "")
            or ""
        ).strip()

        if (
            not store
            or not barcode
            or store == "ALL"
        ):
            continue

        qty = safe_int(
            item.get("stock", 0)
            or 0
        )

        stock_rows.append([
            today,
            store,
            barcode,
            item.get("name", ""),
            item.get("option", ""),
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
        SHEET_CHUNK_SIZE,
    ):
        chunk = all_rows[
            i:i + SHEET_CHUNK_SIZE
        ]

        start_row = i + 1
        end_row = i + len(chunk)

        ws.update(
            range_name=(
                f"A{start_row}:F{end_row}"
            ),
            values=chunk,
        )

    print(
        f"  ✅ 재고 "
        f"{len(stock_rows):,}건 저장 완료"
    )


# =====================================================
# 5. 매출 API 한 페이지
# =====================================================

def get_sales_page(session, page):

    params = {
        "page": page,
        "perPage": PER_PAGE,
    }

    last_error = None

    for attempt in range(
        1,
        API_RETRY_COUNT + 1,
    ):

        try:
            res = session.get(
                f"{BASE_URL}/order",
                params=params,
                timeout=60,
            )

            print(
                f"  📡 매출 API "
                f"page={page} "
                f"응답: {res.status_code}"
            )

            if res.status_code == 200:

                try:
                    data = res.json()
                except Exception as e:
                    last_error = (
                        f"JSON 파싱 실패: {e}"
                    )

                    if attempt < API_RETRY_COUNT:
                        time.sleep(attempt * 3)
                        continue

                    raise Exception(
                        f"매출 API JSON 파싱 실패 "
                        f"(page {page})"
                    )

                if isinstance(data, list):
                    return data, 1

                if not isinstance(data, dict):
                    raise Exception(
                        f"매출 API 응답 구조가 "
                        f"예상과 다릅니다. "
                        f"(page {page})"
                    )

                orders = data.get("data", [])

                meta = data.get("meta", {})

                if not isinstance(meta, dict):
                    meta = {}

                last_page = (
                    data.get("last_page")
                    or meta.get("last_page")
                    or 1
                )

                return orders, int(last_page)

            last_error = (
                f"{res.status_code} "
                f"{res.text[:500]}"
            )

            print(
                f"  ⚠️ 매출 API 오류 "
                f"{attempt}/{API_RETRY_COUNT}: "
                f"{last_error}"
            )

        except requests.RequestException as e:

            last_error = str(e)

            print(
                f"  ⚠️ 매출 API 요청 오류 "
                f"{attempt}/{API_RETRY_COUNT}: "
                f"{e}"
            )

        if attempt < API_RETRY_COUNT:
            time.sleep(attempt * 3)

    raise Exception(
        f"매출 API 조회 실패 "
        f"(page {page}): "
        f"{last_error}"
    )


# =====================================================
# 6. 매출 시트 구조
# =====================================================

SALES_HEADER = [
    "날짜",
    "매장",
    "바코드",
    "상품명",
    "옵션명",
    "판매수량",
    "영수증번호",
    "주문번호",
    "상품순번",
]


def make_sales_key(
    date,
    store,
    barcode,
    receipt,
    order_idx,
    item_seq,
):
    """
    상품 단위의 고유키.

    같은 영수증에서 상품이 여러 개일 수 있으므로
    영수증번호만으로 중복을 막지 않고
    주문번호 + 상품순번까지 사용한다.
    """

    return (
        str(date or "").strip(),
        str(store or "").strip(),
        str(barcode or "").strip(),
        str(receipt or "").strip(),
        str(order_idx or "").strip(),
        str(item_seq or "").strip(),
    )


def get_existing_sales_state(ws):

    print(
        "🔎 기존 매출 데이터 확인 중..."
    )

    records = ws.get_all_values()

    if not records:
        print(
            "  기존 매출 데이터: 0건"
        )

        return set(), False

    header = records[0]

    missing = [
        field
        for field in SALES_HEADER
        if field not in header
    ]

    if missing:
        print(
            "  ⚠️ 기존 매출 시트 헤더가 "
            "새 구조와 다릅니다."
        )

        print(
            f"  누락 헤더: {missing}"
        )

        print(
            "  ℹ️ 기존 데이터를 새 구조로 "
            "재구축합니다."
        )

        return set(), True

    date_idx = header.index("날짜")
    store_idx = header.index("매장")
    barcode_idx = header.index("바코드")
    receipt_idx = header.index("영수증번호")
    order_idx = header.index("주문번호")
    item_seq_idx = header.index("상품순번")

    existing_keys = set()

    for row in records[1:]:

        max_idx = max(
            date_idx,
            store_idx,
            barcode_idx,
            receipt_idx,
            order_idx,
            item_seq_idx,
        )

        if len(row) <= max_idx:
            continue

        key = make_sales_key(
            row[date_idx],
            row[store_idx],
            row[barcode_idx],
            row[receipt_idx],
            row[order_idx],
            row[item_seq_idx],
        )

        existing_keys.add(key)

    print(
        f"  기존 매출 데이터: "
        f"{len(existing_keys):,}건"
    )

    return existing_keys, False


# =====================================================
# 7. 주문 → 판매/반품 내역
# =====================================================

def convert_orders_to_sales(orders):

    sales = []

    for order in orders:

        if not isinstance(order, dict):
            continue

        order_type = str(
            order.get("order_type", "")
            or ""
        ).strip()

        # ---------------------------------------------
        # 판매 / 반품 모두 저장
        # ---------------------------------------------

        is_return = (
            order_type in (
                "반품",
                "return",
                "refund",
            )
        )

        is_sale = (
            order_type in (
                "",
                "판매",
                "sale",
                "normal",
            )
        )

        if not is_sale and not is_return:
            continue

        sale_date = parse_order_date(order)

        if sale_date is None:
            continue

        if sale_date < SALES_START_DATE:
            continue

        datetime_text = str(
            order.get("datetime", "")
            or ""
        )

        date_text = sale_date.strftime(
            "%Y-%m-%d"
        )

        store_name = norm(
            order.get("store_name", "")
        )

        receipt = str(
            order.get("receipt", "")
            or ""
        ).strip()

        order_idx = str(
            order.get("idx", "")
            or ""
        ).strip()

        items = (
            order.get("items")
            or []
        )

        for item_position, item in enumerate(
            items,
            start=1,
        ):

            if not isinstance(item, dict):
                continue

            barcode = str(
                item.get("barcode", "")
                or ""
            ).strip()

            if not barcode:
                continue

            raw_qty = safe_int(
                item.get("qty", 0)
                or 0
            )

            if raw_qty == 0:
                continue

            # 반품은 API가 -1로 주는 경우도 있고
            # +1로 주는 경우도 있으므로 항상 음수화
            if is_return:
                qty = -abs(raw_qty)
            else:
                qty = abs(raw_qty)

            # API item idx를 우선 상품순번으로 사용
            item_seq = (
                item.get("idx")
                or item.get("seq")
                or item_position
            )

            item_seq = str(
                item_seq
            ).strip()

            sales.append({
                "date": date_text,
                "store": store_name,
                "barcode": barcode,
                "name": (
                    item.get("product_name", "")
                    or ""
                ),
                "option": (
                    item.get("option_name", "")
                    or ""
                ),
                "qty": qty,
                "receipt": receipt,
                "order_idx": order_idx,
                "item_seq": item_seq,
                "datetime": datetime_text,
                "order_type": (
                    "반품"
                    if is_return
                    else "판매"
                ),
            })

    return sales


# =====================================================
# 8. 주문 날짜 범위
# =====================================================

def get_page_date_range(orders):

    dates = []

    for order in orders:

        if not isinstance(order, dict):
            continue

        sale_date = parse_order_date(order)

        if sale_date:
            dates.append(sale_date)

    if not dates:
        return None, None

    return min(dates), max(dates)


# =====================================================
# 9. 전체 매출 조회
# =====================================================

def get_sales(
    session,
    existing_keys,
):

    print(
        "💰 판매내역 동기화 시작..."
    )

    print(
        f"  📅 저장 시작일: "
        f"{SALES_START_DATE}"
    )

    # -------------------------------------------------
    # 전체 페이지 수 확인
    # -------------------------------------------------

    print(
        "  🔎 전체 매출 페이지 확인 중..."
    )

    _, last_page = get_sales_page(
        session,
        1,
    )

    print(
        f"  📄 전체 매출 페이지: "
        f"{last_page:,}"
    )

    if last_page <= 0:
        return []

    # -------------------------------------------------
    # 셀메이트의 현재 페이지 구조에서는
    # 마지막 페이지가 최신 데이터.
    # 따라서 마지막 페이지 → 1페이지 방향으로 이동.
    # -------------------------------------------------

    print(
        "  🔄 최신 매출부터 역순으로 조회합니다."
    )

    new_sales = []

    seen_keys = set(existing_keys)

    checked_pages = 0

    for page in range(
        last_page,
        0,
        -1,
    ):

        orders, _ = get_sales_page(
            session,
            page,
        )

        checked_pages += 1

        if not orders:
            print(
                f"  ⚠️ page {page}: "
                "주문 데이터 없음"
            )
            continue

        oldest_date, newest_date = (
            get_page_date_range(orders)
        )

        if (
            oldest_date
            and newest_date
        ):
            print(
                f"  📅 page {page:,}: "
                f"{oldest_date} ~ {newest_date}"
            )

        page_sales = convert_orders_to_sales(
            orders
        )

        page_new_count = 0

        for sale in page_sales:

            key = make_sales_key(
                sale["date"],
                sale["store"],
                sale["barcode"],
                sale["receipt"],
                sale["order_idx"],
                sale["item_seq"],
            )

            if key in seen_keys:
                continue

            seen_keys.add(key)

            new_sales.append(sale)

            page_new_count += 1

        print(
            f"  🔎 page {page:,}: "
            f"주문 {len(orders):,}건 / "
            f"신규 {page_new_count:,}건"
        )

        # 7/1 이전 데이터가 포함된 페이지에 도달하면
        # 해당 페이지까지는 7/1 이후 데이터가 섞여 있을 수 있으므로
        # 그 페이지를 처리한 후 다음 페이지부터 종료.
        if (
            oldest_date is not None
            and oldest_date < SALES_START_DATE
        ):
            print(
                f"  🛑 {SALES_START_DATE} 이전 "
                "데이터 도달"
            )
            break

    # -------------------------------------------------
    # 정렬
    # -------------------------------------------------

    new_sales.sort(
        key=lambda x: (
            x["date"],
            x["datetime"],
            x["store"],
            x["receipt"],
            x["item_seq"],
        )
    )

    print(
        f"  🔎 확인 페이지: "
        f"{checked_pages:,}개"
    )

    print(
        "----------------------------------------"
    )

    # -------------------------------------------------
    # 요약
    # -------------------------------------------------

    sale_count = 0
    sale_qty = 0

    return_count = 0
    return_qty = 0

    for sale in new_sales:

        if sale["qty"] < 0:
            return_count += 1
            return_qty += abs(sale["qty"])
        else:
            sale_count += 1
            sale_qty += sale["qty"]

    net_qty = sale_qty - return_qty

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
        f"{net_qty:,}개"
    )

    print(
        "----------------------------------------"
    )

    print(
        f"✅ 이번 실행 신규 판매/반품 내역: "
        f"{len(new_sales):,}건"
    )

    return new_sales


# =====================================================
# 10. 기존 매출 구조 재구축
# =====================================================

def reset_sales_sheet(ws):

    print(
        "  🔄 매출데이터 시트를 "
        "새 구조로 초기화합니다."
    )

    ws.clear()

    ws.update(
        range_name="A1",
        values=[SALES_HEADER],
    )


# =====================================================
# 11. 매출 저장
# =====================================================

def save_sales_to_sheets(
    sales_data,
    force_rebuild=False,
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
            cols=len(SALES_HEADER),
        )

    existing = ws.get_all_values()

    # ---------------------------------------------
    # 기존 시트가 없거나 헤더가 다르면 초기화
    # ---------------------------------------------

    if (
        force_rebuild
        or not existing
        or existing[0] != SALES_HEADER
    ):

        if existing and existing[0] != SALES_HEADER:
            print(
                "  ⚠️ 기존 매출 시트 헤더가 "
                "새 구조와 다릅니다."
            )

        reset_sales_sheet(ws)

        existing = [
            SALES_HEADER
        ]

    # ---------------------------------------------
    # 기존 고유키 생성
    # ---------------------------------------------

    existing_keys = set()

    header = existing[0]

    date_idx = header.index("날짜")
    store_idx = header.index("매장")
    barcode_idx = header.index("바코드")
    receipt_idx = header.index("영수증번호")
    order_idx = header.index("주문번호")
    item_seq_idx = header.index("상품순번")

    for row in existing[1:]:

        if len(row) <= max(
            date_idx,
            store_idx,
            barcode_idx,
            receipt_idx,
            order_idx,
            item_seq_idx,
        ):
            continue

        key = make_sales_key(
            row[date_idx],
            row[store_idx],
            row[barcode_idx],
            row[receipt_idx],
            row[order_idx],
            row[item_seq_idx],
        )

        existing_keys.add(key)

    # ---------------------------------------------
    # 신규 데이터만 저장
    # ---------------------------------------------

    rows = []

    for sale in sales_data:

        key = make_sales_key(
            sale.get("date", ""),
            sale.get("store", ""),
            sale.get("barcode", ""),
            sale.get("receipt", ""),
            sale.get("order_idx", ""),
            sale.get("item_seq", ""),
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
            sale.get("item_seq", ""),
        ])

        existing_keys.add(key)

    if not rows:

        print(
            "  ℹ️ 새로 저장할 매출/반품이 없습니다."
        )

        return

    print(
        f"  📦 신규 저장 매출/반품: "
        f"{len(rows):,}건"
    )

    start_row = len(existing) + 1

    for i in range(
        0,
        len(rows),
        SHEET_CHUNK_SIZE,
    ):

        chunk = rows[
            i:i + SHEET_CHUNK_SIZE
        ]

        current_start = start_row + i

        current_end = (
            current_start
            + len(chunk)
            - 1
        )

        ws.update(
            range_name=(
                f"A{current_start}:"
                f"I{current_end}"
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
# 12. 최근 14일 판매속도
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
    ]

    missing = [
        field
        for field in required
        if field not in header
    ]

    if missing:

        print(
            f"⚠️ 판매속도 계산에 필요한 "
            f"헤더가 없습니다: {missing}"
        )

        return

    date_idx = header.index("날짜")
    store_idx = header.index("매장")
    barcode_idx = header.index("바코드")
    name_idx = header.index("상품명")
    option_idx = header.index("옵션명")
    qty_idx = header.index("판매수량")

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
        ):
            continue

        date_text = row[date_idx]

        try:
            sale_date = datetime.strptime(
                date_text,
                "%Y-%m-%d",
            ).date()

        except ValueError:
            continue

        if not (
            start_date
            <= sale_date
            <= today
        ):
            continue

        store = row[store_idx]
        barcode = row[barcode_idx]

        if not store or not barcode:
            continue

        qty = safe_int(
            row[qty_idx],
            0,
        )

        key = (
            store,
            barcode,
        )

        if key not in summary:

            summary[key] = {
                "store": store,
                "barcode": barcode,
                "name": row[name_idx],
                "option": row[option_idx],
                "total_qty": 0,
            }

        summary[key]["total_qty"] += qty

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
            x["barcode"],
        ),
    ):

        total_qty = int(
            item["total_qty"]
        )

        average = (
            total_qty
            / SALES_AVERAGE_DAYS
        )

        output.append([
            today.strftime("%Y-%m-%d"),
            (
                start_date.strftime("%Y-%m-%d")
                + " ~ "
                + today.strftime("%Y-%m-%d")
            ),
            item["store"],
            item["barcode"],
            item["name"],
            item["option"],
            total_qty,
            round(average, 2),
            SALES_AVERAGE_DAYS,
        ])

    ws.clear()

    for i in range(
        0,
        len(output),
        SHEET_CHUNK_SIZE,
    ):

        chunk = output[
            i:i + SHEET_CHUNK_SIZE
        ]

        start_row = i + 1
        end_row = i + len(chunk)

        ws.update(
            range_name=(
                f"A{start_row}:I{end_row}"
            ),
            values=chunk,
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
# 13. 메인
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
            store_list,
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

                existing_keys, needs_rebuild = (
                    get_existing_sales_state(
                        sales_ws
                    )
                )

            except gspread.WorksheetNotFound:

                print(
                    "  기존 매출 데이터: 0건"
                )

                existing_keys = set()
                needs_rebuild = True

            sales_data = get_sales(
                session,
                existing_keys,
            )

            save_sales_to_sheets(
                sales_data,
                force_rebuild=needs_rebuild,
            )

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
