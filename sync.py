
# =====================================================
# 헤트라스 셀메이트 자동 동기화 스크립트
# =====================================================

import os
import json
import urllib.parse
import requests
import gspread

from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials


# =====================================================
# 환경변수
# =====================================================

SELLMATE_ID = os.environ["SELLMATE_ID"]
SELLMATE_PW = os.environ["SELLMATE_PW"]
SELLMATE_DOMAIN = os.environ.get("SELLMATE_DOMAIN", "hetras")
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDS = json.loads(os.environ["GOOGLE_CREDENTIALS"])

# 셀메이트 현재 JS 버전
SELLMATE_JS_VERSION = "2.8.4"


# =====================================================
# API
# =====================================================

BASE_URL = "https://sellmatepos.com/json"


# =====================================================
# 공통
# =====================================================

def norm(value):
    return str(value).strip().rstrip("점").rstrip("店")


# =====================================================
# 1. 로그인
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
        "Origin": "https://sellmatepos.com",
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

    try:
        res = session.get(
            f"{BASE_URL}/store?mode=list",
            timeout=30,
        )

    except requests.RequestException as e:
        raise Exception(
            f"매장 API 요청 실패: {e}"
        )

    print(
        f"  매장 API 응답: "
        f"{res.status_code}"
    )

    print(
        f"  Content-Type: "
        f"{res.headers.get('Content-Type', '')}"
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
            "매장 API 응답이 JSON이 아닙니다: "
            f"{res.text[:1000]}"
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

    if not isinstance(
        store_list,
        dict
    ):
        raise Exception(
            f"매장 목록 데이터가 "
            f"올바르지 않습니다: "
            f"{store_list}"
        )

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
                    "perPage": 100,
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

            if res.status_code == 412:

                raise Exception(
                    "재고 API가 412 "
                    "'Need JS Update'를 "
                    "반환했습니다. "
                    f"현재 JS 버전: "
                    f"{SELLMATE_JS_VERSION}"
                )

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
                "재고 API 응답이 "
                "JSON이 아닙니다: "
                f"{res.text[:1000]}"
            )

        if isinstance(
            data,
            list
        ):

            items = data
            last_page = 1

        elif isinstance(
            data,
            dict
        ):

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

        else:

            items = []
            last_page = 1

        if not items:

            if page == 1:

                raise Exception(
                    "재고 API 응답 데이터가 "
                    "비어 있습니다."
                )

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

            if (
                not barcode
                or barcode == "None"
            ):
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

            if stocks:

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

                    all_stock.append({
                        "store": store_name,
                        "barcode": barcode,
                        "name": product_name,
                        "option": option_name,
                        "stock": qty,
                    })

            else:

                try:

                    total = int(
                        item.get(
                            "total_stock",
                            0
                        )
                        or 0
                    )

                except (
                    ValueError,
                    TypeError
                ):

                    total = 0

                all_stock.append({
                    "store": "ALL",
                    "barcode": barcode,
                    "name": product_name,
                    "option": option_name,
                    "stock": total,
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

    print(
        f"  샘플: "
        f"{all_stock[0]}"
    )

    return all_stock


# =====================================================
# 4. 매출 조회
# =====================================================

def get_sales(
    session,
    store_list
):

    print("💰 매출 데이터 조회 중...")

    today = datetime.now().strftime(
        "%Y-%m-%d"
    )

    start = (
        datetime.now()
        - timedelta(days=14)
    ).strftime(
        "%Y-%m-%d"
    )

    start_dt = (
        f"{start} 00:00:00"
    )

    end_dt = (
        f"{today} 23:59:59"
    )

    all_sales = []

    page = 1

    while True:

        params = [
            ("page", page),
            ("perPage", 100),

            (
                "filters[0][field]",
                "datetime"
            ),

            (
                "filters[0][operator]",
                ">="
            ),

            (
                "filters[0][value]",
                start_dt
            ),

            (
                "filters[1][field]",
                "datetime"
            ),

            (
                "filters[1][operator]",
                "<="
            ),

            (
                "filters[1][value]",
                end_dt
            ),

            (
                "timeflag",
                "true"
            ),

            (
                "sort[0][field]",
                "datetime"
            ),

            (
                "sort[0][direction]",
                "DESC"
            ),
        ]

        try:

            res = session.get(
                f"{BASE_URL}/order",
                params=params,
                timeout=30,
            )

        except requests.RequestException as e:

            raise Exception(
                f"매출 API 요청 실패 "
                f"(page {page}): {e}"
            )

        print(
            f"  매출 API 응답: "
            f"{res.status_code}"
        )

        if res.status_code != 200:

            if res.status_code == 412:

                raise Exception(
                    "매출 API가 412 "
                    "'Need JS Update'를 "
                    "반환했습니다. "
                    f"현재 JS 버전: "
                    f"{SELLMATE_JS_VERSION}"
                )

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
                "매출 API 응답이 "
                "JSON이 아닙니다: "
                f"{res.text[:1000]}"
            )

        if isinstance(
            data,
            list
        ):

            orders = data
            last_page = 1

        elif isinstance(
            data,
            dict
        ):

            orders = data.get(
                "data",
                []
            )

            last_page = data.get(
                "last_page",
                data.get(
                    "meta",
                    {}
                ).get(
                    "last_page",
                    1
                )
            )

        else:

            orders = []
            last_page = 1

        if not orders:

            break

        for order in orders:

            if not isinstance(
                order,
                dict
            ):
                continue

            order_type = order.get(
                "order_type",
                ""
            )

            if order_type not in (
                "판매",
                "sale",
                "normal",
                "",
            ):
                continue

            store_name = norm(
                order.get(
                    "store_name",
                    ""
                )
            )

            order_date = str(
                order.get(
                    "datetime",
                    ""
                )
            )[:10]

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

                if (
                    not barcode
                    or barcode == "None"
                ):
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

                all_sales.append({
                    "date": order_date,
                    "store": store_name,
                    "barcode": barcode,
                    "name": (
                        item.get(
                            "product_name",
                            ""
                        )
                        or ""
                    ),
                    "option": (
                        item.get(
                            "option_name",
                            ""
                        )
                        or ""
                    ),
                    "qty": qty,
                })

        print(
            f"  매출 page "
            f"{page}/{last_page} "
            f"(누적 "
            f"{len(all_sales)}건)"
        )

        if page >= last_page:
            break

        page += 1

    print(
        f"✅ 매출 총 "
        f"{len(all_sales)}건"
    )

    if all_sales:
        print(
            f"  샘플: "
            f"{all_sales[0]}"
        )

    return all_sales

# =====================================================
# 5-1. 재고 Google Sheets 저장
# =====================================================

def save_stock_to_sheets(stock_data):

    print("📊 재고 데이터를 Google Sheets에 저장 중...")

    if not stock_data:
        raise Exception(
            "재고 데이터가 0건이라 저장하지 않습니다."
        )

    creds = Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    gc = gspread.authorize(creds)

    sh = gc.open_by_key(SPREADSHEET_ID)

    today = datetime.now().strftime("%Y-%m-%d")

    try:
        ws = sh.worksheet("재고데이터")

    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(
            title="재고데이터",
            rows=10000,
            cols=6,
        )

    existing = ws.get_all_values()

    header = [
        "날짜",
        "매장",
        "바코드",
        "상품명",
        "옵션명",
        "현재고",
    ]

    # 오늘 데이터만 제거하고 과거 데이터는 유지
    rows_to_keep = []

    if existing:

        for row in existing[1:]:

            if row and row[0] != today:
                rows_to_keep.append(row)

    stock_rows = []

    for item in stock_data:

        store_name = item.get(
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
            not barcode
            or barcode == "None"
            or not store_name
            or store_name == "ALL"
        ):
            continue

        try:

            stock_qty = int(
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

            stock_qty = 0

        stock_rows.append([
            today,
            store_name,
            barcode,
            item.get("name", ""),
            item.get("option", ""),
            stock_qty,
        ])

    if not stock_rows:

        raise Exception(
            "저장 가능한 재고 데이터가 0건입니다."
        )

    print(
        f"  📦 저장할 재고: "
        f"{len(stock_rows)}건"
    )

    all_rows = [
        header,
        *rows_to_keep,
        *stock_rows,
    ]

    # 정상적인 데이터가 준비된 후 시트 갱신
    ws.clear()

    ws.update(
        range_name="A1",
        values=all_rows,
    )

    print(
        f"  ✅ 재고 {len(stock_rows)}건 저장 완료"
    )


# =====================================================
# 5-2. 매출 Google Sheets 저장
# =====================================================

def save_sales_to_sheets(sales_data):

    print("📊 매출 데이터를 Google Sheets에 저장 중...")

    creds = Credentials.from_service_account_info(
        GOOGLE_CREDS,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )

    gc = gspread.authorize(creds)

    sh = gc.open_by_key(SPREADSHEET_ID)

    today = datetime.now().strftime("%Y-%m-%d")

    try:

        ws = sh.worksheet("매출데이터")

    except gspread.WorksheetNotFound:

        ws = sh.add_worksheet(
            title="매출데이터",
            rows=100000,
            cols=6,
        )

    existing = ws.get_all_values()

    header = [
        "날짜",
        "매장",
        "바코드",
        "상품명",
        "옵션명",
        "판매수량",
    ]

    rows_to_keep = []

    if existing:

        for row in existing[1:]:

            if row and row[0] != today:
                rows_to_keep.append(row)

    sales_rows = []

    for sale in sales_data:

        if sale.get("date") != today:
            continue

        sales_rows.append([
            sale.get("date", ""),
            sale.get("store", ""),
            sale.get("barcode", ""),
            sale.get("name", ""),
            sale.get("option", ""),
            sale.get("qty", 0),
        ])

    # 매출이 0건이면 기존 시트를 건드리지 않음
    if not sales_rows:

        print(
            "⚠️ 오늘 매출 데이터가 0건입니다."
        )

        return

    all_rows = [
        header,
        *rows_to_keep,
        *sales_rows,
    ]

    ws.clear()

    ws.update(
        range_name="A1",
        values=all_rows,
    )

    print(
        f"  ✅ 매출 {len(sales_rows)}건 저장 완료"
    )

# =====================================================
# 6. 메인
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
        # 매장 목록
        # ---------------------------------------------

        store_list = get_store_list(
            session
        )

        # ---------------------------------------------
        # 재고 조회
        # ---------------------------------------------

        stock_data = get_all_stock(
            session,
            store_list
        )

        # ---------------------------------------------
        # ⭐ 재고 먼저 저장
        # ---------------------------------------------

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
        # 매출 조회
        # ---------------------------------------------

        try:

            sales_data = get_sales(
                session,
                store_list
            )

            save_sales_to_sheets(
                sales_data
            )

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
            "🎉 재고 동기화 완료!"
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

