"""再保险合约与巨灾暴露管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "quoted"
CREATE_ROLES = {'underwriter'}
ACTION_ROLES = {'bind': {'underwriter'}, 'endorse': {'underwriter'}, 'submit_claim': {'claims_officer'}, 'calculate': {'claims_officer'}, 'settle': {'finance'}, 'reject': {'finance', 'claims_officer'}}
TRANSITIONS = {'bind': {'quoted': 'bound'}, 'endorse': {'bound': 'bound', 'claim_submitted': 'claim_submitted', 'calculated': 'calculated'}, 'submit_claim': {'bound': 'claim_submitted'}, 'calculate': {'claim_submitted': 'calculated'}, 'settle': {'calculated': 'settled'}, 'reject': {'claim_submitted': 'rejected', 'calculated': 'rejected'}}


def validate_shares(value: Any) -> List[Dict[str, Any]]:
    """校验份额台账：再保险人不得重复，比例合计必须为100%。"""
    if not isinstance(value, list) or not value:
        raise ValidationError("shares必须是非空列表")
    seen = set()
    total = 0.0
    shares: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("shares项必须是对象")
        reinsurer = text(item, "reinsurer")
        if reinsurer in seen:
            raise ValidationError("再保险人%s重复" % reinsurer)
        seen.add(reinsurer)
        share_pct = number(item, "share_pct", 0, 1)
        if share_pct <= 0:
            raise ValidationError("share_pct必须大于0")
        capacity = number(item, "capacity", 0)
        shares.append({"reinsurer": reinsurer, "share_pct": share_pct, "capacity": capacity})
        total += share_pct
    if abs(total - 1.0) > 1e-6:
        raise ValidationError("份额合计必须为100%")
    return shares


def allocate_recovery(recoverable: float, shares: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], float]:
    """按份额拆分摊回金额，额度不足部分记为未覆盖。"""
    allocations: List[Dict[str, Any]] = []
    remaining = round(recoverable, 2)
    last = len(shares) - 1
    uncovered_total = 0.0
    for index, share in enumerate(shares):
        if index == last:
            amount = remaining
        else:
            amount = round(recoverable * float(share["share_pct"]), 2)
            remaining = round(remaining - amount, 2)
        capacity = float(share["capacity"])
        billed = round(min(amount, capacity), 2)
        uncovered = round(amount - billed, 2)
        uncovered_total = round(uncovered_total + uncovered, 2)
        allocations.append({
            "reinsurer": share["reinsurer"],
            "share_pct": share["share_pct"],
            "capacity": capacity,
            "allocated": amount,
            "billed": billed,
            "uncovered": uncovered,
        })
    return allocations, uncovered_total


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

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            shares = validate_shares(data.get("shares"))
            changes["shares"] = shares
            changes["share_version"] = 1
            changes["share_history"] = [{"version": 1, "reason": "绑单确认", "shares": shares}]
            summary = "再保合约已绑定"
        elif action == "endorse":
            reason = text(data, "reason")
            shares = validate_shares(data.get("shares"))
            version = int(p.get("share_version", 1)) + 1
            history = list(p.get("share_history") or [])
            history.append({"version": version, "reason": reason, "shares": shares})
            changes["shares"] = shares
            changes["share_version"] = version
            changes["share_history"] = history
            if record["state"] == "calculated":
                allocations, uncovered = allocate_recovery(float(p["recoverable_amount"]), shares)
                changes["allocations"] = allocations
                changes["uncovered_amount"] = uncovered
                changes["allocations_version"] = version
            summary = "批单V%s已生效，份额台账已更新" % version
        elif action == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            changes["claim_event_id"] = text(data, "event_id")
            summary = "赔案已提交"
        elif action == "calculate":
            loss = number(data, "approved_loss", 0)
            width = float(p["layer_width"])
            recovery = min(max(0.0, loss - float(p["attachment"])), width) * float(p["cession_pct"])
            shares = p.get("shares")
            if not shares:
                raise ValidationError("缺少份额台账，请先绑单确认再保份额")
            allocations, uncovered = allocate_recovery(recovery, shares)
            changes["approved_loss"] = loss
            changes["recoverable_amount"] = round(recovery, 2)
            changes["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
            changes["allocations"] = allocations
            changes["uncovered_amount"] = uncovered
            changes["allocations_version"] = int(p.get("share_version", 1))
            summary = "摊回金额已计算"
        elif action == "settle":
            if float(p["recoverable_amount"]) <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            allocations = list(p.get("allocations") or [])
            bills = [{"reinsurer": item["reinsurer"], "amount": item["billed"], "share_pct": item["share_pct"]} for item in allocations]
            changes["bills"] = bills
            changes["settled_snapshot"] = {
                "share_version": p.get("share_version"),
                "shares": p.get("shares") or [],
                "allocations": allocations,
                "bills": bills,
            }
            summary = "摊回赔款已结算"
        elif action == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒绝"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
