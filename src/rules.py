"""再保险合约与巨灾暴露管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "quoted"
CREATE_ROLES = {'underwriter'}
ACTION_ROLES = {'bind': {'underwriter'}, 'submit_claim': {'claims_officer'}, 'calculate': {'claims_officer'}, 'settle': {'finance'}, 'reject': {'finance', 'claims_officer'}, 'endorse': {'underwriter'}}
TRANSITIONS = {'bind': {'quoted': 'bound'}, 'submit_claim': {'bound': 'claim_submitted'}, 'calculate': {'claim_submitted': 'calculated'}, 'settle': {'calculated': 'settled'}, 'reject': {'claim_submitted': 'rejected', 'calculated': 'rejected'}, 'endorse': {'bound': 'bound', 'claim_submitted': 'claim_submitted', 'calculated': 'calculated', 'settled': 'settled'}}

SHARE_TOTAL_TOLERANCE = 1e-6


def validate_shares(raw: Any) -> List[Dict[str, Any]]:
    """校验份额台账：非空、再保险人不重复、比例合计必须为100%。"""
    if not isinstance(raw, list) or not raw:
        raise ValidationError("shares必须是非空列表")
    seen = set()
    shares: List[Dict[str, Any]] = []
    total = 0.0
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("shares每项必须是对象")
        reinsurer = text(item, "reinsurer")
        if reinsurer in seen:
            raise ValidationError("再保险人%s重复" % reinsurer)
        seen.add(reinsurer)
        share_pct = number(item, "share_pct", 0, 1)
        if share_pct <= 0:
            raise ValidationError("share_pct必须大于0")
        limit = number(item, "limit", 0)
        shares.append({"reinsurer": reinsurer, "share_pct": share_pct, "limit": limit})
        total += share_pct
    if abs(total - 1.0) > SHARE_TOTAL_TOLERANCE:
        raise ValidationError("份额合计必须为100%%，当前为%.4f" % total)
    return shares


def build_breakdown(recovery: float, shares: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按份额把总摊回分摊到各家再保险人，超出该家额度的部分标为未覆盖。"""
    breakdown: List[Dict[str, Any]] = []
    for share in shares:
        allocated = round(recovery * float(share["share_pct"]), 2)
        limit = float(share["limit"])
        covered = round(min(allocated, limit), 2)
        uncovered = round(allocated - covered, 2)
        if uncovered <= 0:
            status = "covered"
        elif covered > 0:
            status = "partial"
        else:
            status = "uncovered"
        breakdown.append({
            "reinsurer": share["reinsurer"],
            "share_pct": float(share["share_pct"]),
            "limit": limit,
            "allocated": allocated,
            "covered": covered,
            "uncovered": uncovered,
            "status": status,
        })
    return breakdown


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "event_id")
        attachment = number(p, "attachment", 0)
        limit = number(p, "limit", 0)
        number(p, "cession_pct", 0, 1)
        number(p, "loss_amount", 0)
        number(p, "reinstatement_pct", 0, 1)
        number(p, "aggregate_prior", 0)
        if limit <= attachment:
            raise ValidationError("赔款限额必须高于起赔点")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        width = float(p["limit"]) - float(p["attachment"])
        retained_loss = max(0.0, float(p["loss_amount"]) - float(p["attachment"]))
        recovery = min(retained_loss, width) * float(p["cession_pct"])
        p["layer_width"] = round(width, 2)
        p["recoverable_amount"] = round(recovery, 2)
        p["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
        p["net_retention"] = round(float(p["loss_amount"]) - recovery, 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        event_id = payload.get("event_id")
        used = float(payload.get("aggregate_prior", 0))
        for item in existing:
            if item["state"] in {"rejected"} or item["payload"].get("event_id") != event_id:
                continue
            used += float(item["payload"].get("recoverable_amount", 0))
        capacity = float(payload["layer_width"]) * float(payload["cession_pct"])
        projected = min(max(0.0, float(payload["loss_amount"]) - float(payload["attachment"])), float(payload["layer_width"])) * float(payload["cession_pct"])
        if used + projected > capacity + 0.01:
            raise Conflict("同一事件累计摊回超过再保容量")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], actor_id: str = "") -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            changes["share_ledger"] = validate_shares(data.get("shares"))
            changes["endorsement_version"] = 1
            changes["endorsements"] = []
            summary = "再保合约已绑定，份额台账V1已确认"
        elif action == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            changes["claim_event_id"] = text(data, "event_id")
            summary = "赔案已提交"
        elif action == "calculate":
            loss = number(data, "approved_loss", 0)
            width = float(p["layer_width"])
            recovery = min(max(0.0, loss - float(p["attachment"])), width) * float(p["cession_pct"])
            changes["approved_loss"] = loss
            changes["recoverable_amount"] = round(recovery, 2)
            changes["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
            breakdown = build_breakdown(recovery, p.get("share_ledger") or [])
            changes["recovery_breakdown"] = breakdown
            changes["uncovered_amount"] = round(sum(item["uncovered"] for item in breakdown), 2)
            changes["calculated_endorsement_version"] = int(p.get("endorsement_version", 1))
            summary = "摊回金额已计算"
        elif action == "settle":
            if float(p["recoverable_amount"]) <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            changes["settlement_snapshot"] = {
                "endorsement_version": int(p.get("endorsement_version", 1)),
                "shares": p.get("share_ledger") or [],
                "bills": p.get("recovery_breakdown") or [],
                "uncovered_amount": float(p.get("uncovered_amount", 0.0)),
            }
            summary = "摊回赔款已结算"
        elif action == "endorse":
            reason = text(data, "reason")
            shares = validate_shares(data.get("shares"))
            version = int(p.get("endorsement_version", 1)) + 1
            endorsements = list(p.get("endorsements") or [])
            endorsements.append({"version": version, "reason": reason, "shares": shares, "issued_by": actor_id})
            changes["share_ledger"] = shares
            changes["endorsements"] = endorsements
            changes["endorsement_version"] = version
            if record["state"] == "calculated":
                breakdown = build_breakdown(float(p["recoverable_amount"]), shares)
                changes["recovery_breakdown"] = breakdown
                changes["uncovered_amount"] = round(sum(item["uncovered"] for item in breakdown), 2)
                changes["calculated_endorsement_version"] = version
                summary = "批单V%s已签发，未结算案件已按最新份额重算" % version
            elif record["state"] == "settled":
                summary = "批单V%s已签发，已结算账单不受影响" % version
            else:
                summary = "批单V%s已签发" % version
        elif action == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒绝"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
