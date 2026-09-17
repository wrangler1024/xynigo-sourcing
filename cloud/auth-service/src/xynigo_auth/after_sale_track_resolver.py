"""Tenant-scoped, read-only discovery of already known refund identities."""

from sqlalchemy import case, cast, func, literal, select, true, union_all
from sqlalchemy.dialects.postgresql import JSONB

from .models import AfterSaleClaimResult, AfterSaleRefundTracking


MAX_HISTORY_ROWS = 5000
MAX_TRACK_ITEMS = 500


def _claim_identities(session, tenant_id, serials=None):
    """Project refund IDs in SQL, not entire receipt/account JSON documents."""
    claim = AfterSaleClaimResult
    conditions = [claim.tenant_id == tenant_id]
    if serials is not None:
        conditions.append(claim.environment_serial.in_(serials))
    if session.get_bind().dialect.name == "postgresql":
        raw = cast(claim.refunds, JSONB)
        array = case((func.jsonb_typeof(raw) == "array", raw),
                     else_=cast(literal("[]"), JSONB))
        entries = func.jsonb_array_elements(array).table_valued("value").alias("receipt")
        bill = entries.c.value.op("->>")("refundBillId")
    else:
        array = case((func.json_type(claim.refunds) == "array", claim.refunds), else_="[]")
        entries = func.json_each(array).table_valued("value", "type").alias("receipt")
        bill = case((entries.c.type == "object",
                     func.json_extract(entries.c.value, "$.refundBillId")), else_=None)
    columns = [claim.environment_serial, claim.order_no, claim.store_name]
    primary = select(*columns, claim.refund_bill_id).where(*conditions)
    packages = select(*columns, bill.label("refund_bill_id")).select_from(claim).join(
        entries, true()).where(*conditions)
    return union_all(primary, packages).subquery()


def resolve_after_sale_track_items(session, tenant_id, serials):
    serials = list(dict.fromkeys(serials))
    history_orders = {serial: set() for serial in serials}
    known_orders = {serial: set() for serial in serials}
    incomplete = {serial: 0 for serial in serials}
    identities = {}

    def collect(serial, order, bill, store):
        order = str(order or "").strip()
        bill = str(bill or "").strip()
        if not order:
            incomplete[serial] += 1
            return
        history_orders[serial].add(order)
        if not bill:
            return
        if len(order) > 32 or len(bill) > 32:
            incomplete[serial] += 1
            return
        known_orders[serial].add(order)
        owners = identities.setdefault(bill, {})
        owners.setdefault((serial, order), str(store or "")[:128])
        if len(identities) > MAX_TRACK_ITEMS:
            raise ValueError("匹配超过 500 个退款单，请减少环境数量，或从提交历史选择批次回访；本次未发起任务。")

    claim = AfterSaleClaimResult
    track = AfterSaleRefundTracking
    record_count = 0
    for model in (claim, track):
        bounded = select(model.id).where(
            model.tenant_id == tenant_id, model.environment_serial.in_(serials)
        ).limit(MAX_HISTORY_ROWS + 1).subquery()
        record_count += session.scalar(select(func.count()).select_from(bounded))
    if record_count > MAX_HISTORY_ROWS:
        raise ValueError("这些环境的历史记录超过查询上限，请减少环境数量，或从提交历史选择批次回访；本次未发起任务。")

    scoped = _claim_identities(session, tenant_id, serials)
    query = select(scoped).distinct().order_by(scoped.c.order_no, scoped.c.refund_bill_id)
    # Small batches also bound the driver buffer when a large historical JSON
    # array contains repeated receipt IDs. Only four short strings cross to Python.
    with session.execute(query.execution_options(yield_per=100)) as records:
        for serial, order, store, bill in records:
            collect(serial, order, bill, store)
    tracks = select(
        track.environment_serial, track.order_no, track.refund_bill_id, track.store_name,
    ).where(track.tenant_id == tenant_id, track.environment_serial.in_(serials))
    with session.execute(tracks.execution_options(yield_per=100)) as records:
        for serial, order, bill, store in records:
            collect(serial, order, bill, store)

    # Validate the same bill's identity across ALL tenant history. Selection
    # of one environment cannot hide a conflict in another environment.
    blocked = set()
    if identities:
        global_claims = _claim_identities(session, tenant_id)
        query = select(global_claims.c.refund_bill_id, global_claims.c.environment_serial,
                       global_claims.c.order_no).where(
                           global_claims.c.refund_bill_id.in_(identities)).distinct()
        with session.execute(query.execution_options(yield_per=100)) as records:
            for bill, serial, order in records:
                if (serial, order) not in identities[bill]:
                    blocked.add(bill)
        # Preserve established tracking bindings too, including records outside
        # the selected environments and legacy records without an environment.
        bindings = session.execute(select(
            track.refund_bill_id, track.environment_serial, track.order_no,
        ).where(track.tenant_id == tenant_id, track.refund_bill_id.in_(identities))).all()
        for bill, serial, order in bindings:
            order = str(order or "")
            if (serial and (serial, order) not in identities[bill]) or (
                    not serial and order and any(key[1] != order for key in identities[bill])):
                blocked.add(bill)
    items = []
    conflicts = {serial: set() for serial in serials}
    for bill, owners in identities.items():
        if len(owners) != 1 or bill in blocked:
            for serial, _ in owners:
                conflicts[serial].add(bill)
            continue
        (serial, order), store = next(iter(owners.items()))
        items.append({"environmentSerial": serial, "orderNo": order,
                      "refundBillId": bill, "storeName": store})
    serial_index = {serial: index for index, serial in enumerate(serials)}
    items.sort(key=lambda item: (serial_index[item["environmentSerial"]],
                                 item["orderNo"], item["refundBillId"]))
    environments = []
    for serial in serials:
        count = sum(item["environmentSerial"] == serial for item in items)
        missing = len(history_orders[serial] - known_orders[serial]) + incomplete[serial]
        notes = []
        if conflicts[serial]:
            notes.append(f"{len(conflicts[serial])} 个退款单归属不一致，已排除，请核对历史记录")
        if missing:
            notes.append(f"{missing} 个订单或记录缺少完整订单号/退款单号，无法自动回访")
        if not count and not notes:
            notes.append("系统没有该环境的退款记录；不代表平台没有退款，可用完整订单和退款单号回访")
        environments.append({"environmentSerial": serial, "billCount": count,
                             "status": ("partial" if notes else "matched") if count else "unmatched",
                             "note": "；".join(notes)})
    return {"items": items, "environments": environments,
            "environmentCount": len(serials), "billCount": len(items),
            "matchedEnvironmentCount": sum(env["billCount"] > 0 for env in environments)}
