"""Daily SKU quantities only: date, store, barcode, signed net quantity."""
import argparse
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KST=ZoneInfo('Asia/Seoul')
TARGET_ID=os.environ.get('RAW_SPREADSHEET_ID','1hGSFTLZxa9ObtDPdMtiCbxcU0GnTlsO9Xq3tMAQOuTQ')
HEADER=['판매일','매장','바코드','순판매수량']
STORES={'안국','명동','성수','제주','한남','홍대','해운대'}
INITIAL_END=date(2026,10,29)
LOOKBACK_DAYS=int(os.environ.get('RAW_LOOKBACK_DAYS','7'))
STATE_MARKER='COMPACT_RAW_STATE_V1:'


def local_datetime(value):
    dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def number(value):
    n=float(value)
    if not -1e15<n<1e15:
        raise ValueError('Invalid quantity')
    return int(n) if n.is_integer() else n


def normalize_order(order):
    terminal=order.get('terminal') or {}
    store=str((terminal.get('store') or order.get('store') or {}).get('name','')).strip().removesuffix('점')
    if store not in STORES:
        return []
    transaction=order.get('transaction') or terminal.get('transaction') or {}
    when=local_datetime(transaction.get('datetime')).date()
    if order.get('idx') is None:
        raise ValueError('Order ID missing')
    units=[('sale',u,u) for u in order.get('ordered_unit') or []]
    units += [('return',u,u.get('ordered_unit') or {}) for u in order.get('returned_unit') or []]
    result=[]
    for kind,line,product_line in units:
        barcodes=(product_line.get('sales_unit') or {}).get('barcode') or {}
        barcode=barcodes.get('code1') or barcodes.get('code2')
        if not barcode or line.get('idx') is None:
            raise ValueError('Line ID or barcode missing')
        qty=number(line['qty'])  # Both sales and nested returns already carry CSV-compatible signs.
        identity=(str(order['idx']),kind,str(line['idx']))
        result.append((identity,when,[when.isoformat(),store,str(barcode),qty]))
    return result


def collect_snapshot(api,start,end):
    daily=defaultdict(int)
    identities=set()
    current=start
    while current<=end:
        page=1
        page_ids=set()
        while True:
            orders,last_page,page_size=api(page,current,current)
            fingerprint=tuple(str(o.get('idx')) for o in orders)
            if orders and fingerprint in page_ids:
                raise RuntimeError('Repeated API page; refusing incomplete snapshot')
            page_ids.add(fingerprint)
            for order in orders:
                for identity,when,row in normalize_order(order):
                    if when!=current:
                        raise RuntimeError('API returned an unexpected transaction date')
                    if identity in identities:
                        raise RuntimeError('Duplicate order-line identity in API snapshot')
                    identities.add(identity)
                    daily[tuple(row[:3])]+=row[3]
            if not orders or len(orders)<page_size or page>=last_page:
                break
            page+=1
            if page>10000:
                raise RuntimeError('API pagination limit reached')
        current+=timedelta(days=1)
    return {key:[*key,qty] for key,qty in daily.items()}


def read_existing(raw):
    values=raw.get_all_values(value_render_option='UNFORMATTED_VALUE')
    if not values or values[0]!=HEADER:
        raise RuntimeError('RAW must first be migrated to the four-column daily layout')
    existing={}
    for at,row in enumerate(values[1:],2):
        if len(row)!=4:
            raise RuntimeError('Incomplete daily RAW row')
        date.fromisoformat(row[0])
        key=tuple(row[:3])
        if key in existing:
            raise RuntimeError('Duplicate day/store/barcode rows in RAW')
        existing[key]=(at,[*row[:3],number(row[3])])
    return existing,len(values)


def validate_snapshot(snapshot,existing,start,end):
    active_days={row[0] for _,row in existing.values()
                 if start.isoformat()<=row[0]<=end.isoformat() and row[3]!=0}
    actual_days={key[0] for key in snapshot}
    if active_days-actual_days:
        raise RuntimeError('API returned no sales for a previously populated day; keeping existing quantities')


def plan_updates(snapshot,existing,last_row,start,end):
    updates=[]
    for key,row in sorted(snapshot.items()):
        if key in existing:
            at,old=existing[key]
            if row!=old:
                updates.append((at,row))
        else:
            last_row+=1
            updates.append((last_row,row))
    for key,(at,row) in existing.items():
        if start.isoformat()<=row[0]<=end.isoformat() and key not in snapshot and row[3]!=0:
            updates.append((at,[*row[:3],0]))
    return sorted(updates),last_row


def cell(value):
    return {'userEnteredValue':{'numberValue':value} if isinstance(value,(int,float))
            else {'stringValue':str(value)}}


def get_complete_through(sh,raw,existing,end):
    meta=sh.fetch_sheet_metadata(params={'includeGridData':True,'ranges':"'raw'!A1",
        'fields':'sheets(data(rowData(values(note))))'})
    for sheet in meta.get('sheets',[]):
        for block in sheet.get('data',[]):
            for row in block.get('rowData',[]):
                note=(row.get('values') or [{}])[0].get('note','')
                if STATE_MARKER in note:
                    data=json.loads(note.split(STATE_MARKER,1)[1])
                    return date.fromisoformat(data['complete_through'])
    # An absent status does not make partially written latest rows authoritative.
    latest=max((date.fromisoformat(k[0]) for k in existing),default=INITIAL_END)
    return min(latest-timedelta(days=1),end)


def publish_status(sh,raw,end,ready=False):
    now=datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    note=(('일별 집계 변환 및 자동 수집 준비 완료' if ready else '일별 집계 자동 수집 성공')+' · '+now+' KST\n'
          '판매일·매장·바코드별 순판매수량만 누적 보관합니다. 거래별 상세 원본은 별도 백업에 있습니다.\n'
          '매일 06시경 전일까지 수집하고 최근 7일을 재조회해 취소·반품을 반영합니다.\n'
          '자동화가 관리하는 RAW에 수동 행을 추가하지 마세요.\n'+STATE_MARKER+
          json.dumps({'complete_through':end.isoformat()},separators=(',',':')))
    sh.batch_update({'requests':[{'updateCells':{'start':{'sheetId':raw.id,'rowIndex':0,'columnIndex':0},
        'rows':[{'values':[{'note':note}]}],'fields':'note'}}]})


def write_updates(sh,raw,updates,last_row):
    if last_row>raw.row_count:
        rows=max(last_row+1000,raw.row_count+10000)
        meta=sh.fetch_sheet_metadata()
        used=sum(s['properties']['gridProperties']['rowCount']*s['properties']['gridProperties']['columnCount']
                 for s in meta['sheets'] if s['properties'].get('sheetType')=='GRID')
        if used+(rows-raw.row_count)*4>10000000:
            raise RuntimeError('Spreadsheet capacity reached; archive before further collection')
        raw.resize(rows=rows,cols=4)
    for offset in range(0,len(updates),3000):
        requests=[]
        for at,row in updates[offset:offset+3000]:
            data={'values':[cell(v) for v in row]}
            if requests and requests[-1]['updateCells']['start']['rowIndex']+len(requests[-1]['updateCells']['rows'])==at-1:
                requests[-1]['updateCells']['rows'].append(data)
            else:
                requests.append({'updateCells':{'start':{'sheetId':raw.id,'rowIndex':at-1,'columnIndex':0},
                    'rows':[data],'fields':'userEnteredValue'}})
        sh.batch_update({'requests':requests})


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    import sync
    sh=sync.get_google_client().open_by_key(TARGET_ID)
    raw=sh.worksheet('raw')
    existing,last=read_existing(raw)
    end=datetime.now(KST).date()-timedelta(days=1)
    if end<=INITIAL_END:
        if not args.dry_run:publish_status(sh,raw,end,ready=True)
        print('DAILY_RAW_READY: compact imported history is ready for the next complete day.')
        return
    complete=get_complete_through(sh,raw,existing,end)
    start=max(date(2026,10,1),min(complete+timedelta(days=1),end)-timedelta(days=LOOKBACK_DAYS-1))
    session=sync.issue_external_token()
    def api(page,start,end):
        orders,last_page=sync.get_sales_page(session,page,start,end)
        return orders,last_page,sync.PER_PAGE
    snapshot=collect_snapshot(api,start,end)
    validate_snapshot(snapshot,existing,start,end)
    updates,new_last=plan_updates(snapshot,existing,last,start,end)
    if args.dry_run:
        print('DAILY_RAW_DRY_RUN_OK: complete snapshot and update plan validated.')
        return
    write_updates(sh,raw,updates,new_last)
    after,actual_last=read_existing(raw)
    if actual_last!=new_last or any(k not in after or after[k][1]!=v for k,v in snapshot.items()):
        raise RuntimeError('Daily RAW verification failed; next run will reconcile fixed daily keys')
    publish_status(sh,raw,end)
    print('DAILY_RAW_SYNC_OK: daily SKU quantities updated and verified.')


if __name__=='__main__':main()
