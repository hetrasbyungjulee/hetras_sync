"""One-time, atomic compaction of a separately backed-up 33-column RAW.

Read only the four authoritative source columns from the live sheet, aggregate
in memory, re-read to detect concurrent edits, then replace RAW and its two
direct formula dependencies in one Sheets batchUpdate transaction.
"""
import json
import os
from collections import defaultdict
from datetime import date,datetime,timedelta
from raw_sync import HEADER,TARGET_ID,cell,number,publish_status


def source_date(v):
    if isinstance(v,(float,int)):
        return (datetime(1899,12,30)+timedelta(days=v)).date().isoformat()
    return date.fromisoformat(str(v)[:10]).isoformat()


def read_source(raw,last):
    cols=raw.batch_get([f'{c}2:{c}{last}' for c in ['A','D','I','K']],
                       value_render_option='UNFORMATTED_VALUE')
    if any(len(c)!=last-1 for c in cols):
        raise RuntimeError('Source column lengths disagree; nothing changed')
    rows=[]
    for store,when,barcode,qty in zip(*cols):
        if not all(len(c)==1 for c in (store,when,barcode,qty)):
            raise RuntimeError('Missing source fields; nothing changed')
        code=barcode[0]
        if isinstance(code,(int,float)):code=str(int(code))
        rows.append([source_date(when[0]),str(store[0]).strip(),str(code).strip(),number(qty[0])])
    return rows


def aggregate(rows):
    result=defaultdict(int)
    for when,store,barcode,qty in rows:
        if not store or not barcode:
            raise RuntimeError('Blank source key; nothing changed')
        result[(when,store,barcode)]+=qty
    return [[*key,qty] for key,qty in sorted(result.items())]


def main():
    import sync
    sh=sync.get_google_client().open_by_key(TARGET_ID)
    raw=sh.worksheet('raw')
    header=raw.row_values(1)
    if header==HEADER:
        print('COMPACT_MIGRATION_ALREADY_DONE')
        return
    if os.environ.get('COMPACT_BACKUP_CONFIRMED')!='true':
        raise RuntimeError('Confirm a separate full original backup before migration')
    if len(header)!=33 or header[:11]!=['매장','단말기','영수증 번호','거래 일시','거래 구분','수령 구분','상품명','옵션명','바코드 번호','단가','수량']:
        raise RuntimeError('Unexpected legacy RAW layout; nothing changed')
    last=len(raw.col_values(1))
    source=read_source(raw,last)
    compact=aggregate(source)
    if sum(r[3] for r in source)!=sum(r[3] for r in compact):
        raise RuntimeError('Quantity reconciliation failed')
    if source!=read_source(raw,last) or len(raw.col_values(1))!=last:
        raise RuntimeError('RAW changed during migration preparation; nothing changed')
    summary=sh.worksheet('Summary')
    helper=sh.worksheet('집계')
    current_formula=summary.acell('C1',value_render_option='FORMULA').value or ''
    formula='=MAX(ARRAYFORMULA(IFERROR(INT(raw!A2:A),IFERROR(DATEVALUE(raw!A2:A),0))))'
    query='=ARRAYFORMULA(LET(dt,IFERROR(INT(raw!A2:A),IFERROR(DATEVALUE(raw!A2:A),0)),qty,IFERROR(raw!D2:D*1,0),QUERY({raw!C2:C&"",raw!B2:B,IF((dt>=Summary!$C$1-6)*(dt<Summary!$C$1+1),qty,0),IF((dt>=Summary!$C$1-4)*(dt<Summary!$C$1+1),qty,0),IF((dt>=Summary!$C$1-2)*(dt<Summary!$C$1+1),qty,0)},"select Col1,Col2,sum(Col3),sum(Col4),sum(Col5) where Col1 <> \'\' and Col2 <> \'\' group by Col1,Col2 label Col1 \'바코드 번호\',Col2 \'매장\',sum(Col3) \'7일 순판매\',sum(Col4) \'5일 순판매\',sum(Col5) \'3일 순판매\'",0)))'
    requests=[{'repeatCell':{'range':{'sheetId':raw.id,'startRowIndex':0,'endRowIndex':raw.row_count,
                    'startColumnIndex':0,'endColumnIndex':raw.col_count},
                    'cell':{},'fields':'userEnteredValue,note'}}]
    output=[HEADER]+compact
    for offset in range(0,len(output),3000):
        requests.append({'updateCells':{'start':{'sheetId':raw.id,'rowIndex':offset,'columnIndex':0},
            'rows':[{'values':[cell(v) for v in row]} for row in output[offset:offset+3000]],
            'fields':'userEnteredValue'}})
    grid_rows=max(50000,len(output)+10000)
    requests.extend([
        {'updateSheetProperties':{'properties':{'sheetId':raw.id,'gridProperties':{
            'rowCount':grid_rows,'columnCount':4,'frozenRowCount':1}},'fields':'gridProperties'}},
        {'repeatCell':{'range':{'sheetId':raw.id,'startRowIndex':1,'endRowIndex':grid_rows,
            'startColumnIndex':0,'endColumnIndex':3},'cell':{'userEnteredFormat':{
            'numberFormat':{'type':'TEXT','pattern':'@'}}},'fields':'userEnteredFormat.numberFormat'}},
        {'repeatCell':{'range':{'sheetId':raw.id,'startRowIndex':1,'endRowIndex':grid_rows,
            'startColumnIndex':3,'endColumnIndex':4},'cell':{'userEnteredFormat':{
            'numberFormat':{'type':'NUMBER','pattern':'#,##0;[Red]-#,##0'}}},'fields':'userEnteredFormat.numberFormat'}},
        {'updateCells':{'start':{'sheetId':helper.id,'rowIndex':0,'columnIndex':0},
            'rows':[{'values':[{'userEnteredValue':{'formulaValue':query}}]}],'fields':'userEnteredValue'}},
        {'updateCells':{'start':{'sheetId':summary.id,'rowIndex':0,'columnIndex':1},
            'rows':[{'values':[{'note':'RAW는 판매일·매장·바코드별 순판매수량 4열로 경량화했습니다. 상세 원본은 별도 백업에 보관합니다. 자동 수집 시 필요한 행만 확장하며 Google Sheets 전체 1,000만 셀 제한이 적용됩니다.'}]}],
            'fields':'note'}},
    ])
    if current_formula.startswith('='):
        requests.append({'updateCells':{'start':{'sheetId':summary.id,'rowIndex':0,'columnIndex':2},
            'rows':[{'values':[{'userEnteredValue':{'formulaValue':formula},
                'note':'RAW 마지막 판매일 자동 반영. 날짜를 직접 입력하면 해당 날짜 기준으로 집계합니다.\n자동 수식: '+formula}]}],
            'fields':'userEnteredValue,note'}})
    for i,width in enumerate([115,100,170,125]):
        requests.append({'updateDimensionProperties':{'range':{'sheetId':raw.id,'dimension':'COLUMNS',
            'startIndex':i,'endIndex':i+1},'properties':{'pixelSize':width},'fields':'pixelSize'}})
    # The full source has already been backed up. All transformations commit atomically.
    sh.batch_update({'requests':requests})
    reread=sh.worksheet('raw').get_all_values()
    normalized=[[r[0],r[1],r[2],number(r[3])] for r in reread[1:]]
    if reread[0]!=HEADER or normalized!=compact:
        raise RuntimeError('Post-migration verification failed; original full backup remains available')
    publish_status(sh,sh.worksheet('raw'),date.fromisoformat(max(r[0] for r in compact)),ready=True)
    print('COMPACT_MIGRATION_OK: live source aggregated and every daily row verified.')


if __name__=='__main__':main()
