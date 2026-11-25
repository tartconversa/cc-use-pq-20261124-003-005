from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from silverwatch import cases, evidence, planning, registry, review, service, transfers
from silverwatch.contracts import (AnalysisPolicy, Approval, Case, CaseEvidence,
                                   CaseTransfer, NeedBatch, Provider, Remediation,
                                   ReviewDecision, TouchBatch, TransferResolution)
from silverwatch.ledger import current_cut
from silverwatch.storage import Store


def run_fixture(path: str | Path) -> dict:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="silverwatch-") as directory:
        store = Store(Path(directory) / "silverwatch.sqlite3")
        with store.transaction(write=True) as db:
            provider = Provider.model_validate(fixture["provider"])
            registry.register_provider(db, provider, "provider-register", 1)
            touch = TouchBatch.model_validate(fixture["touch"])
            registry.register_touch(db, touch)
            need = NeedBatch.model_validate(fixture["need"])
            registry.register_need(db, need)
            case = Case.model_validate(fixture["case"])
            cases.create(db, case, "case-intake", 1)
            material = CaseEvidence.model_validate(fixture["evidence"])
            evidence.add(db, material)
            for entry in fixture.get("transfers", []):
                if entry.get("add_evidence"):
                    evidence.add(db, CaseEvidence.model_validate(entry["add_evidence"]))
                transfer = CaseTransfer.model_validate(entry["request"])
                transfers.request(db, transfer)
                if entry.get("resolution"):
                    transfers.resolve(db, transfer.transfer_id,
                                      TransferResolution.model_validate(entry["resolution"]))
            for decision_data in fixture["decisions"]:
                review.decide(db, ReviewDecision.model_validate(decision_data))
            action = Remediation.model_validate(fixture["remediation"])
            cases.add_remediation(db, action)
            verified = Remediation.model_validate(fixture["verified_remediation"])
            cases.update_remediation(db, verified.action_id, verified, 2)
            review.close(db, case.case_id, "substantiated", 1)
            analysis_input = fixture["analysis"]
            policy = AnalysisPolicy.model_validate(analysis_input["policy"])
            created = service.analyze(db, {**analysis_input, "policy": policy.model_dump(mode="json"),
                                           "through_cut": current_cut(db)})
            for role, reviewer_id in (("service_planning", "planner"),
                                      ("consumer_protection", "ombuds")):
                body = Approval(approval_id=f"approve-{role.replace('_', '-')}", analysis_id=created["analysis_id"],
                                reviewer=reviewer_id, role=role, approved=True,
                                analysis_digest=created["digest"], rationale="Reviewed the frozen result.")
                service.approve(db, body.model_dump(mode="json"))
            review_state = service.review_status(db, created["analysis_id"])
            bulletin = service.publish(db, created["analysis_id"], 0)
            plan = planning.plan_region(db, region="suburban",
                                        period_end=datetime.fromisoformat("2026-11-01T00:00:00+00:00"))
            handover = None
            if fixture.get("transfers"):
                view = transfers.case_transfer_view(db, case.case_id)
                handover = {"responsible_region": view["responsibility"]["responsible_region"],
                            "origin_region": view["responsibility"]["origin_region"],
                            "transfer_count": len(view["transfers"]),
                            "final_status": view["transfers"][-1]["status"]}
            return {"scenario": fixture["scenario"], "analysis": created,
                    "review_ready": review_state["ready"],
                    "bulletin": {"edition": bulletin["edition"],
                                 "digest": bulletin["bulletin_digest"],
                                 "case_counts": bulletin["document"]["case_counts"],
                                 "area_count": len(bulletin["document"]["areas"])},
                    "handover": handover,
                    "service_plan": plan["totals"]}
