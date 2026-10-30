"""Read-only CSV/API reconciliation; never logs records or credentials."""
import hashlib
import json
from collections import Counter
from datetime import date

import sync

STORES = {'안국','명동','성수','제주','한남','홍대','해운대'}
EXPECTED = '772b93683860b2ef51a0c86368c51a4987f021240ed02e7381f71f6ecf52e987'

def shape(v, depth=0):
    if depth > 5:
        return type(v).__name__
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0], depth + 1)] if v else []
    return type(v).__name__

def core(order, unit):
    terminal = order.get('terminal') or {}
    store = (terminal.get('store') or order.get('store') or {}).get('name','').removesuffix('점')
    transaction = order.get('transaction') or terminal.get('transaction') or {}
    receipt = order.get('receipt') or terminal.get('receipt') or {}
    sales_unit = unit.get('sales_unit') or {}
    barcode = sales_unit.get('barcode') or {}
    return [store, str(receipt.get('number','')), str(transaction.get('datetime',''))[:19].replace('T',' '),
            str(barcode.get('code1') or barcode.get('code2') or ''), int(unit.get('qty',0))]

def main():
    session = sync.issue_external_token()
    rows = {'ordered': [], 'both': [], 'fallback': []}
    types = Counter()
    emitted = set()
    page = 1
    while True:
        orders, last = sync.get_sales_page(session,page,date(2026,10,29),date(2026,10,29))
        if not orders:
            break
        for order in orders:
            key = (str(order.get('type')),str(order.get('status')))
            types[key] += 1
            if key not in emitted:
                emitted.add(key)
                print('SCHEMA', key, json.dumps(shape(order),ensure_ascii=False))
            a = order.get('ordered_unit') or []
            b = order.get('returned_unit') or []
            for label, units in [('ordered',a),('both',a+b),('fallback',a or b)]:
                for unit in units:
                    v = core(order,unit)
                    if v[0] in STORES:
                        rows[label].append(v)
        if len(orders)<sync.PER_PAGE or page>=last:
            break
        page += 1
    for label, values in rows.items():
        h=hashlib.sha256(json.dumps(sorted(values),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        print('CSV_CORE_MATCH',label,h==EXPECTED)
    print('TRANSACTION_TYPES', sorted(types))

if __name__=='__main__':
    main()
