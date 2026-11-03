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
CODE_MAP_SHEET = "코드매핑"

# 실행 중 Sellmate 응답에서 자동 수집되는 구형바코드 -> P코드 매핑
CODE_ALIAS_MAP: Dict[str, str] = {}
CODE_ALIAS_CONFLICTS = set()

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

        # P코드를 최우선 사용. P코드가 없을 때만 기존 88 바코드 fallback.
        barcode = find_product_code(item)
        if not barcode:
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

    # Sellmate External API 실제 주문 응답은 상품 라인을
    # order.ordered_unit 배열에 넣어 내려준다.
    # 이 키를 최우선으로 처리해야 주문 전체 JSON을 상품으로
    # 오인하거나 다른 중첩 배열을 잘못 선택하는 문제가 없다.
    preferred_keys = (
        "ordered_unit",
        "ordered_units",
        "orderedUnit",
        "orderedUnits",
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
                # Sellmate External API의 실제 상품 line
                # ordered_unit은 여기서 바로 상품 후보로 사용한다.
                if key in ("ordered_unit", "ordered_units", "orderedUnit", "orderedUnits") and isinstance(item, dict):
                    if item not in candidates:
                        candidates.append(item)
                else:
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
    """중첩 JSON 전체에서 후보 키 우선순서를 지켜 첫 유효값을 찾는다."""
    keylist = list(keys)
    for key in keylist:
        for obj in walk_dicts(data):
            if key in obj:
                value = scalar_value(obj.get(key))
                if value:
                    return value
    return ""


def is_p_code(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.upper().startswith("P")


def _scalar_codes(value: Any) -> List[str]:
    """코드 필드가 문자열/숫자/dict 어느 형태든 실제 코드값들을 뽑는다."""
    out: List[str] = []

    def add(v: Any) -> None:
        if isinstance(v, (str, int, float)):
            s = str(v).strip()
            if s and s not in out:
                out.append(s)

    if isinstance(value, dict):
        for key in (
            "code", "code1", "code2", "code3", "code4", "code5",
            "itemCode", "item_code", "variantCode", "variant_code",
            "sku", "productCode", "product_code",
        ):
            add(value.get(key))
    else:
        add(value)

    return out


def register_code_alias(old_code: str, p_code: str) -> None:
    """
    88/구형바코드 -> P코드 매핑 저장.
    같은 구형코드가 서로 다른 P코드와 연결되면 안전을 위해 충돌 처리한다.
    """
    old_code = str(old_code or "").strip()
    p_code = str(p_code or "").strip()

    if not old_code or not p_code or old_code == p_code:
        return
    if is_p_code(old_code) or not is_p_code(p_code):
        return
    if old_code in CODE_ALIAS_CONFLICTS:
        return

    existing = CODE_ALIAS_MAP.get(old_code)
    if existing and existing != p_code:
        CODE_ALIAS_MAP.pop(old_code, None)
        CODE_ALIAS_CONFLICTS.add(old_code)
        print(f"⚠️ 코드매핑 충돌 제외: {old_code} -> {existing} / {p_code}")
        return

    CODE_ALIAS_MAP[old_code] = p_code


def collect_product_codes(data: Any) -> Tuple[str, List[str]]:
    """
    Sellmate 상품 line 하나에서
    현재 P코드와 함께 존재하는 기존 barcode/code1 값들을 동시에 수집한다.
    """
    p_code = ""

    # P코드는 상품/variant 코드 계열을 우선
    p_keys = (
        "itemCode", "item_code",
        "variantCode", "variant_code",
        "sku",
        "productCode", "product_code",
        "goodsCode", "goods_code",
    )

    for key in p_keys:
        if p_code:
            break
        for obj in walk_dicts(data):
            if key in obj:
                for value in _scalar_codes(obj.get(key)):
                    if is_p_code(value):
                        p_code = value
                        break
            if p_code:
                break

    # 일반 code 필드에 P코드가 있는 응답 보조
    if not p_code:
        for obj in walk_dicts(data):
            for key in ("code", "code0", "code4", "code5"):
                if key in obj:
                    for value in _scalar_codes(obj.get(key)):
                        if is_p_code(value):
                            p_code = value
                            break
                if p_code:
                    break
            if p_code:
                break

    legacy_codes: List[str] = []
    legacy_keys = (
        "barcode", "barcode1", "barcode2", "barcode3", "barcodeNo",
        "barcode_number", "productBarcode", "product_barcode",
        "code1", "code2", "code3",
        "globalBarcode", "global_barcode",
    )

    for obj in walk_dicts(data):
        for key in legacy_keys:
            if key not in obj:
                continue
            for value in _scalar_codes(obj.get(key)):
                if value and not is_p_code(value) and value not in legacy_codes:
                    legacy_codes.append(value)

    if p_code:
        for old in legacy_codes:
            register_code_alias(old, p_code)

    return p_code, legacy_codes


def find_product_code(data: Any) -> str:
    """
    최종 상품코드:
    1) P코드 최우선
    2) P코드가 없을 때만 기존 barcode/code1 fallback

    동시에 같은 상품 line 안의 88코드 <-> P코드 대응을 CODE_ALIAS_MAP에 수집한다.
    """
    p_code, legacy_codes = collect_product_codes(data)
    if p_code:
        return p_code
    return legacy_codes[0] if legacy_codes else ""


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
    terminal_obj = as_dict(order.get("terminal"))
    terminal_store = as_dict(terminal_obj.get("store"))

    store_name = first_nonempty(
        scalar_value(order.get("store_name")),
        scalar_value(order.get("storeName")),
        scalar_value(order.get("store_name_ko")),
        scalar_value(order.get("storeNameKo")),
        scalar_value(order_store.get("name")),
        scalar_value(order_store.get("store_name")),
        scalar_value(order_store.get("storeName")),
        scalar_value(terminal_store.get("name")),
        scalar_value(terminal_store.get("store_name")),
        scalar_value(terminal_store.get("storeName")),
        scalar_value(terminal_obj.get("store_name")),
        scalar_value(terminal_obj.get("storeName")),
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
    receipt_obj = as_dict(order.get("receipt"))
    receipt = first_nonempty(
        scalar_value(order.get("receipt_number")),
        scalar_value(order.get("receiptNumber")),
        scalar_value(order.get("receipt_no")),
        scalar_value(order.get("receiptNo")),
        scalar_value(receipt_obj.get("number")),
        scalar_value(receipt_obj.get("receipt_number")),
        scalar_value(receipt_obj.get("receiptNumber")),
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
        # 상품코드
        # ----------------------------------------------------
        # 셀메이트의 신규 P코드를 최우선으로 사용하고,
        # P코드가 없는 상품만 기존 88 바코드를 fallback으로 사용한다.
        barcode = find_product_code(item)

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
# 88코드 <-> P코드 영구 매핑
# ============================================================

def load_code_alias_map() -> None:
    """
    이전 실행에서 수집한 코드매핑 시트를 읽어 이번 실행의 메모리 매핑에 합친다.
    """
    sh = open_sheet()
    try:
        ws = sh.worksheet(CODE_MAP_SHEET)
    except gspread.WorksheetNotFound:
        print("ℹ️ 코드매핑 시트 없음 - 이번 실행에서 새로 생성합니다.")
        return

    values = ws.get_all_values()
    if len(values) <= 1:
        return

    loaded = 0
    for row in values[1:]:
        if len(row) < 2:
            continue
        old_code = str(row[0] or "").strip()
        p_code = str(row[1] or "").strip()
        before = CODE_ALIAS_MAP.get(old_code)
        register_code_alias(old_code, p_code)
        if old_code in CODE_ALIAS_MAP and CODE_ALIAS_MAP.get(old_code) != before:
            loaded += 1

    print(f"🧭 기존 코드매핑 {len(CODE_ALIAS_MAP):,}건 로드")


def save_code_alias_map() -> None:
    """
    이번 실행에서 새로 발견한 매핑까지 코드매핑 시트에 안전하게 덮어쓴다.
    충돌난 코드는 저장하지 않는다.
    """
    sh = open_sheet()
    ws = ensure_worksheet(sh, CODE_MAP_SHEET, 5000, 3)

    header = ["구형바코드", "P코드", "수정시간"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = [
        [old_code, p_code, now]
        for old_code, p_code in sorted(CODE_ALIAS_MAP.items())
        if old_code not in CODE_ALIAS_CONFLICTS
    ]

    ws.clear()
    ws.update([header], "A1")
    if rows:
        for offset in range(0, len(rows), 5000):
            chunk = rows[offset:offset + 5000]
            start_row = offset + 2
            end_row = start_row + len(chunk) - 1
            ws.update(chunk, f"A{start_row}:C{end_row}")

    print(f"💾 코드매핑 {len(rows):,}건 저장 완료")


def code_map_needs_bootstrap() -> bool:
    """
    오늘 이미 동기화 완료 상태여도 코드매핑 시트가 아직 없거나 비어 있으면
    이번 패치 적용 첫 실행은 계속 진행하도록 한다.
    """
    sh = open_sheet()
    try:
        ws = sh.worksheet(CODE_MAP_SHEET)
        return len(ws.get_all_values()) < 2
    except Exception:
        return True


# ============================================================
# 상품마스터 P코드 보정
# ============================================================

def normalize_product_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    for ch in (" ", "_", "-", "/", "(", ")", "[", "]"):
        text = text.replace(ch, "")
    return text


def load_product_master_pcode_map() -> Dict[str, str]:
    """
    상품마스터의 상품명 -> P코드 매핑.
    과거 매출데이터에 88코드가 남아 있어도 판매속도는 즉시 P코드로 표시되도록 보정한다.
    """
    sh = open_sheet()
    try:
        ws = sh.worksheet("상품마스터")
    except gspread.WorksheetNotFound:
        return {}

    values = ws.get_all_values()
    if len(values) <= 1:
        return {}

    header = [str(x).strip() for x in values[0]]
    if "바코드" not in header or "상품명" not in header:
        return {}

    bi = header.index("바코드")
    ni = header.index("상품명")
    result: Dict[str, str] = {}

    for row in values[1:]:
        if len(row) <= max(bi, ni):
            continue
        code = str(row[bi] or "").strip()
        name = str(row[ni] or "").strip()
        if code and name and is_p_code(code):
            result[normalize_product_name(name)] = code

    print(f"🧭 상품마스터 P코드 매핑 {len(result):,}건 로드")
    return result


# ============================================================
# 7-day velocity
# ============================================================

def normalize_product_option(value: Any) -> str:
    """옵션명 비교용 정규화: 공백/대소문자 차이만 제거."""
    return " ".join(str(value or "").strip().lower().split())


def build_sales_pair_pcode_map(values: List[List[Any]], idx: Dict[str, int]) -> Tuple[Dict[Tuple[str, str], str], Dict[str, str]]:
    """
    매출데이터에 이미 존재하는 P코드 행을 이용해
    (상품명 + 옵션명) -> P코드 매핑을 만든다.

    같은 상품명+옵션명에 P코드가 2개 이상 존재하면 잘못 합칠 위험이 있으므로
    그 조합은 자동 매핑하지 않는다.

    이름만으로도 P코드가 정확히 1개뿐인 상품은 name_map으로 보조 매핑한다.
    텍스타일퍼퓸처럼 같은 상품명에 여러 옵션/P코드가 있으면 이름만으로는 절대 매핑하지 않는다.
    """
    pair_candidates: Dict[Tuple[str, str], set] = {}
    name_candidates: Dict[str, set] = {}

    for row in values[1:]:
        if len(row) <= max(idx.values()):
            continue

        barcode = str(row[idx["바코드"]] or "").strip()
        if not is_p_code(barcode):
            continue

        name_key = normalize_product_name(row[idx["상품명"]])
        option_key = normalize_product_option(row[idx["옵션명"]])

        if not name_key:
            continue

        pair_key = (name_key, option_key)
        pair_candidates.setdefault(pair_key, set()).add(barcode)
        name_candidates.setdefault(name_key, set()).add(barcode)

    pair_map: Dict[Tuple[str, str], str] = {
        key: next(iter(codes))
        for key, codes in pair_candidates.items()
        if len(codes) == 1
    }

    name_map: Dict[str, str] = {
        key: next(iter(codes))
        for key, codes in name_candidates.items()
        if len(codes) == 1
    }

    ambiguous_pairs = sum(1 for codes in pair_candidates.values() if len(codes) > 1)
    print(
        f"🧭 매출 P코드 매핑: 상품+옵션 {len(pair_map):,}건 / "
        f"이름 단일매핑 {len(name_map):,}건 / 애매한 조합 {ambiguous_pairs:,}건"
    )
    return pair_map, name_map


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

    # 핵심:
    # 같은 상품명에 여러 SKU가 있는 상품은 "상품명"만으로 매핑하면 오매핑된다.
    # 따라서 기존 매출데이터의 P코드 행을 기반으로 "상품명 + 옵션명" 매핑을 최우선 사용한다.
    pair_pcode_map, unique_name_pcode_map = build_sales_pair_pcode_map(values, idx)

    remapped_count = 0
    unmapped_legacy = {}

    for row in values[1:]:
        if len(row) <= max(idx.values()):
            continue

        d = parse_date(row[idx["날짜"]])
        if not d or d < start or d > today:
            continue

        store = row[idx["매장"]].strip()
        barcode = row[idx["바코드"]].strip()
        product_name = row[idx["상품명"]].strip()
        option = row[idx["옵션명"]].strip()

        # 과거 88코드 → 현재 P코드 보정
        # 1순위: 상품명 + 옵션명 정확 매핑
        # 2순위: 해당 상품명에 P코드가 딱 1개뿐인 경우에만 이름 단독 매핑
        if barcode and not is_p_code(barcode):
            # 1순위: Sellmate 동일 상품 line에서 직접 수집한 정확한 88 -> P 매핑
            mapped = CODE_ALIAS_MAP.get(barcode, "")

            # 2순위: 상품명 + 옵션명 조합
            if not mapped:
                name_key = normalize_product_name(product_name)
                option_key = normalize_product_option(option)
                mapped = pair_pcode_map.get((name_key, option_key), "")

                # 3순위: 상품명에 P코드가 딱 1개뿐인 경우만 보조
                if not mapped:
                    mapped = unique_name_pcode_map.get(name_key, "")

            if mapped:
                barcode = mapped
                remapped_count += 1
            else:
                unmapped_legacy[(barcode, product_name, option)] = True

        if not store or not barcode:
            continue

        try:
            qty = int(float(row[idx["판매수량"]] or 0))
        except (ValueError, TypeError):
            qty = 0

        if row[idx["판매구분"]].strip() == "반품":
            qty = -qty

        # 88코드와 P코드가 같은 P코드로 보정되면 여기서 자동 합산됨
        key = (store, barcode)
        if key not in summary:
            summary[key] = {
                "store": store,
                "barcode": barcode,
                "name": product_name,
                "option": option,
                "qty": 0,
            }

        summary[key]["qty"] += qty

        # P코드 행에 옵션명이 있으면 화면용 옵션도 보강
        if option and not summary[key].get("option"):
            summary[key]["option"] = option

    output = [VELOCITY_HEADER]
    legacy_velocity = []

    for item in sorted(summary.values(), key=lambda x: (x["store"], x["barcode"])):
        if not is_p_code(item["barcode"]):
            legacy_velocity.append(
                (item["store"], item["barcode"], item["name"], item.get("option", ""))
            )

        qty = int(item["qty"])
        output.append([
            today.isoformat(),
            f"{start.isoformat()} ~ {today.isoformat()}",
            item["store"],
            item["barcode"],
            item["name"],
            item.get("option", ""),
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

    print(f"🔄 최근 7일 88코드 → P코드 보정 {remapped_count:,}행")
    print(f"📈 판매속도 {len(output) - 1:,}개 상품 계산 완료")

    if legacy_velocity:
        print(f"⚠️ 판매속도에 P코드로 보정하지 못한 상품 {len(legacy_velocity):,}건")
        for item in legacy_velocity[:30]:
            print("   fallback:", item)
    else:
        print("✅ 판매속도 바코드 100% P코드 확인")


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

    # 과거 실행에서 수집한 88 -> P 매핑을 먼저 로드
    load_code_alias_map()

    already_done = check_daily_done()
    if already_done and not code_map_needs_bootstrap():
        return
    if already_done:
        print("🔄 코드매핑 최초 구축을 위해 오늘 완료 상태여도 1회 재조회합니다.")

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
        # Sellmate 주문/재고 응답에서 수집한 정확한 88 -> P 매핑 저장
        save_code_alias_map()
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
