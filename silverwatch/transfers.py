from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection

from pydantic import ValidationError

from silverwatch import cases, evidence
from silverwatch.contracts import CaseTransfer, TransferResolution
from silverwatch.digest import digest
from silverwatch.errors import conflict, invalid, missing
from silverwatch.ledger import append, current_entities

SOURCE = "case-transfer"


def _rows(db: Connection, *, cut: int | None = None,
          as_of: datetime | None = None) -> list[dict]:
    return current_entities(db, "case_transfer", cut=cut, as_of=as_of)


def for_case(db: Connection, case_id: str, *, cut: int | None = None,
             as_of: datetime | None = None) -> list[dict]:
    rows = [row for row in _rows(db, cut=cut, as_of=as_of)
            if row["payload"]["case_id"] == case_id]
    return sorted(rows, key=lambda row: row["seq"])


def pending_for_case(db: Connection, case_id: str, *, cut: int | None = None,
                     as_of: datetime | None = None) -> dict | None:
    pending = [row["payload"] for row in for_case(db, case_id, cut=cut, as_of=as_of)
               if row["payload"]["status"] == "requested"]
    return pending[-1] if pending else None


def responsibility_map(db: Connection, *, cut: int | None = None,
                       as_of: datetime | None = None) -> dict[str, dict]:
    """每个案件在指定截点的责任地区；未接收的转交不转移责任。"""
    batches = {row["entity_id"]: row["payload"]
               for row in current_entities(db, "touch", cut=cut, as_of=as_of)}
    grouped: dict[str, list[dict]] = {}
    for row in _rows(db, cut=cut, as_of=as_of):
        grouped.setdefault(row["payload"]["case_id"], []).append(row)
    result = {}
    for case_row in cases.current_cases(db, cut=cut, as_of=as_of):
        case_id = case_row["entity_id"]
        batch = batches.get(case_row["payload"]["batch_id"])
        accepted = None
        pending = None
        for row in sorted(grouped.get(case_id, []), key=lambda item: item["seq"]):
            payload = row["payload"]
            if payload["status"] == "accepted":
                accepted = payload
            elif payload["status"] == "requested":
                pending = payload
        result[case_id] = {
            "case_id": case_id,
            "responsible_region": (accepted["to_region"] if accepted
                                   else batch["region"] if batch else None),
            "origin_region": batch["region"] if batch else None,
            "origin_district": batch["district"] if batch else None,
            "accepted_transfer": accepted,
            "pending_transfer": pending,
        }
    return result


def case_responsibility(db: Connection, case_id: str, *, cut: int | None = None,
                        as_of: datetime | None = None) -> dict:
    responsibility = responsibility_map(db, cut=cut, as_of=as_of)
    if case_id not in responsibility:
        raise missing("case", case_id)
    return responsibility[case_id]


def request(db: Connection, body: CaseTransfer) -> dict:
    if body.status != "requested":
        raise invalid("transfer_status", "转交发起不能携带接收结论")
    payload = body.model_dump(mode="json")
    existing = next((row for row in _rows(db) if row["entity_id"] == body.transfer_id), None)
    if existing is not None:
        # 相同来源修订安全重放；内容不同则由账本拒绝改写。
        return {**payload, **append(db, "case_transfer", body.transfer_id, 1, SOURCE,
                                    payload, body.requested_at)}
    case_row = next((row for row in cases.current_cases(db)
                     if row["entity_id"] == body.case_id), None)
    if case_row is None:
        raise missing("case", body.case_id)
    if case_row["payload"]["status"] == "closed":
        raise conflict("case_closed", "已关闭的案件不能发起跨地区转交")
    responsibility = case_responsibility(db, body.case_id)
    if responsibility["responsible_region"] != body.from_region:
        raise conflict("transfer_origin_changed", "案件责任地区与转交发起方不一致",
                       {"current_region": responsibility["responsible_region"]})
    if responsibility["pending_transfer"] is not None:
        raise conflict("transfer_pending", "案件已有等待接收结论的转交",
                       {"transfer_id": responsibility["pending_transfer"]["transfer_id"]})
    if body.supersedes is not None:
        prior = next((row for row in _rows(db) if row["entity_id"] == body.supersedes), None)
        if prior is None:
            raise missing("case_transfer", body.supersedes)
        prior_body = prior["payload"]
        if prior_body["case_id"] != body.case_id:
            raise invalid("transfer_supersedes", "再次转交必须承接同一案件的前次转交")
        if prior_body["status"] != "rejected":
            raise conflict("transfer_not_rejected", "只有被拒收的转交才能补充资料后再次转交")
    if body.supplement_refs:
        available = {row["evidence_id"] for row in evidence.for_case(db, body.case_id)
                     if datetime.fromisoformat(row["observed_at"]) <= body.requested_at}
        absent = sorted(set(body.supplement_refs) - available)
        if absent:
            raise invalid("transfer_supplement", "补充材料必须是案件已登记的核实材料",
                          {"missing": absent})
    result = append(db, "case_transfer", body.transfer_id, 1, SOURCE, payload, body.requested_at)
    return {**payload, **result}


def resolve(db: Connection, transfer_id: str, body: TransferResolution) -> dict:
    row = next((row for row in _rows(db) if row["entity_id"] == transfer_id), None)
    if row is None:
        raise missing("case_transfer", transfer_id)
    current = row["payload"]
    merged = {**current, "status": body.decision, "decided_by": body.decided_by,
              "decided_at": body.decided_at.isoformat(), "decision_note": body.note}
    try:
        resolved = CaseTransfer.model_validate(merged)
    except ValidationError as exc:
        raise invalid("transfer_resolution", "接收决定不符合转交约束",
                      exc.errors(include_url=False)) from exc
    payload = resolved.model_dump(mode="json")
    if current["status"] != "requested":
        if digest(current) == digest(payload):
            return {**payload, "seq": row["seq"], "digest": row["digest"], "replayed": True}
        raise conflict("transfer_sealed", "转交已有接收结论，不能改写")
    result = append(db, "case_transfer", transfer_id, row["revision"] + 1, SOURCE,
                    payload, body.decided_at)
    return {**payload, **result}


def regional_summary(db: Connection, *, region: str, cut: int | None = None,
                     as_of: datetime | None = None) -> dict:
    """地区在指定截点承接与转出的案件；每个案件只计入一个责任地区。"""
    responsibility = responsibility_map(db, cut=cut, as_of=as_of)
    findings = cases.all_findings(db, cut=cut, as_of=as_of)
    case_rows = {row["entity_id"]: row["payload"]
                 for row in cases.current_cases(db, cut=cut, as_of=as_of)}
    incoming = []
    outgoing = []
    for case_id in sorted(responsibility):
        resp = responsibility[case_id]
        accepted = resp["accepted_transfer"]
        if accepted is None or resp["responsible_region"] == resp["origin_region"]:
            continue
        case = case_rows[case_id]
        base = {"case_id": case_id, "transfer_id": accepted["transfer_id"],
                "finding": findings[case_id]["finding"],
                "severity": case["severity"], "status": case["status"],
                "accepted_at": accepted["decided_at"], "reason": accepted["reason"]}
        if resp["responsible_region"] == region and resp["origin_region"] is not None:
            incoming.append({**base, "from_region": accepted["from_region"],
                             "origin_region": resp["origin_region"],
                             "origin_district": resp["origin_district"]})
        if resp["origin_region"] == region:
            outgoing.append({**base, "to_region": resp["responsible_region"]})
    counts = {"substantiated": 0, "pending": 0, "inconclusive": 0}
    for item in incoming:
        if item["finding"] in counts:
            counts[item["finding"]] += 1
    return {"transferred_in": sorted(incoming, key=lambda item: (item["accepted_at"], item["case_id"])),
            "transferred_out": sorted(outgoing, key=lambda item: (item["accepted_at"], item["case_id"])),
            "transferred_in_counts": counts}


def case_transfer_view(db: Connection, case_id: str, *, cut: int | None = None) -> dict:
    responsibility = case_responsibility(db, case_id, cut=cut)
    records = [{**row["payload"], "event_seq": row["seq"], "digest": row["digest"]}
               for row in for_case(db, case_id, cut=cut)]
    return {"case_id": case_id, "responsibility": responsibility, "transfers": records}
