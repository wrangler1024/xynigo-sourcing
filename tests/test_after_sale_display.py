from purchase_tool.after_sale_display import delivery_date, complete_delivery_date, order_item_summary, delivery_summary


def test_display_dates_validate_calendar_without_timezone_conversion():
    assert delivery_date('7 Sep 2026 11:31:55') == '2026-09-07'
    assert delivery_date('07 septiembre 2026 11:31:55') == '2026-09-07'
    assert delivery_date('2026-09-07T23:59:59-06:00') == '2026-09-07'
    assert delivery_date('31 Feb 2026') == ''
    assert delivery_date('Entrega: 07-17 Sep') == ''


def test_all_packages_must_have_distinct_complete_delivery_evidence():
    rows = [{'packageNo':'P1','delivered':True,'deliveredAt':'7 Sep 2026'},
            {'packageNo':'P2','delivered':True,'deliveredAt':'9 Sep 2026'}]
    assert complete_delivery_date(rows, 2) == '2026-09-09'
    assert complete_delivery_date(rows[:1], 2) == ''
    assert complete_delivery_date([rows[0], rows[0]], 2) == ''
    assert complete_delivery_date([rows[0],dict(rows[1],delivered=False)], 2) == ''
    assert complete_delivery_date([rows[0],dict(rows[1],deliveredAt='')], 2) == ''


def test_quantities_are_not_inferred_from_photos_or_partial_item_data():
    assert order_item_summary({'goodsImages':['a','b']})['itemCount'] is None
    facts=order_item_summary({'itemCount':2,'goodsImages':['a','a'], 'goodsItems':[{'goodsImg':'a','quantity':None}]})
    assert facts['itemCount']==2 and len(facts['goodsImages'])==1
    assert facts['goodsItems'][0]['quantity'] is None
    assert order_item_summary({'itemCount':True})['itemCount'] is None


def test_partial_multi_package_date_never_used_for_filter():
    order={'packages':[{'packageNo':'P1'},{'packageNo':'P2'}], 'deliveredAt':'07 Sep 2026'}
    assert delivery_summary({'delivered':True},order)['deliveredDate']==''
    single=dict(order,packages=[{'packageNo':'P1'}])
    assert delivery_summary({'delivered':True,'totalPackageCount':1},single)['deliveredDate']=='2026-09-07'
    assert delivery_summary({},single)['deliveryDateStatus']=='unknown'
    assert delivery_summary({'delivered':True},single)['deliveredDate']==''


def test_real_web_filter_and_progress_functions():
    import subprocess
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/fixtures/after_sale_filters_ui.cjs')],cwd=root,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
