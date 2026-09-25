import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'event_id': 'CAT-2026-01', 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4, 'loss_amount': 3000000.0, 'reinstatement_pct': 0.15, 'aggregate_prior': 0.0}
SHARES = [{'reinsurer': 'SwissRe', 'share_pct': 0.6, 'limit': 400000.0}, {'reinsurer': 'MunichRe', 'share_pct': 0.4, 'limit': 200000.0}]
NEW_SHARES = [{'reinsurer': 'SwissRe', 'share_pct': 0.5, 'limit': 1000000.0}, {'reinsurer': 'MunichRe', 'share_pct': 0.5, 'limit': 1000000.0}]
UW = Actor('uw-1', 'underwriter')
CLAIMS = Actor('cl-1', 'claims_officer')
FINANCE = Actor('fin-1', 'finance')


class ShareLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def _create(self):
        self.counter += 1
        return self.service.create(UW, "RI-2500%s" % self.counter, CREATE_DATA)

    def _bind(self, record, shares):
        return self.service.act(UW, record["id"], record["version"], "bind", {'underwriter_id': 'UW-8', 'shares': shares})

    def _to_calculated(self, shares=None):
        record = self._bind(self._create(), shares or SHARES)
        record = self.service.act(CLAIMS, record["id"], record["version"], "submit_claim", {'claim_number': 'CLM-88', 'event_id': 'CAT-2026-01'})
        return self.service.act(CLAIMS, record["id"], record["version"], "calculate", {'approved_loss': 2800000.0})

    def test_bind_rejects_total_not_100_percent(self):
        record = self._create()
        bad = [{'reinsurer': 'SwissRe', 'share_pct': 0.6, 'limit': 400000.0}]
        with self.assertRaises(ValidationError):
            self._bind(record, bad)
        self.assertEqual(self.service.get_record(UW, record["id"])["state"], "quoted")

    def test_bind_rejects_duplicate_reinsurer(self):
        record = self._create()
        dup = [{'reinsurer': 'SwissRe', 'share_pct': 0.5, 'limit': 1.0}, {'reinsurer': 'SwissRe', 'share_pct': 0.5, 'limit': 1.0}]
        with self.assertRaises(ValidationError):
            self._bind(record, dup)
        self.assertEqual(self.service.get_record(UW, record["id"])["state"], "quoted")

    def test_bind_confirms_share_ledger_v1(self):
        record = self._bind(self._create(), SHARES)
        self.assertEqual(record["state"], "bound")
        self.assertEqual(record["payload"]["share_ledger"], SHARES)
        self.assertEqual(record["payload"]["endorsement_version"], 1)

    def test_calculate_generates_per_reinsurer_breakdown(self):
        record = self._to_calculated()
        payload = record["payload"]
        self.assertEqual(payload["recoverable_amount"], 720000.0)
        breakdown = {item["reinsurer"]: item for item in payload["recovery_breakdown"]}
        swiss = breakdown["SwissRe"]
        self.assertEqual(swiss["allocated"], 432000.0)
        self.assertEqual(swiss["covered"], 400000.0)
        self.assertEqual(swiss["uncovered"], 32000.0)
        self.assertEqual(swiss["status"], "partial")
        munich = breakdown["MunichRe"]
        self.assertEqual(munich["allocated"], 288000.0)
        self.assertEqual(munich["covered"], 200000.0)
        self.assertEqual(munich["uncovered"], 88000.0)
        self.assertEqual(payload["uncovered_amount"], 120000.0)
        self.assertEqual(payload["calculated_endorsement_version"], 1)

    def test_endorsement_recalculates_unsettled_case(self):
        record = self._to_calculated()
        record = self.service.act(UW, record["id"], record["version"], "endorse", {'reason': '份额调整：MunichRe扩容', 'shares': NEW_SHARES})
        payload = record["payload"]
        self.assertEqual(record["state"], "calculated")
        self.assertEqual(payload["endorsement_version"], 2)
        self.assertEqual(payload["calculated_endorsement_version"], 2)
        self.assertEqual(payload["share_ledger"], NEW_SHARES)
        breakdown = {item["reinsurer"]: item for item in payload["recovery_breakdown"]}
        self.assertEqual(breakdown["SwissRe"]["allocated"], 360000.0)
        self.assertEqual(breakdown["SwissRe"]["uncovered"], 0.0)
        self.assertEqual(payload["uncovered_amount"], 0.0)
        self.assertEqual(len(payload["endorsements"]), 1)
        self.assertEqual(payload["endorsements"][0]["reason"], '份额调整：MunichRe扩容')
        self.assertEqual(payload["endorsements"][0]["version"], 2)

    def test_endorsement_requires_reason_and_valid_shares(self):
        record = self._to_calculated()
        with self.assertRaises(ValidationError):
            self.service.act(UW, record["id"], record["version"], "endorse", {'shares': NEW_SHARES})
        with self.assertRaises(ValidationError):
            self.service.act(UW, record["id"], record["version"], "endorse", {'reason': '比例不足', 'shares': NEW_SHARES[:1]})

    def test_endorsement_role_and_state_guards(self):
        record = self._to_calculated()
        with self.assertRaises(PermissionDenied):
            self.service.act(FINANCE, record["id"], record["version"], "endorse", {'reason': '越权', 'shares': NEW_SHARES})
        fresh = self._create()
        with self.assertRaises(Conflict):
            self.service.act(UW, fresh["id"], fresh["version"], "endorse", {'reason': '未绑单', 'shares': NEW_SHARES})

    def test_settlement_snapshot_immune_to_endorsement(self):
        record = self._to_calculated()
        record = self.service.act(FINANCE, record["id"], record["version"], "settle", {'payment_reference': 'PAY-1'})
        snapshot = record["payload"]["settlement_snapshot"]
        self.assertEqual(snapshot["endorsement_version"], 1)
        self.assertEqual(snapshot["shares"], SHARES)
        bills = {item["reinsurer"]: item for item in snapshot["bills"]}
        self.assertEqual(bills["SwissRe"]["covered"], 400000.0)
        self.assertEqual(snapshot["uncovered_amount"], 120000.0)
        record = self.service.act(UW, record["id"], record["version"], "endorse", {'reason': '结算后调整', 'shares': NEW_SHARES})
        payload = record["payload"]
        self.assertEqual(payload["endorsement_version"], 2)
        self.assertEqual(payload["share_ledger"], NEW_SHARES)
        self.assertEqual(payload["settlement_snapshot"], snapshot)
