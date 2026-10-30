"""Incrementally mirror Sellmate orders into the existing 33-column RAW.

The imported CSV through 2026-10-29 stays intact. API rows carry an opaque
identity in the store cell's note; no extra columns or receipt alterations.
Only complete API snapshots may change the sheet. No records/credentials are
logged. Fields absent from the external API remain blank rather than invented.
"""
import argparse
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
TARGET_ID = os.environ.get('RAW_SPREADSHEET_ID', '1hGSFTLZxa9ObtDPdMtiCbxcU0GnTlsO9Xq3tMAQOuTQ')
SEED_LAST_ROW = 225841
SEED_END = date(2026, 10, 29)
PREFIX = 'SELLMATE_RAW_V1:'
STORES = {'안국', '명동', '성수', '제주', '한남', '홍대', '해운대'}
HEADER = ['매장','단말기','영수증 번호','거래 일시','거래 구분','수령 구분',
          '상품명','옵션명','바코드 번호','단가','수량','금액','상품 할인',
          '할인율','할인 유형','현금','카드','포인트','쿠폰','주문 수동 할인',
          '총 상품 금액','등급 할인','거래 시간','linePay','wechatPay','aliPay',
          'kakaopay','naverPay','zeroPay','상품 코드','현금 영수증','수수료(원)','적립 포인트']


def local_datetime(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)


def number(value):
    if value is None or value == '':
        return ''
    result = float(value)
    if not (-1e15 < result < 1e15):
        raise ValueError('Invalid numeric API field')
    return int(result) if result.is_integer() else result


def normalize_order(order):
    terminal = order.get('terminal') or {}
    store = str((terminal.get('store') or order.get('store') or {}).get('name', '')).strip().removesuffix('점')
    if store not in STORES:
        return []
    transaction = order.get('transaction') or terminal.get('transaction') or {}
    receipt = order.get('receipt') or terminal.get('receipt') or {}
    dt = local_datetime(transaction.get('datetime'))
    if order.get('idx') is None or not receipt.get('number'):
        raise ValueError('Order identity/receipt missing; refusing incomplete import')
    groups = [('ordered_unit', order.get('ordered_unit') or []),
              ('returned_unit', order.get('returned_unit') or [])]
    all_units = [u for _, units in groups for u in units]
    gross = sum(number(u['unit_price']) * number(u['qty']) for u in all_units)
    discounts = sum(number(u.get('discount_amount', 0)) for u in all_units)
    result = []
    for group, units in groups:
        for unit in units:
            if unit.get('idx') is None:
                raise ValueError('Order line identity missing')
            sales = unit.get('sales_unit') or {}
            product = sales.get('product_class') or {}
            barcodes = sales.get('barcode') or {}
            barcode = barcodes.get('code1') or barcodes.get('code2')
            if not barcode:
                raise ValueError('Order line barcode missing')
            qty = number(unit['qty'])  # Preserve sign, including return reversals.
            price = number(unit['unit_price'])
            options = sales.get('option_names') or {}
            option = sales.get('origin_option_name') or ' / '.join(str(v) for v in options.values())
            kind = {'sale':'판매','return':'반품','exchange':'교환'}.get(str(order.get('type')), str(order.get('type') or ''))
            values = [store, terminal.get('name',''), str(receipt['number']),
                      dt.strftime('%Y-%m-%d %H:%M:%S'), kind, '일반',
                      product.get('name',''), option, str(barcode), price, qty, price*qty,
                      number(unit.get('discount_amount')), number(unit.get('discount_rate')),
                      unit.get('discount_name',''), number(order.get('cash_sales')),
                      number(order.get('card_sales')), number(order.get('point_sales')),
                      number(order.get('coupon_sales')), number(order.get('cart_amount_discount')),
                      gross-discounts, number(order.get('level_discount')), dt.hour,
                      '', '', '', '', '', '', product.get('code',''), '',
                      number(order.get('commission_fee')), number(order.get('save_point'))]
            identity = json.dumps([str(order['idx']), group, str(unit['idx'])], separators=(',',':'))
            key = hashlib.sha256(identity.encode()).hexdigest()
            result.append((key, dt.date(), values))
    return result


def collect_snapshot(api, start, end):
    """Do not write anything until every page has been successfully read."""
    records = {}
    current = start
    while current <= end:
        page = 1
        fingerprints = set()
        while True:
            orders, last_page, page_size = api(page, current, current)
            fingerprint = tuple(str(o.get('idx')) for o in orders)
            if orders and fingerprint in fingerprints:
                raise RuntimeError('API repeated a page; snapshot is incomplete')
            fingerprints.add(fingerprint)
            for order in orders:
                for key, when, values in normalize_order(order):
                    if when != current:
                        raise RuntimeError('API returned dates outside the requested day')
                    if key in records:
                        raise RuntimeError('Duplicate order-line identity in API snapshot')
                    records[key] = (when, values)
            if not orders or len(orders)<page_size or page>=last_page:
                break
            page += 1
            if page > 10000:
                raise RuntimeError('API pagination limit reached')
        current += timedelta(days=1)
    return records


def cell(value):
    if value == '' or value is None:
        return {}
    return {'userEnteredValue': {'numberValue':value} if isinstance(value,(int,float))
            else {'stringValue':str(value)}}


def row_data(key, when, values, deleted=False):
    cells = [cell(v) for v in values]
    cells[0]['note'] = PREFIX + json.dumps({'key':key,'date':when.isoformat(),'deleted':deleted},separators=(',',':'))
    return {'values':cells}


def plan_updates(snapshot, existing, last_row, start, end):
    """Return fixed row writes, which are safe to repeat after a lost response."""
    updates = []
    for key,(when,values) in sorted(snapshot.items(),key=lambda x:(x[1][0],x[0])):
        if key in existing:
            at, old_when, old_values = existing[key]
            if values != old_values:
                updates.append((at,row_data(key,when,values)))
        else:
            last_row += 1
            updates.append((last_row,row_data(key,when,values)))
    for key,(at,when,values) in existing.items():
        if start<=when<=end and key not in snapshot and values[10] != 0:
            tombstone = list(values)
            tombstone[4], tombstone[10], tombstone[11] = '취소', 0, 0
            updates.append((at,row_data(key,when,tombstone,deleted=True)))
    return sorted(updates), last_row


def read_existing(sh, raw):
    stores = raw.col_values(1)
    last = len(stores)
    if last < SEED_LAST_ROW:
        raise RuntimeError('Imported CSV rows were removed; refusing to guess a new baseline')
    result = {}
    if last == SEED_LAST_ROW:
        return result, last
    tail = raw.get(f'A{SEED_LAST_ROW+1}:AG{last}',value_render_option='UNFORMATTED_VALUE')
    meta = sh.fetch_sheet_metadata(params={
        'includeGridData':True,'ranges':f"'raw'!A{SEED_LAST_ROW+1}:A{last}",
        'fields':'sheets(data(startRow,rowData(values(note))))'})
    notes = {}
    for sheet in meta.get('sheets',[]):
        for block in sheet.get('data',[]):
            for i,row in enumerate(block.get('rowData',[]),block.get('startRow',0)+1):
                notes[i] = (row.get('values') or [{}])[0].get('note','')
    for i,values in enumerate(tail,SEED_LAST_ROW+1):
        note = notes.get(i,'')
        if not note.startswith(PREFIX):
            raise RuntimeError('Untracked manual rows after CSV baseline; reconcile before syncing')
        info = json.loads(note[len(PREFIX):])
        key = info['key']
        if key in result:
            raise RuntimeError('Duplicate sync identities already exist in RAW')
        result[key] = (i,date.fromisoformat(info['date']),list(values)+['']*(33-len(values)))
    return result,last


def write_updates(sh,raw,updates,last_row):
    if last_row > raw.row_count:
        raise RuntimeError('RAW capacity reached. Archive history before adding rows; no records were removed')
    for offset in range(0,len(updates),500):
        chunk = updates[offset:offset+500]
        requests = []
        for at,data in chunk:
            if requests:
                prev = requests[-1]['updateCells']
                if prev['start']['rowIndex']+len(prev['rows']) == at-1:
                    prev['rows'].append(data)
                    continue
            requests.append({'updateCells':{'start':{'sheetId':raw.id,'rowIndex':at-1,'columnIndex':0},
                'rows':[data],'fields':'userEnteredValue,note'}})
        sh.batch_update({'requests':requests})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args()
    import sync
    client = sync.get_google_client()
    sh = client.open_by_key(TARGET_ID)
    raw = sh.worksheet('raw')
    if raw.row_values(1) != HEADER:
        raise RuntimeError('RAW header differs from the original CSV layout')
    existing,last_row = read_existing(sh,raw)
    end = datetime.now(KST).date()-timedelta(days=1)
    latest = max((x[1] for x in existing.values()),default=SEED_END)
    start = max(SEED_END+timedelta(days=1),min(latest,end)-timedelta(days=13))
    if start>end:
        print('RAW_READY: Imported history is current through yesterday; next run will collect the next complete day.')
        return
    session = sync.issue_external_token()
    def api(page,start,end):
        orders,last = sync.get_sales_page(session,page,start,end)
        return orders,last,sync.PER_PAGE
    snapshot = collect_snapshot(api,start,end)
    # An empty API response must not erase previously imported rows.
    old_active = any(start<=d<=end and v[10]!=0 for _,d,v in existing.values())
    if not snapshot and old_active:
        raise RuntimeError('Empty API snapshot for existing sales; refusing cancellation updates')
    updates,new_last = plan_updates(snapshot,existing,last_row,start,end)
    if args.dry_run:
        print('RAW_DRY_RUN_OK: API snapshot and update plan validated; no sheet writes.')
        return
    write_updates(sh,raw,updates,new_last)
    # Re-read notes to confirm writes are visible and no identity was duplicated.
    after,actual_last = read_existing(sh,raw)
    if actual_last!=new_last or any(k not in after or after[k][2]!=v for k,(_,v) in snapshot.items()):
        raise RuntimeError('RAW verification mismatch; subsequent run will reconcile fixed identities')
    print('RAW_SYNC_OK: completed full-day sales update and verified persistent identities.')


if __name__=='__main__':
    main()
