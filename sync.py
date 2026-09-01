import os
import json
import time
import requests
import gspread

from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials


# =====================================================
# 헤트라스 셀메이트 EXTERNAL API → Google Sheets
#
# 핵심
# 1. POS External API client_credentials 방식 사용
# 2. JS 버전 / /json/order 의존 제거
# 3. 재고: GET /external/{domain}/stock
# 4. 매출: GET /external/{domain}/order
# 5. 매출은 페이지 단위로 즉시 Google Sheets 저장
# 6. 중복 KEY 방지
# 7. API 401 발생 시 토큰 자동 재발급
# 8. 7일 판매속도 계산
# 9. 중간 실패 시 다음 실행에서 이어서 처리
# =====================================================


# =====================================================
# 환경변수
# =====================================================

SELLMATE_DOMAIN = os.environ.get(
    "SELLMATE_DOMAIN",
    "hetras",
).strip()

SELLMATE_CLIENT_ID = (
    os.environ.get("SELLMATE_CLIENT_ID") or ""
).strip()

SELLMATE_CLIENT_SECRET = (
    os.environ.get("SELLMATE_CLIENT_SECRET") or ""
).strip()

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

GOOGLE_CREDS = json.loads(
    os.environ["GOOGLE_CREDENTIALS"]
)

# 최초 전체 보정 여부
FULL_RESCAN = (
    os.environ.get(
        "FULL_RESCAN",
        "false",
    ).strip().lower()
    == "true"
)

# 전체 보정 시작일.
# 현재 헤트라스 데이터가 2026년부터라면 2026-01-01 사용.
# 더 오래된 데이터까지 필요하면 GitHub Actions 변수로
# SALES_HISTORY_START_DATE=2000-01-01 을 지정하면 된다.
SALES_HISTORY_START_DATE = os.environ.get(
    "SALES_HISTORY_START_DATE",
    "2026-01-01",
).strip()

SALES_AVERAGE_DAYS = 7

# API 기간 단위.
# 너무 큰 기간은 페이지가 많아질 수 있으므로 7일씩 처리.
SALES_RANGE_DAYS = 7

PER_PAGE = 100

SHEET_CHUNK_SIZE = 5000

API_RETRY_COUNT = 3

EXTERNAL_BASE_URL = os.environ.get(
    "SELLMATE_EXTERNAL_BASE_URL",
    "https://sellmatepos.com",
).rstrip("/")

TOKEN_CACHE_SHEET = "API상태"
CURSOR_SHEET = "매출동기화상태"
SYNC_LOG_SHEET = "동기화로그"
SALES_SHEET = "매출데이터"
STOCK_SHEET = "재고데이터"
VELOCITY_SHEET = "판매속도"


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


def parse_date(value):
    if not value:
        return None

    text = str(value).strip()

    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(
                text[:19],
                fmt,
            ).date()
        except ValueError:
            continue

    return None


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
# External API 인증
# =====================================================

def issue_external_token():
    """
    테넌트 클라이언트 인증.
    POST /external/{domain}/issueToken
    """

    if (
        not SELLMATE_CLIENT_ID
        or not SELLMATE_CLIENT_SECRET
    ):
        raise Exception(
            "SELLMATE_CLIENT_ID / "
            "SELLMATE_CLIENT_SECRET 환경변수가 없습니다."
        )

    url = (
        f"{EXTERNAL_BASE_URL}"
        f"/external/{SELLMATE_DOMAIN}/issueToken"
    )

    payload = {
        "client_id": int(
            SELLMATE_CLIENT_ID
        ),
        "client_secret":
            SELLMATE_CLIENT_SECRET,
    }

    last_error = None

    for attempt in range(
        1,
        API_RETRY_COUNT + 1,
    ):
        try:
            res = requests.post(
                url,
                json=payload,
                headers={
                    "Content-Type":
                        "application/json",
                    "Accept":
                        "application/json",
                },
                timeout=30,
            )

            print(
                f"  🔑 External Token "
                f"응답: {res.status_code}"
            )

            if res.status_code == 200:
                data = res.json()

                token = (
                    data.get("access_token")
                    or ""
                )

                if not token:
                    raise Exception(
                        "access_token이 없습니다."
                    )

                print(
                    f"  ✅ External API 토큰 발급 성공 "
                    f"(expires_in={data.get('expires_in', '')})"
                )

                return token

            last_error = (
                f"{res.status_code} "
                f"{res.text[:500]}"
            )

        except Exception as e:
            last_error = str(e)

        if attempt < API_RETRY_COUNT:
            time.sleep(attempt * 2)

    # 문서상 global endpoint도 존재하므로
    # tenant endpoint 실패 시 1회 fallback
    fallback_url = (
        f"{EXTERNAL_BASE_URL}"
        f"/external/issueToken"
    )

    try:
        res = requests.post(
            fallback_url,
            json=payload,
            headers={
                "Content-Type":
                    "application/json",
                "Accept":
                    "application/json",
            },
            timeout=30,
        )

        print(
            f"  🔑 Global External Token "
            f"응답: {res.status_code}"
        )

        if res.status_code == 200:
            data = res.json()
            token = data.get(
                "access_token",
                "",
            )

            if token:
                print(
                    "  ✅ Global External API "
                    "토큰 발급 성공"
                )
                return token

        last_error = (
            f"{res.status_code} "
            f"{res.text[:500]}"
        )

    except Exception as e:
        last_error = str(e)

    raise Exception(
        f"External API 토큰 발급 실패: "
        f"{last_error}"
    )


def create_external_session():
    token = issue_external_token()

    session = requests.Session()

    session.headers.update({
        "Authorization":
            f"Bearer {token}",
        "Accept":
            "application/json",
        "Content-Type":
            "application/json",
    })

    return session


def refresh_external_session(session):
    print(
        "  🔄 External API 토큰 재발급..."
    )

    token = issue_external_token()

    session.headers.update({
        "Authorization":
            f"Bearer {token}",
    })


def external_get(
    session,
    path,
    params=None,
):
    """
    External API GET.
    401 발생 시 토큰을 재발급하고 같은 요청을 재시도한다.
    """

    url = (
        f"{EXTERNAL_BASE_URL}"
        f"{path}"
    )

    last_error = None

    for attempt in range(
        1,
        API_RETRY_COUNT + 1,
    ):
        try:
            res = session.get(
                url,
                params=params or {},
                timeout=90,
            )

            if res.status_code == 401:
                print(
                    f"  ⚠️ External API 401 "
                    f"({attempt}/{API_RETRY_COUNT})"
                )

                refresh_external_session(
                    session
                )

                if attempt < API_RETRY_COUNT:
                    time.sleep(1)
                    continue

            if res.status_code == 200:
                return res

            last_error = (
                f"{res.status_code} "
                f"{res.text[:500]}"
            )

            print(
                f"  ⚠️ External API 오류 "
                f"{attempt}/{API_RETRY_COUNT}: "
                f"{last_error}"
            )

        except requests.RequestException as e:
            last_error = str(e)

            print(
                f"  ⚠️ External API 네트워크 오류 "
                f"{attempt}/{API_RETRY_COUNT}: "
                f"{e}"
            )

        if attempt < API_RETRY_COUNT:
            time.sleep(
                min(
                    attempt * 2,
                    8,
                )
            )

    raise Exception(
        f"External API GET 실패: "
        f"{url} / {last_error}"
    )


# =====================================================
# 매장
# =====================================================

def get_store_list(session):
    print(
        "🏪 External API 매장 목록 조회 중..."
    )

    res = external_get(
        session,
        f"/external/{SELLMATE_DOMAIN}/store",
        {
            "page": 1,
            "perPage": 100,
        },
    )

    data = res.json()

    if isinstance(data, list):
        items = data
    else:
        items = data.get(
            "data",
            [],
        ) or []

    stores = {}

    for item in items:
        if not isinstance(
            item,
            dict,
        ):
            continue

        name = norm(
            item.get("name", "")
            or item.get("store_name", "")
        )

        idx = (
            item.get("idx")
            or item.get("store_idx")
        )

        if name and idx is not None:
            stores[name] = int(idx)

    if not stores:
        raise Exception(
            "External API에서 매장을 가져오지 못했습니다."
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

def extract_items(data):
    if isinstance(data, list):
        return data, 1

    if not isinstance(data, dict):
        return [], 1

    items = (
        data.get("data")
        or []
    )

    meta = (
        data.get("meta")
        or {}
    )

    last_page = (
        data.get("last_page")
        or meta.get("last_page")
        or 1
    )

    # links.last가 URL인 경우
    links = data.get("links") or {}
    if not last_page and links.get("last"):
        try:
            from urllib.parse import urlparse, parse_qs

            parsed = urlparse(
                links["last"]
            )
            query = parse_qs(
                parsed.query
            )
            last_page = int(
                query.get(
                    "page",
                    [1],
                )[0]
            )
        except Exception:
            last_page = 1

    return items, int(
        last_page or 1
    )


def get_all_stock(session, store_list):
    print(
        "📦 External API 재고 조회 중..."
    )

    idx_to_store = {
        int(value): key
        for key, value in store_list.items()
    }

    all_stock = []

    page = 1

    while True:
        res = external_get(
            session,
            f"/external/{SELLMATE_DOMAIN}/stock",
            {
                "page": page,
                "perPage": PER_PAGE,
            },
        )

        print(
            f"  📡 재고 API page={page} "
            f"응답: {res.status_code}"
        )

        data = res.json()

        items, last_page = extract_items(
            data
        )

        if not items:
            break

        for item in items:

            if not isinstance(
                item,
                dict,
            ):
                continue

            variant = (
                item.get("variant")
                or {}
            )

            product_class = (
                variant.get(
                    "productClass"
                )
                or item.get(
                    "product_class"
                )
                or {}
            )

            barcode_obj = (
                variant.get("barcode")
                or item.get("barcode")
                or {}
            )

            barcode = str(
                barcode_obj.get("code", "")
                or barcode_obj.get("code1", "")
                or item.get("code1", "")
                or ""
            ).strip()

            if not barcode:
                continue

            product_name = (
                item.get("product_name")
                or product_class.get("name")
                or item.get("name")
                or ""
            )

            option_name = (
                item.get(
                    "variant_option_name"
                )
                or item.get(
                    "origin_option_name"
                )
                or variant.get(
                    "origin_option_name"
                )
                or ""
            )

            variant_price = (
                item.get("variant_price")
                or item.get("price")
                or variant.get("price")
                or 0
            )

            warehouses = (
                item.get("warehouses")
                or item.get("stocks")
                or []
            )

            # API가 warehouse 하나를
            # 직접 반환하는 구조 대응
            if not warehouses:
                warehouse = (
                    item.get("warehouse")
                    or {}
                )

                if warehouse:
                    warehouses = [{
                        "warehouse":
                            warehouse,
                        "qty":
                            item.get(
                                "qty",
                                item.get(
                                    "stock",
                                    0,
                                ),
                            ),
                    }]

            for stock in warehouses:

                if not isinstance(
                    stock,
                    dict,
                ):
                    continue

                warehouse = (
                    stock.get("warehouse")
                    or {}
                )

                warehouse_store = (
                    warehouse.get("store")
                    or {}
                )

                store_idx = (
                    warehouse.get(
                        "store_idx"
                    )
                    or warehouse_store.get(
                        "idx"
                    )
                    or stock.get(
                        "store_idx"
                    )
                )

                store_name = idx_to_store.get(
                    int(store_idx)
                    if str(store_idx).isdigit()
                    else store_idx,
                    "",
                )

                if not store_name:
                    store_name = norm(
                        warehouse_store.get(
                            "name",
                            "",
                        )
                        or warehouse.get(
                            "store_name",
                            "",
                        )
                        or stock.get(
                            "store_name",
                            "",
                        )
                    )

                if not store_name:
                    continue

                qty = (
                    stock.get("qty")
                    if stock.get("qty") is not None
                    else stock.get("stock")
                )

                if qty is None:
                    qty = 0

                try:
                    qty = int(
                        float(qty)
                    )
                except (
                    ValueError,
                    TypeError,
                ):
                    qty = 0

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
                    "price":
                        variant_price,
                })

        print(
            f"  📄 재고 page "
            f"{page}/{last_page} "
            f"누적 {len(all_stock):,}건"
        )

        if page >= last_page:
            break

        page += 1

    print(
        f"✅ 재고 총 {len(all_stock):,}건"
    )

    return all_stock


def save_stock_to_sheets(stock_data):

    if not stock_data:
        print(
            "  ⚠️ 저장할 재고가 없습니다."
        )
        return False

    gc = get_google_client()
    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:
        ws = sh.worksheet(
            STOCK_SHEET
        )
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=STOCK_SHEET,
            rows=20000,
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

    rows = []

    # 동일 매장+바코드+옵션이 중복되는 경우
    # 마지막 값을 덮지 않고 합산하지 않는다.
    # API가 창고별로 내려줄 경우 매장 총재고가 필요하므로 합산한다.
    merged = {}

    for item in stock_data:

        key = (
            item.get("store", ""),
            item.get("barcode", ""),
            item.get("option", ""),
        )

        try:
            qty = int(
                item.get("stock", 0)
                or 0
            )
        except (
            ValueError,
            TypeError,
        ):
            qty = 0

        if key not in merged:
            merged[key] = {
                "name":
                    item.get(
                        "name",
                        "",
                    ),
                "qty":
                    qty,
            }
        else:
            merged[key]["qty"] += qty

    for (
        store,
        barcode,
        option,
    ), info in sorted(
        merged.items()
    ):
        if not store or not barcode:
            continue

        rows.append([
            today,
            store,
            barcode,
            info["name"],
            option,
            info["qty"],
        ])

    if not rows:
        print(
            "  ⚠️ 저장 가능한 재고가 없습니다."
        )
        return False

    all_rows = [
        header,
        *rows_to_keep,
        *rows,
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

        start_row = 1 + i
        end_row = (
            start_row
            + len(chunk)
            - 1
        )

        ws.update(
            range_name=(
                f"A{start_row}:F{end_row}"
            ),
            values=chunk,
        )

    print(
        f"  ✅ 재고 {len(rows):,}건 저장 완료"
    )

    return True


# =====================================================
# 매출 KEY / 변환
# =====================================================

def make_sale_key(sale):

    return "|".join([
        str(
            sale.get(
                "date",
                "",
            )
        ).strip(),

        str(
            sale.get(
                "store",
                "",
            )
        ).strip(),

        str(
            sale.get(
                "barcode",
                "",
            )
        ).strip(),

        str(
            sale.get(
                "receipt",
                "",
            )
        ).strip(),

        str(
            sale.get(
                "order_idx",
                "",
            )
        ).strip(),

        str(
            sale.get(
                "item_idx",
                "",
            )
        ).strip(),

        str(
            sale.get(
                "order_type",
                "판매",
            )
        ).strip(),
    ])


def convert_order_to_sales(order):
    sales = []

    if not isinstance(
        order,
        dict,
    ):
        return sales

    date_text = (
        order.get("datetime")
        or order.get("date")
        or order.get("ordered_at")
        or order.get("created_at")
        or ""
    )

    sale_date = parse_date(
        date_text
    )

    if not sale_date:
        return sales

    store = (
        order.get("store")
        or {}
    )

    if not isinstance(
        store,
        dict,
    ):
        store = {}

    store_name = norm(
        order.get("store_name", "")
        or order.get("storeName", "")
        or store.get("name", "")
        or store.get("store_name", "")
    )

    receipt = str(
        order.get("receipt")
        or order.get("receipt_number")
        or order.get("receiptNumber")
        or ""
    ).strip()

    order_number = str(
        order.get("order_no")
        or order.get("order_number")
        or order.get("origin_order_number")
        or order.get("originOrderNumber")
        or order.get("idx")
        or ""
    ).strip()

    order_type = str(
        order.get("order_type")
        or order.get("transaction_type")
        or order.get("type")
        or "판매"
    ).strip()

    lower_type = order_type.lower()

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
        or "refund" in lower_type
        or "return" in lower_type
    )

    items = (
        order.get("items")
        or order.get("order_items")
        or []
    )

    # External API 응답이 주문 1건 = 품목 1건인 경우
    if not items and (
        order.get("barcode")
        or order.get("product_name")
    ):
        items = [order]

    for index, item in enumerate(
        items,
        start=1,
    ):

        if not isinstance(
            item,
            dict,
        ):
            continue

        variant = (
            item.get("variant")
            or {}
        )

        barcode_obj = (
            item.get("barcode")
            or variant.get("barcode")
            or {}
        )

        if isinstance(
            barcode_obj,
            dict,
        ):
            barcode = str(
                barcode_obj.get("code")
                or barcode_obj.get("code1")
                or ""
            ).strip()
        else:
            barcode = str(
                barcode_obj or ""
            ).strip()

        barcode = (
            barcode
            or str(
                item.get("code1")
                or item.get("barcode_number")
                or ""
            ).strip()
        )

        if not barcode:
            continue

        qty = (
            item.get("qty")
            if item.get("qty") is not None
            else item.get("quantity")
        )

        if qty is None:
            qty = item.get(
                "item_qty",
                0,
            )

        try:
            qty = int(
                float(qty or 0)
            )
        except (
            ValueError,
            TypeError,
        ):
            qty = 0

        if qty <= 0:
            continue

        product_class = (
            variant.get(
                "productClass"
            )
            or item.get(
                "product_class"
            )
            or {}
        )

        name = (
            item.get("product_name")
            or item.get("productName")
            or item.get("name")
            or product_class.get("name")
            or ""
        )

        option = (
            item.get("variant_option_name")
            or item.get("option_name")
            or item.get("option")
            or ""
        )

        item_idx = str(
            item.get("idx")
            or item.get("item_idx")
            or item.get("order_item_idx")
            or index
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
                name,
            "option":
                option,
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
                str(date_text),
        })

    return sales


# =====================================================
# 매출 시트 / KEY
# =====================================================

def prepare_sales_sheet():

    gc = get_google_client()
    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:
        ws = sh.worksheet(
            SALES_SHEET
        )
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=SALES_SHEET,
            rows=300000,
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

    if not existing:
        ws.update(
            range_name="A1:J1",
            values=[header],
        )
        return ws

    if existing[0] == header:
        return ws

    old_header = existing[0]

    old_index = {
        name: idx
        for idx, name
        in enumerate(old_header)
    }

    migrated = []

    for row in existing[1:]:

        def value(
            field,
            default="",
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

        migrated.append([
            value("날짜"),
            value("매장"),
            value("바코드"),
            value("상품명"),
            value("옵션명"),
            value("판매수량", 0),
            value("영수증번호"),
            value("주문번호"),
            value("상품순번"),
            value(
                "판매구분",
                "판매",
            ) or "판매",
        ])

    ws.clear()

    ws.update(
        range_name="A1:J1",
        values=[header],
    )

    for i in range(
        0,
        len(migrated),
        SHEET_CHUNK_SIZE,
    ):
        chunk = migrated[
            i:i + SHEET_CHUNK_SIZE
        ]

        start_row = 2 + i
        end_row = (
            start_row
            + len(chunk)
            - 1
        )

        if chunk:
            ws.update(
                range_name=(
                    f"A{start_row}:J{end_row}"
                ),
                values=chunk,
            )

    return ws


def get_existing_sale_keys(ws):

    print(
        "🔎 기존 매출 KEY 확인 중..."
    )

    records = ws.get_all_values()

    if not records:
        return set()

    header = records[0]

    required = [
        "날짜",
        "매장",
        "바코드",
        "영수증번호",
        "주문번호",
        "상품순번",
        "판매구분",
    ]

    missing = [
        x for x in required
        if x not in header
    ]

    if missing:
        print(
            f"  ⚠️ KEY 컬럼 누락: {missing}"
        )
        return set()

    idx = {
        x: header.index(x)
        for x in required
    }

    keys = set()

    for row in records[1:]:

        if len(row) <= max(
            idx.values()
        ):
            continue

        keys.add(
            "|".join([
                str(
                    row[idx["날짜"]]
                ).strip(),

                str(
                    row[idx["매장"]]
                ).strip(),

                str(
                    row[idx["바코드"]]
                ).strip(),

                str(
                    row[idx["영수증번호"]]
                ).strip(),

                str(
                    row[idx["주문번호"]]
                ).strip(),

                str(
                    row[idx["상품순번"]]
                ).strip(),

                str(
                    row[idx["판매구분"]]
                ).strip()
                or "판매",
            ])
        )

    print(
        f"  기존 KEY: {len(keys):,}건"
    )

    return keys


# =====================================================
# 매출 커서
# =====================================================

CURSOR_HEADER = [
    "매장",
    "store_idx",
    "마지막기간",
    "마지막페이지",
    "상태",
    "업데이트시간",
]


def prepare_cursor_sheet():

    gc = get_google_client()
    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:
        return sh.worksheet(
            CURSOR_SHEET
        )
    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(
            title=CURSOR_SHEET,
            rows=100,
            cols=len(CURSOR_HEADER),
        )

        ws.update(
            range_name="A1:F1",
            values=[CURSOR_HEADER],
        )

        return ws


def load_cursors():

    ws = prepare_cursor_sheet()

    rows = ws.get_all_values()

    cursors = {}

    for row in rows[1:]:

        if len(row) < 4:
            continue

        store = norm(
            row[0]
        )

        if not store:
            continue

        try:
            page = int(
                row[3] or 0
            )
        except (
            ValueError,
            TypeError,
        ):
            page = 0

        cursors[store] = {
            "range":
                row[2] or "",
            "page":
                page,
            "status":
                row[4]
                if len(row) > 4
                else "",
        }

    return cursors


def save_cursor(
    store_name,
    store_idx,
    range_text,
    page,
    status,
):

    ws = prepare_cursor_sheet()

    rows = ws.get_all_values()

    target_row = None

    for row_no, row in enumerate(
        rows[1:],
        start=2,
    ):

        if (
            row
            and norm(row[0])
            == norm(store_name)
        ):
            target_row = row_no
            break

    values = [[
        store_name,
        str(store_idx),
        range_text,
        str(page),
        status,
        datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    ]]

    if target_row is None:

        ws.append_rows(
            values,
            value_input_option="RAW",
        )

    else:

        ws.update(
            range_name=(
                f"A{target_row}:F{target_row}"
            ),
            values=values,
        )


# =====================================================
# 매출 페이지 조회
# =====================================================

def get_sales_page(
    session,
    page,
    start_date,
    end_date,
):

    params = {
        "page":
            page,
        "perPage":
            PER_PAGE,
        "startDate":
            start_date.strftime(
                "%Y-%m-%d"
            ),
        "endDate":
            end_date.strftime(
                "%Y-%m-%d"
            ),
    }

    res = external_get(
        session,
        f"/external/{SELLMATE_DOMAIN}/order",
        params,
    )

    print(
        f"  📡 매출 API "
        f"page={page} "
        f"기간={params['startDate']}~"
        f"{params['endDate']} "
        f"응답: {res.status_code}"
    )

    data = res.json()

    return extract_items(
        data
    )


# =====================================================
# 매출 페이지 저장
# =====================================================

def append_sales_page(
    ws,
    sales,
    seen_keys,
):

    if not sales:
        return 0, []

    rows = []
    new_sales = []

    for sale in sales:

        key = make_sale_key(
            sale
        )

        if key in seen_keys:
            continue

        seen_keys.add(key)

        rows.append([
            sale.get(
                "date",
                "",
            ),
            sale.get(
                "store",
                "",
            ),
            sale.get(
                "barcode",
                "",
            ),
            sale.get(
                "name",
                "",
            ),
            sale.get(
                "option",
                "",
            ),
            sale.get(
                "qty",
                0,
            ),
            sale.get(
                "receipt",
                "",
            ),
            sale.get(
                "order_idx",
                "",
            ),
            sale.get(
                "item_idx",
                "",
            ),
            sale.get(
                "order_type",
                "판매",
            ),
        ])

        new_sales.append(
            sale
        )

    if not rows:
        return 0, []

    for i in range(
        0,
        len(rows),
        SHEET_CHUNK_SIZE,
    ):

        chunk = rows[
            i:i + SHEET_CHUNK_SIZE
        ]

        ws.append_rows(
            chunk,
            value_input_option="RAW",
        )

    return (
        len(rows),
        new_sales,
    )


# =====================================================
# 전체 매출 동기화
# =====================================================

def get_sales(
    session,
    store_list,
    existing_keys,
):

    print(
        "💰 External API 전체 매출 조회 시작..."
    )

    ws = prepare_sales_sheet()

    seen_keys = set(
        existing_keys
    )

    all_new_sales = []

    cursors = load_cursors()

    today = get_today()

    if FULL_RESCAN:
        history_start = parse_date(
            SALES_HISTORY_START_DATE
        )

        if not history_start:
            raise Exception(
                "SALES_HISTORY_START_DATE 형식 오류"
            )

        print(
            f"  🧹 FULL_RESCAN=true "
            f"{history_start} ~ {today}"
        )

        # 전체 보정에서는 기존 cursor 무시
        cursor_map = {}

    else:
        # 평상시에는 최근 7일을 다시 조회
        # 누락/반품/지연 반영을 위해 overlap
        history_start = (
            today
            - timedelta(
                days=SALES_RANGE_DAYS - 1
            )
        )

        print(
            f"  🔄 최근 "
            f"{SALES_RANGE_DAYS}일 보정: "
            f"{history_start} ~ {today}"
        )

        cursor_map = cursors

    # -------------------------------------------------
    # 날짜 구간을 7일씩 나눔
    # -------------------------------------------------

    range_start = history_start

    while range_start <= today:

        range_end = min(
            range_start
            + timedelta(
                days=SALES_RANGE_DAYS - 1
            ),
            today,
        )

        range_text = (
            f"{range_start}~{range_end}"
        )

        print("")
        print(
            "========================================"
        )
        print(
            f"📅 기간: {range_text}"
        )

        page = 1

        # FULL_RESCAN이 아니면
        # 날짜 구간 cursor가 있으면 그 다음 페이지부터
        if not FULL_RESCAN:

            for store_name, store_idx in (
                store_list.items()
            ):

                cursor = cursor_map.get(
                    norm(store_name)
                )

                # 최근 보정은 store별 API를 호출하지 않으므로
                # cursor는 참고용 상태로만 유지한다.
                if cursor:
                    pass

        while True:

            orders, last_page = (
                get_sales_page(
                    session,
                    page,
                    range_start,
                    range_end,
                )
            )

            if not orders:
                break

            page_sales = []

            for order in orders:

                page_sales.extend(
                    convert_order_to_sales(
                        order
                    )
                )

            new_count, new_sales = (
                append_sales_page(
                    ws,
                    page_sales,
                    seen_keys,
                )
            )

            all_new_sales.extend(
                new_sales
            )

            print(
                f"  📄 page "
                f"{page}/{last_page} "
                f"주문 {len(orders)}건 "
                f"변환 {len(page_sales)}건 "
                f"신규 저장 {new_count}건"
            )

            if page >= last_page:
                break

            page += 1

        # 기간 완료
        print(
            f"  ✅ 기간 완료: "
            f"{range_text}"
        )

        range_start = (
            range_end
            + timedelta(days=1)
        )

    print("")
    print(
        "========================================"
    )

    print(
        f"📊 이번 실행 신규 저장: "
        f"{len(all_new_sales):,}건"
    )

    return all_new_sales


# =====================================================
# 호환용
# =====================================================

def save_sales_to_sheets(sales_data):
    print(
        "  ℹ️ 매출은 페이지 조회 시 "
        "Google Sheets에 즉시 저장되었습니다."
    )


# =====================================================
# 판매속도 7일
# =====================================================

def calculate_7day_average():

    print(
        "📈 최근 7일 일평균 판매량 계산 중..."
    )

    gc = get_google_client()
    sh = gc.open_by_key(
        SPREADSHEET_ID
    )

    try:
        sales_ws = sh.worksheet(
            SALES_SHEET
        )
    except gspread.WorksheetNotFound:
        print(
            "⚠️ 매출데이터 시트가 없습니다."
        )
        return

    try:
        ws = sh.worksheet(
            VELOCITY_SHEET
        )
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(
            title=VELOCITY_SHEET,
            rows=20000,
            cols=9,
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
        x for x in required
        if x not in header
    ]

    if missing:
        print(
            f"⚠️ 판매속도 헤더 누락: {missing}"
        )
        return

    idx = {
        x: header.index(x)
        for x in required
    }

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
            idx.values()
        ):
            continue

        sale_date = parse_date(
            row[idx["날짜"]]
        )

        if not sale_date:
            continue

        if not (
            start_date
            <= sale_date
            <= today
        ):
            continue

        store = row[
            idx["매장"]
        ]

        barcode = row[
            idx["바코드"]
        ]

        if not store or not barcode:
            continue

        try:
            qty = int(
                float(
                    row[
                        idx["판매수량"]
                    ]
                    or 0
                )
            )
        except (
            ValueError,
            TypeError,
        ):
            qty = 0

        key = (
            store,
            barcode,
        )

        if key not in summary:
            summary[key] = {
                "store":
                    store,
                "barcode":
                    barcode,
                "name":
                    row[
                        idx["상품명"]
                    ],
                "option":
                    row[
                        idx["옵션명"]
                    ],
                "total":
                    0,
            }

        if (
            row[
                idx["판매구분"]
            ]
            == "반품"
        ):
            summary[key]["total"] -= qty
        else:
            summary[key]["total"] += qty

    output = [[
        "기준일",
        "조회기간",
        "매장",
        "바코드",
        "상품명",
        "옵션명",
        "7일 순판매수량",
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

        total = int(
            item["total"]
        )

        output.append([
            today.strftime(
                "%Y-%m-%d"
            ),
            (
                f"{start_date} ~ "
                f"{today}"
            ),
            item["store"],
            item["barcode"],
            item["name"],
            item["option"],
            total,
            round(
                total
                / SALES_AVERAGE_DAYS,
                2,
            ),
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

        start_row = 1 + i
        end_row = (
            start_row
            + len(chunk)
            - 1
        )

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


# =====================================================
# 동기화 완료 로그
# =====================================================

def check_daily_sync():

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
            cols=4,
        )

        ws.update(
            range_name="A1:D1",
            values=[[
                "날짜",
                "재고",
                "매출",
                "완료시간",
            ]],
        )

        return False

    today_text = get_today().strftime(
        "%Y-%m-%d"
    )

    rows = ws.get_all_values()

    for row in rows[1:]:

        if (
            row
            and row[0]
            == today_text
        ):

            return True

    return False


def save_daily_sync(
    stock_success,
    sales_success,
):

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
            cols=4,
        )

        ws.update(
            range_name="A1:D1",
            values=[[
                "날짜",
                "재고",
                "매출",
                "완료시간",
            ]],
        )

    ws.append_row([
        get_today().strftime(
            "%Y-%m-%d"
        ),
        "완료"
        if stock_success
        else "실패",
        "완료"
        if sales_success
        else "실패",
        datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    ])


# =====================================================
# MAIN
# =====================================================

def main():

    print(
        "========================================"
    )
    print(
        "🚀 헤트라스 셀메이트 EXTERNAL API 동기화 시작"
    )
    print(
        "========================================"
    )

    if check_daily_sync():
        print(
            "⏭️ 오늘 이미 동기화 완료"
        )
        return

    # -------------------------------------------------
    # External API 세션
    # -------------------------------------------------

    try:
        session = create_external_session()
    except Exception as e:
        print(
            f"❌ External API 인증 실패: {e}"
        )
        raise

    # -------------------------------------------------
    # 매장
    # -------------------------------------------------

    store_list = get_store_list(
        session
    )

    # -------------------------------------------------
    # 재고
    # -------------------------------------------------

    stock_success = False

    try:

        stock_data = get_all_stock(
            session,
            store_list,
        )

        stock_success = (
            save_stock_to_sheets(
                stock_data
            )
        )

        if stock_success:
            print(
                "📦 재고 동기화 완료!"
            )

    except Exception as e:

        print(
            f"⚠️ 재고 동기화 실패: {e}"
        )

        print(
            "ℹ️ 재고 실패와 관계없이 "
            "매출 동기화를 계속합니다."
        )

    # -------------------------------------------------
    # 매출
    # -------------------------------------------------

    sales_success = False

    try:

        sales_ws = prepare_sales_sheet()

        existing_keys = (
            get_existing_sale_keys(
                sales_ws
            )
        )

        sales_data = get_sales(
            session,
            store_list,
            existing_keys,
        )

        print(
            f"✅ 이번 실행 신규 "
            f"판매/반품: "
            f"{len(sales_data):,}건"
        )

        save_sales_to_sheets(
            sales_data
        )

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
            "ℹ️ 이미 Google Sheets에 "
            "저장된 페이지 데이터는 유지됩니다."
        )

    # -------------------------------------------------
    # 완료 로그
    # -------------------------------------------------

    if sales_success:

        save_daily_sync(
            stock_success,
            sales_success,
        )

        print(
            "🎉 매출 동기화 완료!"
        )

    else:

        print(
            "⚠️ 매출이 완료되지 않아 "
            "완료 로그를 기록하지 않습니다."
        )

    print(
        "========================================"
    )

    if stock_success and sales_success:
        print(
            "🎉 재고 + 매출 동기화 완료"
        )
    elif sales_success:
        print(
            "✅ 매출 완료 / "
            "⚠️ 재고 실패"
        )
    else:
        print(
            "❌ 매출 동기화 미완료"
        )

    print(
        "========================================"
    )


if __name__ == "__main__":
    main()
