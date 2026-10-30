import copy
import unittest
from datetime import date
from raw_sync import normalize_order, collect_snapshot, plan_updates, row_data, PREFIX

DAY = date(2026,10,30)

def order(qty=2, typ='sale', status='normal'):
    return {'idx':100,'type':typ,'status':status,
        'terminal':{'name':'테스트 단말기','store':{'name':'안국'}},
        'receipt':{'number':'0000001'},'transaction':{'datetime':'2026-10-30 12:00:00'},
        'cash_sales':100,'card_sales':0,'point_sales':0,'coupon_sales':0,
        'cart_amount_discount':0,'level_discount':0,'commission_fee':0,'save_point':0,
        'ordered_unit':[{'idx':501,'qty':qty,'unit_price':100,'discount_amount':0,
            'discount_rate':0,'discount_name':'','sales_unit':{'origin_option_name':'07 호텔우드',
            'barcode':{'code1':'PHEKRTEST'},'product_class':{'name':'테스트상품','code':'ITEM'}}}],
        'returned_unit':[]}

class RawTests(unittest.TestCase):
    def test_signed_returns_and_reversals(self):
        for qty,typ in [(2,'sale'),(-2,'sale'),(-2,'return'),(2,'return')]:
            _,_,r=normalize_order(order(qty,typ))[0]
            self.assertEqual(len(r),33)
            self.assertEqual(r[10],qty)
            self.assertEqual(r[11],qty*100)
            self.assertEqual(r[2],'0000001')

    def test_missing_payment_channels_remain_blank(self):
        r=normalize_order(order())[0][2]
        self.assertEqual(r[23:29],['']*6)

    def test_identity_ignores_status_quantity_changes(self):
        a=normalize_order(order())[0][0]
        b=normalize_order(order(-2,'return','cancel'))[0][0]
        self.assertEqual(a,b)

    def test_same_barcode_separate_lines_preserved(self):
        o=order();second=copy.deepcopy(o['ordered_unit'][0]);second['idx']=502
        o['ordered_unit'].append(second)
        r=normalize_order(o)
        self.assertEqual(len(r),2)
        self.assertNotEqual(r[0][0],r[1][0])

    def test_no_writes_before_pagination_finishes(self):
        def api(page,*args):
            if page==2:raise RuntimeError('network')
            return [order()],2,1
        with self.assertRaises(RuntimeError):collect_snapshot(api,DAY,DAY)

    def test_repeated_page_is_rejected(self):
        with self.assertRaises(RuntimeError):
            collect_snapshot(lambda *args:([order()],999,1),DAY,DAY)

    def test_duplicate_identity_is_rejected(self):
        with self.assertRaises(RuntimeError):
            collect_snapshot(lambda *args:([order(),order()],1,100),DAY,DAY)

    def test_out_of_window_data_rejected(self):
        with self.assertRaises(RuntimeError):
            collect_snapshot(lambda *args:([order()],1,100),date(2026,10,31),date(2026,10,31))

    def test_repeat_run_does_not_append_again(self):
        key,d,values=normalize_order(order())[0]
        snapshot={key:(d,values)}
        updates,last=plan_updates(snapshot,{},225841,DAY,DAY)
        self.assertEqual(last,225842)
        self.assertEqual(updates[0][0],225842)
        self.assertTrue(updates[0][1]['values'][0]['note'].startswith(PREFIX))
        again,final=plan_updates(snapshot,{key:(225842,d,values)},last,DAY,DAY)
        self.assertEqual(again,[])
        self.assertEqual(final,last)

    def test_update_in_place_and_tombstone_missing_lines(self):
        key,d,values=normalize_order(order())[0]
        _,_,changed=normalize_order(order(-1))[0]
        updates,last=plan_updates({key:(d,changed)},{key:(225842,d,values)},225842,DAY,DAY)
        self.assertEqual(last,225842)
        self.assertEqual(updates[0][0],225842)
        self.assertEqual(updates[0][1]['values'][10]['userEnteredValue']['numberValue'],-1)
        updates,_=plan_updates({},{key:(225842,d,values)},225842,DAY,DAY)
        self.assertEqual(updates[0][1]['values'][10]['userEnteredValue']['numberValue'],0)
        self.assertEqual(values[10],2)

if __name__=='__main__':unittest.main()
