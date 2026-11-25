from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from silverwatch import analysis, cases, comparison, evidence, queries, reconciliation, review, transfers, workqueue
from silverwatch.api import create_app
from silverwatch.contracts import (AnalysisPolicy, Case, CaseEvidence, CaseTransfer,
                                   TransferResolution)
from silverwatch.errors import Problem
from silverwatch.ledger import audit_integrity, current_cut
from tests.support import (DatabaseCase, case_data, evidence_data, instant,
                           provider_data, resolve_case, seed_case, touch_data)


def transfer_data(**updates):
    value = {"transfer_id": "transfer-one", "case_id": "case-one",
             "from_region": "suburban", "to_region": "urban",
             "reason": "Contract and payment records are held by the urban office.",
             "requested_by": "operator-one", "requested_at": "2026-10-12T09:00:00Z"}
    return value | updates


def resolution_data(**updates):
    value = {"decision": "accepted", "decided_by": "operator-two",
             "decided_at": "2026-10-13T09:00:00Z",
             "note": "The urban office takes over contract verification."}
    return value | updates


def policy_data(**updates):
    value = {"policy_id": "policy-one", "minimum_exposure": 100,
             "minimum_supply_coverage_bp": 8000, "max_substantiated_rate_bp": 50,
             "severe_open_cases_block": True, "require_verified_remediation": True,
             "minimum_touch_coverage_bp": 8000}
    return value | updates


def make_transfer(**updates):
    return CaseTransfer.model_validate(transfer_data(**updates))


def make_resolution(**updates):
    return TransferResolution.model_validate(resolution_data(**updates))


def make_policy():
    return AnalysisPolicy.model_validate(policy_data())


class TransferRequestTests(DatabaseCase, unittest.TestCase):
    def test_pending_transfer_keeps_case_in_origin_queue(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            transfers.request(db, make_transfer())
            responsibility = transfers.case_responsibility(db, "case-one")
            self.assertEqual(responsibility["responsible_region"], "suburban")
            self.assertEqual(responsibility["pending_transfer"]["transfer_id"], "transfer-one")
            origin = workqueue.investigation_queue(db, region="suburban",
                                                   as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(origin["count"], 1)
            note = origin["items"][0]["transfer"]
            self.assertEqual(note["state"], "awaiting_region_decision")
            self.assertEqual(note["to_region"], "urban")
            receiving = workqueue.investigation_queue(db, region="urban",
                                                      as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(receiving["count"], 0)

    def test_request_must_match_current_responsible_region(self):
        with self.transaction() as db:
            seed_case(db)
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(from_region="urban", to_region="suburban"))
            self.assertEqual(captured.exception.code, "transfer_origin_changed")

    def test_request_for_unknown_case_is_missing(self):
        with self.transaction() as db:
            seed_case(db)
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(case_id="other-case"))
            self.assertEqual(captured.exception.status, 404)

    def test_same_region_transfer_is_rejected_by_contract(self):
        with self.assertRaises(ValueError):
            make_transfer(from_region="suburban", to_region="suburban")

    def test_second_request_while_pending_is_conflict(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(transfer_id="transfer-two"))
            self.assertEqual(captured.exception.code, "transfer_pending")

    def test_identical_request_replay_does_not_duplicate_events(self):
        with self.transaction() as db:
            seed_case(db)
            first = transfers.request(db, make_transfer())
            cut = current_cut(db)
            second = transfers.request(db, make_transfer())
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(current_cut(db), cut)

    def test_transfer_id_cannot_be_reused_for_different_content(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(to_region="rural"))
            self.assertEqual(captured.exception.code, "source_revision_changed")

    def test_closed_case_cannot_be_transferred(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db, first="not_substantiated", second="not_substantiated")
            review.close(db, "case-one", "not_substantiated", 1)
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer())
            self.assertEqual(captured.exception.code, "case_closed")


class TransferResolutionTests(DatabaseCase, unittest.TestCase):
    def test_acceptance_moves_case_to_receiving_region_queue(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            responsibility = transfers.case_responsibility(db, "case-one")
            self.assertEqual(responsibility["responsible_region"], "urban")
            self.assertEqual(responsibility["origin_region"], "suburban")
            origin = workqueue.investigation_queue(db, region="suburban",
                                                   as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(origin["count"], 0)
            receiving = workqueue.investigation_queue(db, region="urban",
                                                      as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(receiving["count"], 1)
            item = receiving["items"][0]
            self.assertEqual(item["transfer"]["state"], "transferred_in")
            self.assertEqual(item["transfer"]["from_region"], "suburban")
            self.assertEqual(item["district"], "district-a")

    def test_rejection_keeps_origin_responsible(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution(
                decision="rejected", note="Payment records were not attached."))
            responsibility = transfers.case_responsibility(db, "case-one")
            self.assertEqual(responsibility["responsible_region"], "suburban")
            self.assertIsNone(responsibility["accepted_transfer"])
            queue = workqueue.transfer_queue(db, region="suburban",
                                             as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(len(queue["resolved"]), 1)
            self.assertEqual(queue["resolved"][0]["status"], "rejected")

    def test_resolution_requires_a_distinct_decider(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            with self.assertRaises(Problem) as captured:
                transfers.resolve(db, "transfer-one", make_resolution(decided_by="operator-one"))
            self.assertEqual(captured.exception.code, "transfer_resolution")

    def test_resolution_cannot_precede_the_request(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            with self.assertRaises(Problem) as captured:
                transfers.resolve(db, "transfer-one",
                                  make_resolution(decided_at="2026-10-11T09:00:00Z"))
            self.assertEqual(captured.exception.code, "transfer_resolution")

    def test_resolution_for_unknown_transfer_is_missing(self):
        with self.transaction() as db:
            seed_case(db)
            with self.assertRaises(Problem) as captured:
                transfers.resolve(db, "transfer-none", make_resolution())
            self.assertEqual(captured.exception.status, 404)

    def test_resolved_transfer_is_sealed_but_replayable(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            replay = transfers.resolve(db, "transfer-one", make_resolution())
            self.assertTrue(replay["replayed"])
            with self.assertRaises(Problem) as captured:
                transfers.resolve(db, "transfer-one",
                                  make_resolution(note="A different conclusion."))
            self.assertEqual(captured.exception.code, "transfer_sealed")

    def test_close_is_blocked_until_transfer_is_decided(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db, first="not_substantiated", second="not_substantiated")
            transfers.request(db, make_transfer())
            with self.assertRaises(Problem) as captured:
                review.close(db, "case-one", "not_substantiated", 1)
            self.assertEqual(captured.exception.code, "transfer_pending")
            transfers.resolve(db, "transfer-one", make_resolution(
                decision="rejected", note="Handled locally after all."))
            review.close(db, "case-one", "not_substantiated", 1)
            self.assertEqual(review.case_status(db, "case-one")["case"]["status"], "closed")


class ReTransferTests(DatabaseCase, unittest.TestCase):
    def reject_first(self, db):
        transfers.request(db, make_transfer())
        transfers.resolve(db, "transfer-one", make_resolution(
            decision="rejected", note="Need the contract copy."))

    def test_retransfer_requires_a_rejected_prior_transfer(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(
                    transfer_id="transfer-two", from_region="urban", to_region="suburban",
                    supersedes="transfer-one", supplement_refs=["evidence-one"]))
            self.assertEqual(captured.exception.code, "transfer_not_rejected")

    def test_retransfer_must_cite_supplementary_material(self):
        with self.assertRaises(ValueError):
            make_transfer(transfer_id="transfer-two", supersedes="transfer-one")

    def test_supplement_must_be_registered_case_evidence(self):
        with self.transaction() as db:
            seed_case(db)
            self.reject_first(db)
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(
                    transfer_id="transfer-two", supersedes="transfer-one",
                    supplement_refs=["unknown-evidence"]))
            self.assertEqual(captured.exception.code, "transfer_supplement")

    def test_supplement_must_predate_the_new_request(self):
        with self.transaction() as db:
            seed_case(db)
            self.reject_first(db)
            evidence.add(db, CaseEvidence.model_validate(evidence_data(
                evidence_id="late-contract", observed_at="2026-10-14T00:00:00Z")))
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(
                    transfer_id="transfer-two", requested_at="2026-10-12T13:00:00Z",
                    supersedes="transfer-one", supplement_refs=["late-contract"]))
            self.assertEqual(captured.exception.code, "transfer_supplement")

    def test_supersedes_must_reference_the_same_case(self):
        with self.transaction() as db:
            seed_case(db)
            cases.create(db, Case.model_validate(case_data(case_id="case-two")), "case-feed", 1)
            transfers.request(db, make_transfer(transfer_id="transfer-other", case_id="case-two"))
            transfers.resolve(db, "transfer-other", make_resolution(
                decision="rejected", note="Not an urban matter."))
            with self.assertRaises(Problem) as captured:
                transfers.request(db, make_transfer(
                    transfer_id="transfer-two", supersedes="transfer-other",
                    supplement_refs=["evidence-one"]))
            self.assertEqual(captured.exception.code, "transfer_supersedes")

    def test_retransfer_with_supplement_can_be_accepted(self):
        with self.transaction() as db:
            seed_case(db)
            self.reject_first(db)
            evidence.add(db, CaseEvidence.model_validate(evidence_data(
                evidence_id="contract-copy", evidence_type="contract",
                observed_at="2026-10-12T12:00:00Z")))
            transfers.request(db, make_transfer(
                transfer_id="transfer-two", requested_at="2026-10-12T13:00:00Z",
                supersedes="transfer-one", supplement_refs=["contract-copy"]))
            transfers.resolve(db, "transfer-two", make_resolution())
            responsibility = transfers.case_responsibility(db, "case-one")
            self.assertEqual(responsibility["responsible_region"], "urban")
            view = transfers.case_transfer_view(db, "case-one")
            self.assertEqual([row["status"] for row in view["transfers"]],
                             ["rejected", "accepted"])
            self.assertEqual(view["transfers"][1]["supersedes"], "transfer-one")
            self.assertEqual(view["transfers"][1]["supplement_refs"], ["contract-copy"])


class RegionalAttributionTests(DatabaseCase, unittest.TestCase):
    def run_analysis(self, db, region, *, cut=None, as_of="2026-11-15T00:00:00Z"):
        return analysis.run(db, cut=cut if cut is not None else current_cut(db),
                            as_of=instant(as_of), region=region, service_kind="meal",
                            cohort="70_79", policy=make_policy())

    def test_accepted_transfer_counts_case_once_across_regions(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            origin = self.run_analysis(db, "suburban")
            self.assertEqual(origin["case_counts"]["substantiated"], 0)
            self.assertEqual(origin["transfers"]["transferred_out"][0]["case_id"], "case-one")
            self.assertEqual(origin["transfers"]["transferred_out"][0]["finding"], "substantiated")
            area = origin["areas"][0]
            self.assertEqual(area["substantiated"], 0)
            self.assertEqual(area["transferred_out"][0]["to_region"], "urban")
            receiving = self.run_analysis(db, "urban")
            self.assertEqual(receiving["case_counts"]["substantiated"], 0)
            self.assertEqual(receiving["transfers"]["transferred_in_counts"]["substantiated"], 1)
            incoming = receiving["transfers"]["transferred_in"][0]
            self.assertEqual(incoming["case_id"], "case-one")
            self.assertEqual(incoming["origin_district"], "district-a")
            total = (origin["case_counts"]["substantiated"]
                     + origin["transfers"]["transferred_in_counts"]["substantiated"]
                     + receiving["case_counts"]["substantiated"]
                     + receiving["transfers"]["transferred_in_counts"]["substantiated"])
            self.assertEqual(total, 1)

    def test_pending_transfer_keeps_case_in_origin_analysis(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            origin = self.run_analysis(db, "suburban")
            self.assertEqual(origin["case_counts"]["pending"], 1)
            self.assertEqual(origin["transfers"]["transferred_out"], [])
            receiving = self.run_analysis(db, "urban")
            self.assertEqual(receiving["transfers"]["transferred_in"], [])

    def test_historical_cut_keeps_case_in_origin_region(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            transfers.request(db, make_transfer())
            before_accept = current_cut(db)
            transfers.resolve(db, "transfer-one", make_resolution())
            historical = self.run_analysis(db, "suburban", cut=before_accept)
            self.assertEqual(historical["case_counts"]["substantiated"], 1)
            self.assertEqual(historical["transfers"]["transferred_out"], [])
            current = self.run_analysis(db, "suburban")
            self.assertEqual(current["case_counts"]["substantiated"], 0)
            self.assertEqual(len(current["transfers"]["transferred_out"]), 1)

    def test_district_overview_attributes_responsibility_once(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            origin = queries.district_overview(db, "suburban",
                                               as_of=instant("2026-11-15T00:00:00Z"))
            self.assertEqual(origin["case_total"], 0)
            self.assertEqual(origin["transferred_out"][0]["case_id"], "case-one")
            self.assertEqual(origin["areas"][0]["substantiated"], 0)
            receiving = queries.district_overview(db, "urban",
                                                  as_of=instant("2026-11-15T00:00:00Z"))
            self.assertEqual(receiving["case_total"], 1)
            self.assertEqual(receiving["transferred_in"][0]["case_id"], "case-one")
            self.assertEqual(receiving["transferred_in"][0]["from_region"], "suburban")

    def test_region_comparison_reports_transfers_separately(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            result = comparison.compare_regions(db, regions=["suburban", "urban"],
                                                as_of=instant("2026-11-15T00:00:00Z"),
                                                policy=make_policy())
            by_region = {row["region"]: row for row in result["regions"]}
            self.assertEqual(by_region["suburban"]["confirmed"], 0)
            self.assertEqual(by_region["suburban"]["transferred_out"], 1)
            self.assertEqual(by_region["urban"]["transferred_in_confirmed"], 1)


class TransferTraceabilityTests(DatabaseCase, unittest.TestCase):
    def test_timeline_shows_transfer_chain_and_keeps_provider_identity(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            timeline = queries.case_timeline(db, "case-one")
            self.assertEqual(timeline["case"]["provider_id"], "provider-one")
            self.assertEqual(timeline["responsibility"]["responsible_region"], "urban")
            self.assertEqual(timeline["responsibility"]["origin_region"], "suburban")
            self.assertEqual(len(timeline["transfers"]), 1)
            profile = queries.provider_profile(db, "provider-one")
            self.assertEqual(profile["provider"]["region"], "suburban")
            self.assertEqual(profile["provider"]["display_name"], "Community Meals")

    def test_transfer_does_not_trigger_provider_scope_findings(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            self.assertTrue(reconciliation.reconcile(db)["complete"])
            self.assertTrue(queries.validate_references(db)["valid"])

    def test_audit_integrity_covers_transfer_events(self):
        with self.transaction() as db:
            seed_case(db)
            transfers.request(db, make_transfer())
            transfers.resolve(db, "transfer-one", make_resolution())
            result = audit_integrity(db)
            self.assertTrue(result["valid"])
            self.assertEqual(result["event_counts"]["case_transfer"], 2)

    def test_finding_change_during_transfer_is_visible_to_both_regions(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            transfers.request(db, make_transfer())
            resolve_case(db)
            origin = workqueue.investigation_queue(db, region="suburban",
                                                   as_of=instant("2026-10-15T00:00:00Z"))
            item = origin["items"][0]
            self.assertEqual(item["finding"], "substantiated")
            self.assertEqual(item["transfer"]["state"], "awaiting_region_decision")
            transfers.resolve(db, "transfer-one", make_resolution())
            receiving = workqueue.investigation_queue(db, region="urban",
                                                      as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(receiving["items"][0]["finding"], "substantiated")
            self.assertEqual(receiving["items"][0]["transfer"]["state"], "transferred_in")
            summary = transfers.regional_summary(db, region="urban")
            self.assertEqual(summary["transferred_in"][0]["finding"], "substantiated")

    def test_transfer_queue_shows_incoming_outgoing_and_resolved(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            transfers.request(db, make_transfer())
            urban = workqueue.transfer_queue(db, region="urban",
                                             as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(urban["awaiting_decision"], 1)
            self.assertEqual(urban["incoming"][0]["case_id"], "case-one")
            self.assertEqual(urban["incoming"][0]["case_finding"], "pending")
            suburban = workqueue.transfer_queue(db, region="suburban",
                                                as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(suburban["awaiting_acceptance"], 1)
            self.assertEqual(suburban["outgoing"][0]["transfer_id"], "transfer-one")
            transfers.resolve(db, "transfer-one", make_resolution())
            resolved = workqueue.transfer_queue(db, region="urban",
                                                as_of=instant("2026-10-16T00:00:00Z"))
            self.assertEqual(resolved["awaiting_decision"], 0)
            self.assertEqual(resolved["resolved"][0]["status"], "accepted")


class TransferApiTests(DatabaseCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.store.location))

    def test_transfer_endpoints_record_the_handover_lifecycle(self):
        self.client.post("/providers?source_ref=feed&revision=1", json=provider_data())
        self.client.post("/touches", json=touch_data())
        self.client.post("/cases?source_ref=case-feed&revision=1", json=case_data())
        created = self.client.post("/cases/case-one/transfers", json=transfer_data())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["status"], "requested")
        view = self.client.get("/cases/case-one/transfers")
        self.assertEqual(view.json()["responsibility"]["responsible_region"], "suburban")
        resolved = self.client.post("/transfers/transfer-one/resolution",
                                    json=resolution_data())
        self.assertEqual(resolved.status_code, 200)
        view = self.client.get("/cases/case-one/transfers")
        self.assertEqual(view.json()["responsibility"]["responsible_region"], "urban")
        work = self.client.get("/work/transfers?region=urban")
        self.assertEqual(work.json()["resolved"][0]["status"], "accepted")

    def test_transfer_request_rejects_mismatched_case_path(self):
        response = self.client.post("/cases/other-case/transfers", json=transfer_data())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "case_scope")


if __name__ == "__main__":
    unittest.main()
