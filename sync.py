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
SALES_HISTORY_START = os.environ.get("SALES_HISTORY_START_DATE", "2000-01-01")
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
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
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
    """External API의 data 구조가 배열/중첩객체인 경우 최대한 안전하게 추출."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("data", "items", "results", "orders", "stocks"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = find_list_payload(value)
            if nested:
                return nested
    return []


def get_last_page(payload: Any, item_count: int) -> int:
    if isinstance(payload, dict):
        for container in (
            payload,
            as_dict(payload.get("meta")),
            as_dict(payload.get("pagination")),
            as_dict(payload.get("links")),
        ):
            for key in ("last_page", "lastPage", "total_pages", "totalPages", "pageCount"):
                value = container.get(key)
                if value not in (None, ""):
                    try:
                        return max(1, int(value))
                    except (ValueError, TypeError):
                        pass
    return 1 if item_count == 0 else 1


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
        ws.update("A1", [header])
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
        ws.update("A1", [merged])


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
    ws.update("A1", [STOCK_HEADER])
    combined = keep + all_rows
    for offset in range(0, len(combined), 5000):
        chunk = combined[offset:offset + 5000]
        start = offset + 2
        end = start + len(chunk) - 1
        ws.update(f"A{start}:F{end}", chunk)

    print(f"✅ 재고 {len(all_rows):,}건 저장 완료")
    return True


# ============================================================
# Sales normalization
# ============================================================

def extract_order_items(order: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for key in ("items", "order_items", "orderItems", "products", "details"):
        value = order.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item
            return
    # 상세 구조가 상품 1건짜리 주문인 경우
    yield order


def order_to_sales(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    order_date = parse_date(first_nonempty(
        order.get("datetime"),
        order.get("order_datetime"),
        order.get("created_at"),
        order.get("ordered_at"),
        order.get("date"),
    ))
    if not order_date:
        return []

    order_store = as_dict(order.get("store"))
    store_name = norm(first_nonempty(
        order.get("store_name"),
        order.get("storeName"),
        order_store.get("name"),
        order_store.get("store_name"),
    ))
    receipt = first_nonempty(order.get("receipt_number"), order.get("receiptNumber"), order.get("receipt"))
    order_number = first_nonempty(
        order.get("order_number"),
        order.get("orderNumber"),
        order.get("order_no"),
        order.get("origin_order_number"),
        order.get("idx"),
    )
    order_type_raw = norm(first_nonempty(
        order.get("order_type"),
        order.get("orderType"),
        order.get("type"),
    ))
    is_return = any(word in order_type_raw.lower() for word in ("반품", "환불", "취소", "return", "refund", "cancel"))

    sales = []
    for item_pos, item in enumerate(extract_order_items(order), start=1):
        barcode = first_nonempty(
            item.get("barcode"),
            item.get("code1"),
            item.get("barcode1"),
            as_dict(item.get("variant")).get("barcode"),
        )
        if not barcode:
            continue

        qty_raw = first_nonempty(
            item.get("qty"),
            item.get("quantity"),
            item.get("sales_qty"),
            item.get("count"),
        )
        try:
            qty = int(float(qty_raw or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty <= 0:
            continue

        item_type_raw = norm(first_nonempty(item.get("order_type"), item.get("type")))
        item_is_return = is_return or any(word in item_type_raw.lower() for word in ("반품", "환불", "취소", "return", "refund", "cancel"))

        item_idx = first_nonempty(
            item.get("idx"),
            item.get("item_idx"),
            item.get("order_item_idx"),
            item_pos,
        )
        name = first_nonempty(item.get("product_name"), item.get("productName"), item.get("name"))
        option = first_nonempty(item.get("option_name"), item.get("optionName"), item.get("option"), item.get("variant_option_name"))

        sales.append({
            "date": order_date.strftime("%Y-%m-%d"),
            "store": store_name,
            "barcode": str(barcode).strip(),
            "name": name,
            "option": option,
            "qty": qty,
            "receipt": receipt,
            "order_number": str(order_number).strip(),
            "item_idx": str(item_idx).strip(),
            "sale_type": "반품" if item_is_return else "판매",
            "datetime": first_nonempty(order.get("datetime"), order.get("order_datetime"), order.get("created_at")),
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
        ws.update(f"A{target}:F{target}", [new_row])
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
            for order in orders:
                sales.extend(order_to_sales(order))

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
    ws.update("A1", output[:1])
    for offset in range(1, len(output), 5000):
        chunk = output[offset:offset + 5000]
        start_row = offset + 1
        end_row = start_row + len(chunk) - 1
        ws.update(f"A{start_row}:I{end_row}", chunk)

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
