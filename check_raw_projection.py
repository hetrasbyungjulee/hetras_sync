"""Read-only reconciliation to CSV. Never log records or account metadata."""
import hashlib
import json
from datetime import date
from raw_sync import normalize_order

EXPECTED = '772b93683860b2ef51a0c86368c51a4987f021240ed02e7381f71f6ecf52e987'


def main():
    import sync
    session = sync.issue_external_token()
    values=[]
    page=1
    seen_pages=set()
    while True:
        orders,last_page=sync.get_sales_page(session,page,date(2026,10,29),date(2026,10,29))
        fingerprint=tuple(str(o.get('idx')) for o in orders)
        if orders and fingerprint in seen_pages:
            raise RuntimeError('Repeated API page during reconciliation')
        seen_pages.add(fingerprint)
        for order in orders:
            for key,when,row in normalize_order(order):
                values.append([row[0],row[2],row[3],row[8],row[10]])
        if not orders or len(orders)<sync.PER_PAGE or page>=last_page:
            break
        page+=1
    actual=hashlib.sha256(json.dumps(sorted(values),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if actual!=EXPECTED:
        raise RuntimeError('API projection differs from the original October 29 CSV; deployment check failed')
    print('CSV_RECONCILIATION_OK: all store/receipt/time/barcode/quantity rows match.')


if __name__=='__main__':
    main()
