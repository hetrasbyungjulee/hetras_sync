import os
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import gspread
import requests
from google.oauth2.service_account import Credentials


# ============================================================
# HETRAS Sellmate -> Google Sheets
# External API 전용 버전
# ============================================================
# 사용하는 인증:
#   SELLMATE_CLIENT_ID
#   SELLMATE_CLIENT_SECRET
# ============================================================

SELLMATE_CLIENT_ID = os.environ.get("SELLMATE_CLIENT_ID", "").strip()
SELLMATE_CLIENT_SECRET = os.environ.get("SELLMATE_CLIENT_SECRET", "").strip()
SELLMATE_DOMAIN = os.environ.get("SELLMATE_DOMAIN", "hetras").strip()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
GOOGLE_CREDENTIALS_RAW = os.environ.get("GOOGLE_CREDENTIALS", "").strip()

EXTERNAL_BASE_URL = os.environ.get(
    "SELLMATE_EXTERNAL_BASE_URL",
    "https://sellmatepos.com",
).rstrip("/")

PER_PAGE = int(os.environ.get("SELLMATE_PER_PAGE", "100"))
API_RETRY_COUNT = int(os.environ.get("API_RETRY_COUNT", "3"))
SALES_AVERAGE_DAYS = 7
SALES_RANGE_DAYS = int(os.environ.get("SALES_RANGE_DAYS", "14"))
SALES_HISTORY_START = os.environ.get("SALES_HISTORY_START_DATE", "2026-07-01")
FORCE_SYNC = os.environ.get("FORCE_SYNC", "false").lower() == "true"

SALES_SHEET = "매출데이터"
STOCK_SHEET = "재고데이터"
VELOCITY_SHEET = "판매속도"
SYNC_LOG_SHEET = "동기화로그"
CHECKPOINT_SHEET = "동기화체크포인트"

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
    "판매구분",
    "주문일시",
]

STOCK_HEADER = [
    "날짜",
    "매장",
    "바코드",
    "상품명",
    "옵션명",
    "현재고",
]

VELOCITY_HEADER = [
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

CHECKPOINT_HEADER = [
    "작업키",
    "시작일",
    "종료일",
    "다음페이지",
    "상태",
    "수정시간",
]

SYNC_LOG_HEADER = ["날짜", "재고", "매출", "완료시간"]


# ============================================================
# 공통
# ============================================================

def fail(message: str) -> None:
    raise RuntimeError(message)


def parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None

    # ISO 8601 timezone 포함 문자열 대응
    try:
        iso_text = text.replace("Z", "+00:00")
        return datetime.fromisoformat(iso_text).date()
    except (ValueError, TypeError):
        pass

    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            pass
    return None


def get_today() -> date:
    return datetime.now(timezone.utc).astimezone().date()


def norm(value: Any) -> str:
    return str(value or "").strip().rstrip("점").rstrip("店")


def as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def find_list_payload(payload: Any) -> List[Dict[str, Any]]:
    """
    Sellmate External API의 다양한 응답 구조에서
    실제 데이터 배열을 최대한 안전하게 추출한다.
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    # 우선순위가 높은 배열 키
    preferred_keys = (
        "data",
        "items",
        "results",
        "orders",
        "order",
        "stocks",
        "stock",
        "details",
        "orderDetails",
        "orderItems",
        "order_items",
        "orderProducts",
        "order_products",
        "products",
        "list",
        "rows",
    )

    for key in preferred_keys:
        value = payload.get(key)

        if isinstance(value, list):
            result = [x for x in value if isinstance(x, dict)]
            if result:
                return result

        if isinstance(value, dict):
            nested = find_list_payload(value)
            if nested:
                return nested

    # 재귀적으로 모든 dict를 탐색
    for value in payload.values():
        if isinstance(value, dict):
            nested = find_list_payload(value)
            if nested:
                return nested

        elif isinstance(value, list):
            dict_items = [x for x in value if isinstance(x, dict)]
            if dict_items:
                return dict_items

    return []


def get_last_page(payload: Any, item_count: int) -> int:
    """API 응답의 pagination/meta에서 마지막 페이지를 안전하게 계산한다."""
    if isinstance(payload, list):
        return 999999 if item_count >= PER_PAGE else 1

    containers: List[Dict[str, Any]] = []
    if isinstance(payload, dict):
        containers.append(payload)
        for key in ("meta", "pagination", "paginate", "pageInfo", "page_info", "paging"):
            value = payload.get(key)
            if isinstance(value, dict):
                containers.append(value)

    for container in containers:
        for key in (
            "lastPage", "last_page", "totalPages", "total_pages",
            "pageCount", "page_count", "pages",
        ):
            value = container.get(key)
            if value not in (None, ""):
                try:
                    return max(1, int(value))
                except (ValueError, TypeError):
                    pass

        total = container.get("total")
        per_page = container.get("perPage") or container.get("per_page") or PER_PAGE
        if total not in (None, ""):
            try:
                total_int = int(total)
                per_page_int = max(1, int(per_page))
                return max(1, (total_int + per_page_int - 1) // per_page_int)
            except (ValueError, TypeError):
                pass

    return 999999 if item_count >= PER_PAGE else 1


# ============================================================
# Google Sheets
# ============================================================

def get_google_client() -> gspread.Client:
    if not SPREADSHEET_ID:
        fail("SPREADSHEET_ID가 없습니다.")
    if not GOOGLE_CREDENTIALS_RAW:
        fail("GOOGLE_CREDENTIALS가 없습니다.")
    try:
        info = json.loads(GOOGLE_CREDENTIALS_RAW)
    except json.JSONDecodeError as exc:
        fail(f"GOOGLE_CREDENTIALS JSON 파싱 실패: {exc}")

    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def open_sheet() -> gspread.Spreadsheet:
    return get_google_client().open_by_key(SPREADSHEET_ID)


def ensure_worksheet(sh: gspread.Spreadsheet, title: str, rows: int, cols: int) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def ensure_header(ws: gspread.Worksheet, header: List[str]) -> None:
    values = ws.get_all_values()
    if not values:
        ws.update([header], "A1")
        return
    if values[0] == header:
        return
    # 기존 헤더에 필요한 컬럼이 있으면 순서를 보존하면서 누락 컬럼 추가.
    old = values[0]
    merged = list(old)
    for col in header:
        if col not in merged:
            merged.append(col)
    if merged != old:
        ws.update([merged], "A1")


# ============================================================
# External API 인증
# ============================================================

def validate_config() -> None:
    missing = []
    if not SELLMATE_CLIENT_ID:
        missing.append("SELLMATE_CLIENT_ID")
    if not SELLMATE_CLIENT_SECRET:
        missing.append("SELLMATE_CLIENT_SECRET")
    if not SELLMATE_DOMAIN:
        missing.append("SELLMATE_DOMAIN")
    if not missing:
        return
    fail("필수 환경변수 누락: " + ", ".join(missing))


def issue_external_token() -> requests.Session:
    print("🔐 Sellmate External API 토큰 발급 중...")

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "HETRAS-Sellmate-Sync/1.0",
    })

    # ★ 중요: domain 없는 시스템 클라이언트 인증
    url = f"{EXTERNAL_BASE_URL}/external/issueToken"

    body = {
        "client_id": int(SELLMATE_CLIENT_ID),
        "client_secret": SELLMATE_CLIENT_SECRET,
    }

    print(f"  🌐 Token URL: {url}")
    print(f"  🔑 Client ID 설정됨: {bool(SELLMATE_CLIENT_ID)}")
    print(f"  🔐 Client Secret 설정됨: {bool(SELLMATE_CLIENT_SECRET)}")
    print(f"  🏢 Domain: {SELLMATE_DOMAIN}")

    last_error = None

    for attempt in range(1, API_RETRY_COUNT + 1):
        try:
            res = session.post(
                url,
                json=body,
                timeout=30,
            )

            print(f"  🔑 External Token 응답: {res.status_code}")

            if res.status_code == 200:
                payload = res.json()

                token = first_nonempty(
                    payload.get("access_token")
                    if isinstance(payload, dict)
                    else "",
                    as_dict(payload.get("data")).get("access_token")
                    if isinstance(payload, dict)
                    else "",
                )

                if not token:
                    fail(
                        "External API 토큰 발급 응답은 200이지만 "
                        "access_token이 없습니다."
                    )

                session.headers.update({
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                })

                print("✅ External API 토큰 발급 성공")
                return session

            last_error = f"{res.status_code} {res.text[:500]}"

            # 인증정보 오류는 재시도해도 의미 없음
            if res.status_code in (400, 401, 403):
                break

        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)

        if attempt < API_RETRY_COUNT:
            time.sleep(attempt * 2)

    fail(f"External API 토큰 발급 실패: {last_error}")


# ============================================================
# Store
# ============================================================

def get_store_list(session: requests.Session) -> Dict[str, Any]:
    print("🏪 External API 매장 목록 조회 중...")
    url = f"{EXTERNAL_BASE_URL}/external/{SELLMATE_DOMAIN}/store"
    try:
        res = session.get(url, params={"perPage": 200, "page": 1}, timeout=30)
    except requests.RequestException as exc:
        print(f"⚠️ 매장 API 요청 실패: {exc}")
        return {}

    print(f"  매장 API 응답: {res.status_code}")
    if res.status_code != 200:
        print(f"  응답: {res.text[:500]}")
        return {}

    try:
        payload = res.json()
    except ValueError:
        print("⚠️ 매장 API JSON 파싱 실패")
        return {}

    stores = {}
    for item in find_list_payload(payload):
        name = norm(first_nonempty(item.get("name"), item.get("store_name"), item.get("storeName")))
        idx = item.get("idx") or item.get("store_idx") or item.get("storeIdx")
        if name and idx is not None:
            stores[name] = idx

    if stores:
        print(f"📍 매장 {len(stores)}개: {list(stores.keys())}")
        for name, idx in stores.items():
            print(f"  • {name}: store_idx={idx}")
    else:
        print("ℹ️ External 매장 목록을 해석하지 못했습니다. 주문 응답의 매장명을 사용합니다.")
    return stores


# ============================================================
# Stock
# ============================================================

def convert_stock_items(payload: Any, idx_to_store: Dict[str, str]) -> List[List[Any]]:
    rows = []
    today = get_today().strftime("%Y-%m-%d")

    for item in find_list_payload(payload):
        variant = as_dict(item.get("variant"))
        product_class = as_dict(item.get("productClass")) or as_dict(variant.get("productClass"))
        barcode_obj = as_dict(variant.get("barcode")) or as_dict(item.get("barcode"))
        warehouse = as_dict(item.get("warehouse"))
        warehouse_store = as_dict(warehouse.get("store"))

        barcode = first_nonempty(
            barcode_obj.get("code"),
            barcode_obj.get("code1"),
            item.get("barcode"),
            item.get("code1"),
        )
        if not barcode:
            continue

        name = first_nonempty(
            item.get("product_name"),
            item.get("name"),
            variant.get("product_name"),
            product_class.get("name"),
        )
        option = first_nonempty(
            item.get("variant_option_name"),
            item.get("option_name"),
            variant.get("option_name"),
        )

        stocks = item.get("stocks") if isinstance(item.get("stocks"), list) else [item]
        for stock in stocks:
            stock = as_dict(stock)
            stock_warehouse = as_dict(stock.get("warehouse")) or warehouse
            stock_store = as_dict(stock_warehouse.get("store")) or warehouse_store
            store_idx = stock.get("store_idx") or stock_warehouse.get("store_idx") or stock_store.get("idx")
            store_name = idx_to_store.get(str(store_idx), "")
            if not store_name:
                store_name = norm(first_nonempty(
                    stock.get("store_name"),
                    stock.get("storeName"),
                    stock_store.get("name"),
                    item.get("store_name"),
                ))
            if not store_name:
                continue

            raw_qty = stock.get("stock")
            if raw_qty in (None, ""):
                raw_qty = stock.get("qty", item.get("stock", item.get("qty", 0)))
            try:
                qty = int(float(raw_qty or 0))
            except (ValueError, TypeError):
                qty = 0

            rows.append([today, store_name, barcode, name, option, qty])
    return rows


def sync_stock(session: requests.Session, store_map: Dict[str, Any]) -> bool:
    print("📦 재고 데이터 조회 중...")
    idx_to_store = {str(v): k for k, v in store_map.items()}
    all_rows: List[List[Any]] = []
    page = 1

    while True:
        url = f"{EXTERNAL_BASE_URL}/external/{SELLMATE_DOMAIN}/stock"
        try:
            res = session.get(url, params={"page": page, "perPage": PER_PAGE}, timeout=60)
        except requests.RequestException as exc:
            print(f"  ⚠️ 재고 API 요청 실패: {exc}")
            return False

        print(f"  재고 API 응답: {res.status_code} (page {page})")
        if res.status_code in (404, 405):
            print("  ℹ️ External 재고 엔드포인트가 현재 계정에서 지원되지 않습니다. 재고는 건너뜁니다.")
            return False
        if res.status_code != 200:
            print(f"  응답: {res.text[:500]}")
            return False

        try:
            payload = res.json()
        except ValueError:
            print("  ⚠️ 재고 API JSON 파싱 실패")
            return False

        items = find_list_payload(payload)
        if not items:
            break
        all_rows.extend(convert_stock_items(payload, idx_to_store))

        last_page = get_last_page(payload, len(items))
        if last_page <= 1 or page >= last_page or len(items) < PER_PAGE:
            break
        page += 1

    if not all_rows:
        print("  ⚠️ 저장할 재고 데이터가 없습니다.")
        return False

    sh = open_sheet()
    ws = ensure_worksheet(sh, STOCK_SHEET, 10000, len(STOCK_HEADER))
    ensure_header(ws, STOCK_HEADER)
    existing = ws.get_all_values()
    today = get_today().strftime("%Y-%m-%d")
    if existing:
        header = existing[0]
        date_idx = header.index("날짜") if "날짜" in header else 0
        keep = [row for row in existing[1:] if len(row) > date_idx and row[date_idx] != today]
    else:
        keep = []

    ws.clear()
    ws.update([STOCK_HEADER], "A1")
    combined = keep + all_rows
    for offset in range(0, len(combined), 5000):
        chunk = combined[offset:offset + 5000]
        start = offset + 2
        end = start + len(chunk) - 1
        ws.update(chunk, f"A{start}:F{end}")

    print(f"✅ 재고 {len(all_rows):,}건 저장 완료")
    return True


# ============================================================
# Sales normalization
# ============================================================
def get_nested_value(data: Any, paths: Iterable[Tuple[str, ...]]) -> Any:
    """
    여러 후보 경로를 순서대로 탐색한다.

    예:
        get_nested_value(
            order,
            [
                ("store", "name"),
                ("store", "store_name"),
                ("storeName",),
            ]
        )
    """
    for path in paths:
        current = data

        try:
            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)

            if current not in (None, ""):
                return current

        except Exception:
            continue

    return None


def scalar_value(value: Any) -> str:
    """
    문자열/숫자는 그대로 문자열로 만들고,
    dict가 들어오면 대표적인 코드/이름 필드를 추출한다.
    """
    if value is None:
        return ""

    if isinstance(value, dict):
        return first_nonempty(
            value.get("code"),
            value.get("value"),
            value.get("id"),
            value.get("idx"),
            value.get("name"),
            value.get("text"),
        )

    return str(value).strip()


def find_first_value(data: Dict[str, Any], keys: Iterable[str]) -> str:
    """
    현재 dict의 1-depth에서 후보 필드를 찾는다.
    """
    for key in keys:
        if key in data:
            value = scalar_value(data.get(key))
            if value:
                return value

    return ""


def walk_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    """
    JSON 전체를 재귀적으로 순회하면서 모든 dict를 반환한다.
    """
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def looks_like_product_item(item: Dict[str, Any]) -> bool:
    """
    Sellmate 주문 JSON에서 실제 판매 상품 line을 판별한다.

    실제 응답은 product/variantInfo/productClass 같은 중첩 객체를
    사용하는 경우가 있으므로 1-depth가 아니라 해당 dict 내부 전체를
    재귀적으로 검사한다.
    """
    barcode_keys = (
        "barcode", "barcode1", "barcode2", "barcode3", "barcodeNo",
        "barcode_number", "productBarcode", "product_barcode",
        "code1", "code2", "code3", "globalBarcode", "global_barcode",
        "sku", "itemCode", "item_code", "variantCode", "variant_code",
    )
    qty_keys = (
        "qty", "quantity", "sales_qty", "salesQty", "salesQuantity",
        "saleQty", "sale_qty", "orderQty", "order_qty", "sellQty",
        "sell_qty", "count", "ea", "amount", "unitQuantity",
        "unit_quantity", "number",
    )
    name_keys = (
        "product_name", "productName", "name", "itemName", "item_name",
        "goodsName", "goods_name", "productClassName", "product_class_name",
    )

    for obj in walk_dicts(item):
        barcode = find_first_value(obj, barcode_keys)
        qty = find_first_value(obj, qty_keys)
        name = find_first_value(obj, name_keys)
        if barcode and (qty or name):
            return True
        if name and qty:
            return True

    return False

def extract_product_items(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    주문 JSON 내부에서 실제 상품 항목들을 추출한다.

    Sellmate 응답 구조가 버전/계정에 따라 조금 달라도
    가능한 한 모두 대응한다.
    """
    candidates: List[Dict[str, Any]] = []
    seen_ids = set()

    preferred_keys = (
        "items",
        "order_items",
        "orderItems",
        "order_details",
        "orderDetails",
        "details",
        "products",
        "orderProducts",
        "order_products",
        "productItems",
        "product_items",
        "goods",
        "goodsItems",
        "lines",
        "lineItems",
    )

    def add_candidate(item: Any) -> None:
        if not isinstance(item, dict):
            return

        if not looks_like_product_item(item):
            return

        identity = id(item)

        if identity in seen_ids:
            return

        seen_ids.add(identity)
        candidates.append(item)

    # 1차: 명시적인 상품 배열부터 찾는다.
    for key in preferred_keys:
        value = order.get(key)

        if isinstance(value, list):
            for item in value:
                add_candidate(item)

        elif isinstance(value, dict):
            # 상품이 한 단계 더 들어있는 경우
            for child in walk_dicts(value):
                add_candidate(child)

    # 2차: 전체 JSON 재귀 탐색
    if not candidates:
        for item in walk_dicts(order):
            add_candidate(item)

    # 3차: 주문 자체가 상품 1건인 구조
    if not candidates and looks_like_product_item(order):
        candidates.append(order)

    return candidates


def extract_order_items(order: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """주문 1건에서 실제 상품 line들을 추출한다."""
    items = extract_product_items(order)
    for item in items:
        yield item

    if not items and looks_like_product_item(order):
        yield order

def find_recursive_scalar(data: Any, keys: Iterable[str]) -> str:
    """중첩 JSON 전체에서 후보 키의 첫 유효값을 찾는다."""
    keyset = set(keys)
    for obj in walk_dicts(data):
        for key in keyset:
            if key in obj:
                value = scalar_value(obj.get(key))
                if value:
                    return value
    return ""


def find_recursive_date(data: Any) -> str:
    candidates = (
        "datetime", "order_datetime", "orderDateTime", "ordered_at",
        "orderedAt", "created_at", "createdAt", "date", "order_date",
        "orderDate", "salesDate", "saleDate", "regDate", "regDatetime",
        "registeredAt", "transactionDate", "transactionDatetime",
    )
    for obj in walk_dicts(data):
        for key in candidates:
            if key in obj:
                value = scalar_value(obj.get(key))
                if parse_date(value):
                    return value
    return ""


def order_to_sales(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Sellmate order 1건을 매출데이터 행으로 변환한다.

    다양한 API 응답 필드명을 지원하며,
    주문 안에 여러 상품이 있으면 상품별로 행을 생성한다.
    """

    # --------------------------------------------------------
    # 주문일시
    # --------------------------------------------------------
    order_datetime = first_nonempty(
        scalar_value(order.get("datetime")),
        scalar_value(order.get("order_datetime")),
        scalar_value(order.get("orderDateTime")),
        scalar_value(order.get("ordered_at")),
        scalar_value(order.get("orderedAt")),
        scalar_value(order.get("created_at")),
        scalar_value(order.get("createdAt")),
        scalar_value(order.get("date")),
        scalar_value(order.get("order_date")),
        scalar_value(order.get("orderDate")),
        scalar_value(as_dict(order.get("transaction")).get("datetime")),
        scalar_value(as_dict(order.get("transaction")).get("date")),
        scalar_value(as_dict(order.get("transaction")).get("created_at")),
    )

    # 중첩된 주문 객체가 있는 경우
    if not order_datetime:
        nested_order = as_dict(order.get("order"))

        order_datetime = first_nonempty(
            scalar_value(nested_order.get("datetime")),
            scalar_value(nested_order.get("order_datetime")),
            scalar_value(nested_order.get("orderDateTime")),
            scalar_value(nested_order.get("created_at")),
            scalar_value(nested_order.get("createdAt")),
            scalar_value(nested_order.get("date")),
            scalar_value(nested_order.get("order_date")),
            scalar_value(nested_order.get("orderDate")),
        )

    order_date = parse_date(order_datetime)

    if not order_date:
        order_datetime = find_recursive_date(order)
        order_date = parse_date(order_datetime)

    if not order_date:
        print("  ⚠️ 주문일시 필드를 찾지 못해 주문 1건을 건너뜁니다.")
        return []

    # --------------------------------------------------------
    # 매장
    # --------------------------------------------------------
    order_store = as_dict(order.get("store"))

    store_name = first_nonempty(
        scalar_value(order.get("store_name")),
        scalar_value(order.get("storeName")),
        scalar_value(order.get("store_name_ko")),
        scalar_value(order.get("storeNameKo")),
        scalar_value(order_store.get("name")),
        scalar_value(order_store.get("store_name")),
        scalar_value(order_store.get("storeName")),
        scalar_value(order.get("shop_name")),
        scalar_value(order.get("shopName")),
    )

    # 중첩된 매장 정보 탐색
    if not store_name:
        for obj in walk_dicts(order):
            possible = first_nonempty(
                scalar_value(obj.get("store_name")),
                scalar_value(obj.get("storeName")),
                scalar_value(obj.get("shop_name")),
                scalar_value(obj.get("shopName")),
            )

            if possible:
                store_name = possible
                break

    if not store_name:
        store_name = norm(find_recursive_scalar(order, ("storeName", "store_name", "shopName", "shop_name")))
    store_name = norm(store_name)

    # --------------------------------------------------------
    # 영수증 번호
    # --------------------------------------------------------
    receipt = first_nonempty(
        scalar_value(order.get("receipt_number")),
        scalar_value(order.get("receiptNumber")),
        scalar_value(order.get("receipt_no")),
        scalar_value(order.get("receiptNo")),
        scalar_value(order.get("receipt")),
    )

    # --------------------------------------------------------
    # 주문번호
    # --------------------------------------------------------
    order_number = first_nonempty(
        scalar_value(order.get("order_number")),
        scalar_value(order.get("orderNumber")),
        scalar_value(order.get("order_no")),
        scalar_value(order.get("orderNo")),
        scalar_value(order.get("origin_order_number")),
        scalar_value(order.get("originOrderNumber")),
        scalar_value(order.get("orderId")),
        scalar_value(order.get("order_id")),
        scalar_value(order.get("idx")),
        scalar_value(order.get("id")),
    )

    # --------------------------------------------------------
    # 주문 상태 / 판매구분
    # --------------------------------------------------------
    order_type_raw = norm(first_nonempty(
        scalar_value(order.get("order_type")),
        scalar_value(order.get("orderType")),
        scalar_value(order.get("type")),
        scalar_value(order.get("status")),
        scalar_value(order.get("order_status")),
        scalar_value(order.get("orderStatus")),
    ))

    order_status_raw = norm(first_nonempty(
        scalar_value(order.get("status")),
        scalar_value(order.get("order_status")),
        scalar_value(order.get("orderStatus")),
        scalar_value(order.get("payment_status")),
        scalar_value(order.get("paymentStatus")),
    ))

    combined_type = (
        f"{order_type_raw} {order_status_raw}"
    ).lower()

    is_return_order = any(
        word in combined_type
        for word in (
            "반품",
            "환불",
            "취소",
            "return",
            "refund",
            "cancel",
        )
    )

    if not receipt:
        receipt = find_recursive_scalar(order, ("receiptNumber", "receipt_number", "receiptNo", "receipt_no", "receipt"))

    if not order_number:
        order_number = find_recursive_scalar(order, ("orderNumber", "order_number", "orderNo", "order_no", "orderId", "order_id", "idx", "id"))

    # --------------------------------------------------------
    # 상품 목록
    # --------------------------------------------------------
    items = list(extract_order_items(order))

    if not items:
        return []

    sales: List[Dict[str, Any]] = []

    for item_pos, item in enumerate(items, start=1):

        # ----------------------------------------------------
        # 바코드
        # ----------------------------------------------------
        # 실제 Sellmate 응답은 variantInfo/productClass 등 여러 단계로
        # 상품 정보가 들어올 수 있으므로 item 전체를 재귀 탐색한다.
        barcode = find_recursive_scalar(
            item,
            (
                "barcode", "barcode1", "barcode2", "barcode3", "barcodeNo",
                "barcode_number", "productBarcode", "product_barcode",
                "code1", "code2", "code3", "globalBarcode", "global_barcode",
                "sku", "itemCode", "item_code", "variantCode", "variant_code",
            ),
        )

        if not barcode:
            continue

        # ----------------------------------------------------
        # 수량
        # ----------------------------------------------------
        qty_raw = find_recursive_scalar(
            item,
            (
                "qty", "quantity", "sales_qty", "salesQty", "salesQuantity",
                "saleQty", "sale_qty", "orderQty", "order_qty", "sellQty",
                "sell_qty", "count", "ea", "amount", "unitQuantity",
                "unit_quantity", "number",
            ),
        )

        try:
            qty = int(float(qty_raw or 0))
        except (ValueError, TypeError):
            qty = 0

        if qty == 0:
            continue

        # ----------------------------------------------------
        # 상품명
        # ----------------------------------------------------
        name = find_recursive_scalar(
            item,
            (
                "product_name", "productName", "itemName", "item_name",
                "goodsName", "goods_name", "productClassName",
                "product_class_name", "name",
            ),
        )

        # ----------------------------------------------------
        # 옵션명
        # ----------------------------------------------------
        option = find_recursive_scalar(
            item,
            (
                "option_name", "optionName", "option",
                "variant_option_name", "variantOptionName",
            ),
        )

        # ----------------------------------------------------
        # 상품별 판매구분
        # ----------------------------------------------------
        item_type_raw = norm(first_nonempty(
            scalar_value(item.get("order_type")),
            scalar_value(item.get("orderType")),
            scalar_value(item.get("type")),
            scalar_value(item.get("status")),
            scalar_value(item.get("order_status")),
            scalar_value(item.get("orderStatus")),
            scalar_value(item.get("sale_type")),
            scalar_value(item.get("saleType")),
        ))

        item_combined_type = (
            f"{combined_type} {item_type_raw}"
        ).lower()

        is_return = is_return_order or any(
            word in item_combined_type
            for word in (
                "반품",
                "환불",
                "취소",
                "return",
                "refund",
                "cancel",
            )
        )

        # 수량이 API에서 음수로 내려오는 경우
        # 판매구분은 판매/반품으로 정규화하고 절대값을 저장한다.
        if qty < 0:
            is_return = True
            qty = abs(qty)

        if qty <= 0:
            continue

        # ----------------------------------------------------
        # 상품 순번 / 상품 ID
        # ----------------------------------------------------
        item_idx = first_nonempty(
            scalar_value(item.get("idx")),
            scalar_value(item.get("item_idx")),
            scalar_value(item.get("itemIdx")),
            scalar_value(item.get("order_item_idx")),
            scalar_value(item.get("orderItemIdx")),
            scalar_value(item.get("line_no")),
            scalar_value(item.get("lineNo")),
            scalar_value(item_pos),
        )

        # ----------------------------------------------------
        # 주문번호가 없는 특수 응답 대응
        # ----------------------------------------------------
        final_order_number = order_number

        if not final_order_number:
            final_order_number = first_nonempty(
                scalar_value(item.get("order_number")),
                scalar_value(item.get("orderNumber")),
                scalar_value(item.get("order_no")),
                scalar_value(item.get("orderNo")),
            )

        # ----------------------------------------------------
        # 매장도 상품 내부에 존재할 수 있음
        # ----------------------------------------------------
        final_store = store_name

        if not final_store:
            item_store = as_dict(item.get("store"))

            final_store = norm(first_nonempty(
                scalar_value(item.get("store_name")),
                scalar_value(item.get("storeName")),
                scalar_value(item_store.get("name")),
                scalar_value(item_store.get("store_name")),
                scalar_value(item_store.get("storeName")),
            ))

        sales.append({
            "date": order_date.strftime("%Y-%m-%d"),
            "store": final_store,
            "barcode": str(barcode).strip(),
            "name": name,
            "option": option,
            "qty": qty,
            "receipt": receipt,
            "order_number": str(final_order_number).strip(),
            "item_idx": str(item_idx).strip(),
            "sale_type": "반품" if is_return else "판매",
            "datetime": order_datetime,
        })

    return sales


def sale_key(sale: Dict[str, Any]) -> Tuple[str, ...]:
    return (
        sale.get("date", ""),
        sale.get("store", ""),
        sale.get("barcode", ""),
        sale.get("order_number", ""),
        sale.get("item_idx", ""),
        sale.get("sale_type", "판매"),
    )


def load_existing_sale_keys(ws: gspread.Worksheet) -> set:
    print("🔎 기존 매출 데이터 확인 중...")
    values = ws.get_all_values()
    if not values:
        print("  기존 매출 데이터: 0건")
        return set()

    header = values[0]
    aliases = {
        "date": "날짜",
        "store": "매장",
        "barcode": "바코드",
        "order": "주문번호",
        "item": "상품순번",
        "type": "판매구분",
    }
    if not all(v in header for v in aliases.values()):
        print("  ⚠️ 기존 매출 헤더가 구형 구조이므로 중복 KEY를 완전하게 만들 수 없습니다.")
        return set()

    idx = {k: header.index(v) for k, v in aliases.items()}
    keys = set()
    for row in values[1:]:
        try:
            keys.add((
                str(row[idx["date"]]).strip(),
                str(row[idx["store"]]).strip(),
                str(row[idx["barcode"]]).strip(),
                str(row[idx["order"]]).strip(),
                str(row[idx["item"]]).strip(),
                str(row[idx["type"]]).strip() or "판매",
            ))
        except IndexError:
            continue
    print(f"  기존 매출 데이터: {len(keys):,}건")
    return keys


def append_sales_page(ws: gspread.Worksheet, sales: List[Dict[str, Any]], seen: set) -> int:
    new_rows = []
    for sale in sales:
        key = sale_key(sale)
        if key in seen:
            continue
        seen.add(key)
        new_rows.append([
            sale.get("date", ""),
            sale.get("store", ""),
            sale.get("barcode", ""),
            sale.get("name", ""),
            sale.get("option", ""),
            sale.get("qty", 0),
            sale.get("receipt", ""),
            sale.get("order_number", ""),
            sale.get("item_idx", ""),
            sale.get("sale_type", "판매"),
            sale.get("datetime", ""),
        ])

    if not new_rows:
        return 0

    ws.append_rows(new_rows, value_input_option="RAW")
    return len(new_rows)


# ============================================================
# Checkpoint
# ============================================================

def get_checkpoint_ws() -> gspread.Worksheet:
    sh = open_sheet()
    ws = ensure_worksheet(sh, CHECKPOINT_SHEET, 1000, len(CHECKPOINT_HEADER))
    ensure_header(ws, CHECKPOINT_HEADER)
    return ws


def checkpoint_get(task_key: str, start_date: date, end_date: date) -> int:
    ws = get_checkpoint_ws()
    rows = ws.get_all_values()
    if not rows:
        return 1
    for row in rows[1:]:
        if len(row) < 6:
            continue
        if row[0] == task_key and row[1] == start_date.isoformat() and row[2] == end_date.isoformat():
            try:
                page = int(row[3])
                if row[4] == "완료":
                    return 1
                return max(1, page)
            except (ValueError, TypeError):
                return 1
    return 1


def checkpoint_save(task_key: str, start_date: date, end_date: date, next_page: int, status: str) -> None:
    ws = get_checkpoint_ws()
    rows = ws.get_all_values()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target = None
    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= 3 and row[0] == task_key and row[1] == start_date.isoformat() and row[2] == end_date.isoformat():
            target = i
            break

    new_row = [task_key, start_date.isoformat(), end_date.isoformat(), next_page, status, now]
    if target:
        ws.update([new_row], f"A{target}:F{target}")
    else:
        ws.append_row(new_row, value_input_option="RAW")


# ============================================================
# Orders API
# ============================================================

def get_sales_page(
    session: requests.Session,
    page: int,
    start_date: date,
    end_date: date,
) -> Tuple[List[Dict[str, Any]], int]:
    url = f"{EXTERNAL_BASE_URL}/external/{SELLMATE_DOMAIN}/order"
    params = {
        "page": page,
        "perPage": PER_PAGE,
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
    }

    last_error = ""
    for attempt in range(1, API_RETRY_COUNT + 1):
        try:
            res = session.get(url, params=params, timeout=90)
            print(
                f"  📡 매출 API page={page} "
                f"기간={start_date}~{end_date} 응답: {res.status_code}"
            )
            if res.status_code == 200:
                payload = res.json()
                orders = find_list_payload(payload)
                last_page = get_last_page(payload, len(orders))
                if len(orders) == PER_PAGE:
                    # 문서의 meta가 없더라도 다음 페이지를 계속 탐색할 수 있도록 함.
                    last_page = max(last_page, page + 1)
                return orders, last_page

            last_error = f"{res.status_code} {res.text[:500]}"
            if res.status_code in (401, 403):
                raise RuntimeError(last_error)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = str(exc)
        if attempt < API_RETRY_COUNT:
            time.sleep(attempt * 3)
    fail(f"매출 API 조회 실패 (page={page}): {last_error}")


def sync_sales(session: requests.Session, sales_ws: gspread.Worksheet, existing_keys: set) -> bool:
    today = get_today()
    if existing_keys:
        first_start = today - timedelta(days=SALES_RANGE_DAYS - 1)
        print(f"🔄 증분 보정 조회: {first_start} ~ {today}")
    else:
        try:
            first_start = datetime.strptime(SALES_HISTORY_START, "%Y-%m-%d").date()
        except ValueError:
            fail(f"SALES_HISTORY_START_DATE 형식 오류: {SALES_HISTORY_START}")
        print(f"🆕 최초 전체 이력 수집: {first_start} ~ {today}")

    seen = set(existing_keys)
    current = first_start
    total_new = 0

    while current <= today:
        end = min(current + timedelta(days=SALES_RANGE_DAYS - 1), today)
        task_key = f"sales:{SELLMATE_DOMAIN}"
        page = checkpoint_get(task_key, current, end)
        checkpoint_save(task_key, current, end, page, "진행중")

        print("================================")
        print(f"📅 기간 조회: {current} ~ {end}")
        print(f"  ▶ 재개 페이지: {page}")

        range_orders = 0
        range_new = 0
        while True:
            orders, last_page = get_sales_page(session, page, current, end)
            if not orders:
                checkpoint_save(task_key, current, end, 1, "완료")
                break

            range_orders += len(orders)
            sales = []

            for order_index, order in enumerate(orders):
                converted = order_to_sales(order)
                sales.extend(converted)

                # 첫 페이지에서 변환 실패한 주문 구조를 확인하기 위한 진단
                if page == 1 and order_index < 3 and not converted:
                    print("  ⚠️ 주문 변환 실패 샘플:")
                    print(
                        json.dumps(
                            order,
                            ensure_ascii=False,
                            default=str,
                        )[:3000]
                    )

            new_count = append_sales_page(sales_ws, sales, seen)
            range_new += new_count
            total_new += new_count
            print(
                f"  📄 page {page} / 주문 {len(orders):,}건 / "
                f"변환 {len(sales):,}건 / 신규 {new_count:,}건"
            )

            next_page = page + 1
            if len(orders) < PER_PAGE:
                checkpoint_save(task_key, current, end, 1, "완료")
                break
            if last_page > 0 and page >= last_page:
                checkpoint_save(task_key, current, end, 1, "완료")
                break

            checkpoint_save(task_key, current, end, next_page, "진행중")
            page = next_page

        print(f"  ✅ 기간 완료: 주문 {range_orders:,}건 / 신규 {range_new:,}건")
        current = end + timedelta(days=1)

    print("================================")
    print(f"📥 이번 실행 신규 매출 저장: {total_new:,}건")
    return True


# ============================================================
# 7-day velocity
# ============================================================

def calculate_7day_velocity() -> None:
    sh = open_sheet()
    try:
        sales_ws = sh.worksheet(SALES_SHEET)
    except gspread.WorksheetNotFound:
        print("⚠️ 매출데이터 시트가 없어 판매속도 계산을 건너뜁니다.")
        return

    ws = ensure_worksheet(sh, VELOCITY_SHEET, 10000, len(VELOCITY_HEADER))
    ensure_header(ws, VELOCITY_HEADER)

    values = sales_ws.get_all_values()
    if len(values) <= 1:
        print("⚠️ 매출 데이터가 없어 판매속도 계산을 건너뜁니다.")
        return

    header = values[0]
    required = ["날짜", "매장", "바코드", "상품명", "옵션명", "판매수량", "판매구분"]
    if not all(x in header for x in required):
        print("⚠️ 판매속도 계산에 필요한 헤더가 없습니다.")
        return

    idx = {x: header.index(x) for x in required}
    today = get_today()
    start = today - timedelta(days=SALES_AVERAGE_DAYS - 1)
    summary: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for row in values[1:]:
        if len(row) <= max(idx.values()):
            continue
        d = parse_date(row[idx["날짜"]])
        if not d or d < start or d > today:
            continue
        store = row[idx["매장"]].strip()
        barcode = row[idx["바코드"]].strip()
        if not store or not barcode:
            continue
        try:
            qty = int(float(row[idx["판매수량"]] or 0))
        except (ValueError, TypeError):
            qty = 0
        if row[idx["판매구분"]].strip() == "반품":
            qty = -qty

        key = (store, barcode)
        if key not in summary:
            summary[key] = {
                "store": store,
                "barcode": barcode,
                "name": row[idx["상품명"]],
                "option": row[idx["옵션명"]],
                "qty": 0,
            }
        summary[key]["qty"] += qty

    output = [VELOCITY_HEADER]
    for item in sorted(summary.values(), key=lambda x: (x["store"], x["barcode"])):
        qty = int(item["qty"])
        output.append([
            today.isoformat(),
            f"{start.isoformat()} ~ {today.isoformat()}",
            item["store"],
            item["barcode"],
            item["name"],
            item["option"],
            qty,
            round(qty / SALES_AVERAGE_DAYS, 2),
            SALES_AVERAGE_DAYS,
        ])

    ws.clear()
    ws.update(output[:1], "A1")
    for offset in range(1, len(output), 5000):
        chunk = output[offset:offset + 5000]
        start_row = offset + 1
        end_row = start_row + len(chunk) - 1
        ws.update(chunk, f"A{start_row}:I{end_row}")

    print(f"📈 판매속도 {len(output) - 1:,}개 상품 계산 완료")


# ============================================================
# Daily log
# ============================================================

def check_daily_done() -> bool:
    if FORCE_SYNC:
        print("⚡ 강제 실행 모드")
        return False
    sh = open_sheet()
    ws = ensure_worksheet(sh, SYNC_LOG_SHEET, 100, len(SYNC_LOG_HEADER))
    ensure_header(ws, SYNC_LOG_HEADER)
    values = ws.get_all_values()
    today = get_today().isoformat()
    for row in values[1:]:
        if row and row[0] == today and len(row) >= 3 and row[2] == "완료":
            print("⏭️ 오늘 이미 동기화가 완료되어 종료합니다.")
            return True
    return False


def write_daily_log(stock_ok: bool, sales_ok: bool) -> None:
    sh = open_sheet()
    ws = ensure_worksheet(sh, SYNC_LOG_SHEET, 100, len(SYNC_LOG_HEADER))
    ensure_header(ws, SYNC_LOG_HEADER)
    ws.append_row([
        get_today().isoformat(),
        "완료" if stock_ok else "건너뜀",
        "완료" if sales_ok else "실패",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ], value_input_option="RAW")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print("========================================")
    print("🚀 헤트라스 셀메이트 EXTERNAL API 동기화 시작")
    print("========================================")
    validate_config()

    if check_daily_done():
        return

    session = issue_external_token()
    store_map = get_store_list(session)

    stock_ok = sync_stock(session, store_map)
    if not stock_ok:
        print("⚠️ 재고 동기화는 건너뜁니다. 매출 동기화는 계속 진행합니다.")

    sh = open_sheet()
    sales_ws = ensure_worksheet(sh, SALES_SHEET, 200000, len(SALES_HEADER))
    ensure_header(sales_ws, SALES_HEADER)
    existing_keys = load_existing_sale_keys(sales_ws)

    sales_ok = False
    try:
        sales_ok = sync_sales(session, sales_ws, existing_keys)
    except Exception as exc:
        print(f"⚠️ 매출 동기화 실패: {exc}")
        print("ℹ️ 체크포인트가 저장되어 다음 실행에서 중단된 페이지부터 재개합니다.")

    if sales_ok:
        calculate_7day_velocity()
        write_daily_log(stock_ok, True)
        print("========================================")
        print("✅ 매출 동기화 및 7일 판매속도 계산 완료")
        print("========================================")
    else:
        print("========================================")
        print("⚠️ 매출이 완료되지 않아 오늘 완료 로그를 기록하지 않습니다.")
        print("========================================")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
