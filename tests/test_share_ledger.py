import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


CREATE_DATA = {'event_id': 'CAT-2026-01', 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4, 'loss_amount': 3000000.0, 'reinstatement_pct': 0.15, 'aggregate_prior': 0.0}
SHARES = [{'reinsurer': 'SwissRe', 'share_pct': 0.6, 'capacity': 400000.0}, {'reinsurer': 'MunichRe', 'share_pct': 0.4, 'capacity': 1000000.0}]


class ShareLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _calculated(self):
        record = self.service.create(Actor("creator", "underwriter"), "RI-1", CREATE_DATA)
        record = self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "bind", {"underwriter_id": "UW-8", "shares": SHARES})
        record = self.service.act(Actor("clm", "claims_officer"), record["id"], record["version"], "submit_claim", {"claim_number": "CLM-1", "event_id": "CAT-2026-01"})
        return self.service.act(Actor("clm", "claims_officer"), record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})

    def test_bind_rejects_bad_total_and_duplicates(self):
        record = self.service.create(Actor("creator", "underwriter"), "RI-1", CREATE_DATA)
        bad_total = [{'reinsurer': 'A', 'share_pct': 0.5, 'capacity': 1.0}, {'reinsurer': 'B', 'share_pct': 0.4, 'capacity': 1.0}]
        with self.assertRaises(ValidationError):
            self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "bind", {"underwriter_id": "UW-8", "shares": bad_total})
        duplicated = [{'reinsurer': 'A', 'share_pct': 0.5, 'capacity': 1.0}, {'reinsurer': 'A', 'share_pct': 0.5, 'capacity': 1.0}]
        with self.assertRaises(ValidationError):
            self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "bind", {"underwriter_id": "UW-8", "shares": duplicated})
        bound = self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "bind", {"underwriter_id": "UW-8", "shares": SHARES})
        self.assertEqual(bound["state"], "bound")
        self.assertEqual(bound["payload"]["share_version"], 1)
        self.assertEqual(len(bound["payload"]["share_history"]), 1)

    def test_calculate_allocates_and_marks_uncovered(self):
        record = self._calculated()
        # 摊回 = (280万 - 100万) * 0.4 = 72万
        self.assertEqual(record["payload"]["recoverable_amount"], 720000.0)
        allocations = record["payload"]["allocations"]
        self.assertEqual(len(allocations), 2)
        swiss, munich = allocations
        self.assertEqual(swiss["allocated"], 432000.0)
        self.assertEqual(swiss["billed"], 400000.0)
        self.assertEqual(swiss["uncovered"], 32000.0)
        self.assertEqual(munich["allocated"], 288000.0)
        self.assertEqual(munich["billed"], 288000.0)
        self.assertEqual(munich["uncovered"], 0.0)
        self.assertEqual(record["payload"]["uncovered_amount"], 32000.0)
        self.assertEqual(record["payload"]["allocations_version"], 1)

    def test_endorse_recalculates_unsettled(self):
        record = self._calculated()
        new_shares = [{'reinsurer': 'SwissRe', 'share_pct': 0.5, 'capacity': 1000000.0}, {'reinsurer': 'MunichRe', 'share_pct': 0.5, 'capacity': 1000000.0}]
        with self.assertRaises(ValidationError):
            self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "endorse", {"shares": new_shares})
        updated = self.service.act(Actor("uw", "underwriter"), record["id"], record["version"], "endorse", {"reason": "调价后重签", "shares": new_shares})
        self.assertEqual(updated["state"], "calculated")
        self.assertEqual(updated["payload"]["share_version"], 2)
        allocations = updated["payload"]["allocations"]
        self.assertEqual(allocations[0]["allocated"], 360000.0)
        self.assertEqual(allocations[0]["uncovered"], 0.0)
        self.assertEqual(updated["payload"]["uncovered_amount"], 0.0)
        self.assertEqual(updated["payload"]["allocations_version"], 2)
        history = updated["payload"]["share_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["version"], 2)
        self.assertEqual(history[-1]["reason"], "调价后重签")

    def test_settled_snapshot_immune_to_endorse(self):
        record = self._calculated()
        settled = self.service.act(Actor("fin", "finance"), record["id"], record["version"], "settle", {"payment_reference": "PAY-1"})
        snapshot = settled["payload"]["settled_snapshot"]
        self.assertEqual(snapshot["share_version"], 1)
        self.assertEqual(snapshot["bills"][0], {"reinsurer": "SwissRe", "amount": 400000.0, "share_pct": 0.6})
        self.assertEqual(snapshot["bills"][1], {"reinsurer": "MunichRe", "amount": 288000.0, "share_pct": 0.4})
        self.assertEqual(settled["payload"]["bills"], snapshot["bills"])
        with self.assertRaises(Conflict):
            self.service.act(Actor("uw", "underwriter"), settled["id"], settled["version"], "endorse", {"reason": "试图调整", "shares": SHARES})
        again = self.service.get_record(Actor("fin", "finance"), settled["id"])
        self.assertEqual(again["payload"]["settled_snapshot"], snapshot)
        self.assertEqual(again["payload"]["share_version"], 1)
