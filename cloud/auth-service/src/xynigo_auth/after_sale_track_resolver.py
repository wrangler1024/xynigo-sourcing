"""Tenant-scoped, read-only discovery of already known refund identities."""

from sqlalchemy import select

from .models import AfterSaleClaimResult, AfterSaleRefundTracking


MAX_HISTORY_ROWS = 5000
MAX_TRACK_ITEMS = 500


def resolve_after_sale_track_items(session, tenant_id, serials):
    serials = list(dict.fromkeys(serials))
    history_orders = {serial: set() for serial in serials}
    known_orders = {serial: set() for serial in serials}
    incomplete = {serial: 0 for serial in serials}
    identities = {}

    def collect(serial, order, bill, store):
        order = str(order or "").strip().upper()
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

    # Project only identity fields: never load screenshots, account details or
    # entire submission snapshots just to resolve a tracking scope.
    claim = AfterSaleClaimResult
    claims = session.execute(select(
        claim.environment_serial, claim.order_no, claim.refund_bill_id,
        claim.refunds, claim.store_name,
    ).where(claim.tenant_id == tenant_id, claim.environment_serial.in_(serials))
        .order_by(claim.created_at.desc(), claim.id.desc()).limit(MAX_HISTORY_ROWS + 1)).all()
    track = AfterSaleRefundTracking
    tracks = session.execute(select(
        track.environment_serial, track.order_no, track.refund_bill_id, track.store_name,
    ).where(track.tenant_id == tenant_id, track.environment_serial.in_(serials))
        .order_by(track.created_at.desc(), track.id.desc()).limit(MAX_HISTORY_ROWS + 1)).all()
    if len(claims) + len(tracks) > MAX_HISTORY_ROWS:
        raise ValueError("这些环境的历史记录超过查询上限，请减少环境数量，或从提交历史选择批次回访；本次未发起任务。")
    for serial, order, primary_bill, refunds, store in claims:
        # Keep both the legacy primary bill and every package's receipt.
        bills = [primary_bill] + [r.get("refundBillId") for r in (refunds or [])
                                  if isinstance(r, dict)]
        for bill in dict.fromkeys(str(b or "").strip() for b in bills):
            collect(serial, order, bill, store)
    for serial, order, bill, store in tracks:
        collect(serial, order, bill, store)

    # A tracking record already bound to a different identity must not be
    # rebound by an old claim snapshot, even outside the requested environments.
    blocked = set()
    if identities:
        bindings = session.execute(select(
            track.refund_bill_id, track.environment_serial, track.order_no,
        ).where(track.tenant_id == tenant_id, track.refund_bill_id.in_(identities))).all()
        for bill, serial, order in bindings:
            order = str(order or "").strip().upper()
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
