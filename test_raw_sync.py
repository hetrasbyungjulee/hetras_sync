import copy
import unittest
from datetime import date
from raw_sync import normalize_order,collect_snapshot,plan_updates,validate_snapshot
from migrate_raw_daily import aggregate,source_date

DAY=date(2026,10,30)

def order(qty=2,idx=1):
    return {'idx':idx,'terminal':{'store':{'name':'안국'}},
        'transaction':{'datetime':'2026-10-30 12:00:00'},
        'ordered_unit':[{'idx':100,'qty':qty,'sales_unit':{'barcode':{'code1':'PHEKRTEST'}}}],
        'returned_unit':[]}

class DailyTests(unittest.TestCase):
    def test_only_four_fields_are_kept(self):
        self.assertEqual(normalize_order(order())[0][2],['2026-10-30','안국','PHEKRTEST',2])

    def test_compaction_preserves_date_store_barcode_boundaries(self):
        rows=[['2026-10-29','안국','001',3],['2026-10-29','안국','001',-1],
              ['2026-10-28','안국','001',4],['2026-10-29','성수','001',5],
              ['2026-10-29','안국','002',0]]
        compact=aggregate(rows)
        self.assertEqual(len(compact),4)
        self.assertEqual(sum(r[3] for r in compact),sum(r[3] for r in rows))
        self.assertIn(['2026-10-29','안국','001',2],compact)

    def test_google_date_serial_and_text_date_agree(self):
        self.assertEqual(source_date(46324.96628472222),'2026-10-29')
        self.assertEqual(source_date('2026-10-29 23:11:27'),'2026-10-29')

    def test_return_sign_and_reversal_preserved(self):
        for qty in [-2,2]:
            o=order();original=o['ordered_unit'].pop()
            o['returned_unit']=[{'idx':300,'qty':qty,'ordered_unit':original}]
            self.assertEqual(normalize_order(o)[0][2][3],qty)

    def test_daily_aggregation_counts_distinct_transactions(self):
        orders=[order(4,1),order(-1,2),order(2,3)]
        s=collect_snapshot(lambda *args:(orders,1,100),DAY,DAY)
        self.assertEqual(list(s.values()),[['2026-10-30','안국','PHEKRTEST',5]])

    def test_same_barcode_multiple_lines_are_summed(self):
        o=order();other=copy.deepcopy(o['ordered_unit'][0]);other['idx']=101
        o['ordered_unit'].append(other)
        s=collect_snapshot(lambda *args:([o],1,100),DAY,DAY)
        self.assertEqual(next(iter(s.values()))[3],4)

    def test_repeated_page_rejected(self):
        with self.assertRaises(RuntimeError):collect_snapshot(lambda *args:([order()],999,1),DAY,DAY)

    def test_duplicate_lines_rejected(self):
        with self.assertRaises(RuntimeError):collect_snapshot(lambda *args:([order(),order()],1,100),DAY,DAY)

    def test_network_failure_does_not_produce_partial_snapshot(self):
        def api(page,*args):
            if page==2:raise RuntimeError('network')
            return [order()],2,1
        with self.assertRaises(RuntimeError):collect_snapshot(api,DAY,DAY)

    def test_same_daily_key_updates_without_append(self):
        key=('2026-10-30','안국','PHEKRTEST')
        updates,last=plan_updates({key:[*key,8]},{key:(2,[*key,5])},2,DAY,DAY)
        self.assertEqual(updates,[(2,[*key,8])]);self.assertEqual(last,2)
        self.assertEqual(plan_updates({key:[*key,8]},{key:(2,[*key,8])},2,DAY,DAY),([],2))

    def test_missing_product_zeroed_but_history_preserved(self):
        key=('2026-10-30','안국','PHEKRTEST');old=('2026-10-01','안국','PHEKRTEST')
        updates,last=plan_updates({},{key:(3,[*key,2]),old:(2,[*old,9])},3,DAY,DAY)
        self.assertEqual(updates,[(3,[*key,0])]);self.assertEqual(last,3)

    def test_empty_populated_day_aborts_changes(self):
        key=('2026-10-30','안국','PHEKRTEST')
        with self.assertRaises(RuntimeError):validate_snapshot({},{key:(2,[*key,5])},DAY,DAY)

    def test_lost_response_repeat_keeps_new_daily_row_unique(self):
        key=('2026-10-30','안국','PHEKRTEST');s={key:[*key,2]}
        self.assertEqual(plan_updates(s,{},1,DAY,DAY),([(2,[*key,2])],2))
        self.assertEqual(plan_updates(s,{key:(2,[*key,2])},2,DAY,DAY),([],2))

    def test_zero_net_rows_are_retained(self):
        s=collect_snapshot(lambda *args:([order(2,1),order(-2,2)],1,100),DAY,DAY)
        self.assertEqual(next(iter(s.values()))[3],0)

if __name__=='__main__':unittest.main()
