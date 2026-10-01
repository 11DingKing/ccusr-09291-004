"""
采购订单分级审批工作流。

控制要点：
1. 根据订单金额确定基础审批级别，关键物料 / 低等级供应商 / 例外供应商逐级上调；
2. 申请提交时冻结订单摘要（JSON + SHA-256），签署人所见即所签，签署前校验完整性；
3. 全部有效批准完成、且预算仍足额之前，不得创建正式采购订单；
4. 审批人回避保留原节点(recused)，替换人另起节点，路线可核对；
5. 审批期间预算变化逐次留痕，预算不足则阻断签署/转单；
6. 拒绝后重提另开申请并关联父申请，按最新规则版本重走全路线；
7. 重复签署、越级签署均被拦截并写入只增事件流水；
8. 规则换版永久保留历史版本，在途申请锁定发起时版本。
"""
import json
import hashlib
from datetime import date, datetime
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy.orm import Session

from app.models import (
    ApprovalRuleVersion, PurchaseApprovalRequest, ApprovalNode, ApprovalEvent,
    PurchaseOrder, PurchaseSuggestion, Material, Supplier, SupplyCapacity
)
from app.crud.approval import (
    crud_approval_rule_version, crud_budget_period, crud_approval_request,
)
from app.crud.supplier import crud_supply_capacity
from app.crud.purchase import crud_purchase_order, crud_purchase_suggestion
from app.schemas import ApprovalSubmitRequest, ApprovalResubmitRequest


# 默认分级阈值（v1）：金额下限含本数，级别从低到高
DEFAULT_THRESHOLDS = [
    {"level": 1, "level_name": "采购主管", "min_amount": 0, "approver": "采购主管"},
    {"level": 2, "level_name": "采购经理", "min_amount": 50000, "approver": "采购经理"},
    {"level": 3, "level_name": "财务总监", "min_amount": 200000, "approver": "财务总监"},
    {"level": 4, "level_name": "总经理", "min_amount": 500000, "approver": "总经理"},
]

HIGH_AMOUNT = 200000.0
MEDIUM_AMOUNT = 50000.0


class ApprovalError(ValueError):
    """审批业务规则拒绝。"""


class ApprovalService:
    # ---------------- 规则版本 ----------------

    @staticmethod
    def ensure_default_version(db: Session) -> ApprovalRuleVersion:
        """启动时幂等写入 v1 规则。"""
        active = crud_approval_rule_version.get_active(db)
        if active:
            return active
        version = ApprovalRuleVersion(
            version=1,
            thresholds_json=json.dumps(DEFAULT_THRESHOLDS, ensure_ascii=False),
            critical_uplift=1,
            low_grade_uplift=1,
            exception_uplift=1,
            change_note="初始分级审批规则",
            is_active=True,
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        return version

    @staticmethod
    def publish_new_version(
        db: Session,
        thresholds: Optional[List[Dict[str, Any]]] = None,
        critical_uplift: int = 1,
        low_grade_uplift: int = 1,
        exception_uplift: int = 1,
        change_note: Optional[str] = None,
    ) -> ApprovalRuleVersion:
        """规则换版：旧版本永久保留，在途申请继续锁定旧版本，重提才适用新版本。"""
        current = ApprovalService.ensure_default_version(db)
        new_version_no = crud_approval_rule_version.get_max_version(db) + 1
        thresholds_json = json.dumps(thresholds or DEFAULT_THRESHOLDS, ensure_ascii=False)

        current.is_active = False
        new_version = ApprovalRuleVersion(
            version=new_version_no,
            thresholds_json=thresholds_json,
            critical_uplift=critical_uplift,
            low_grade_uplift=low_grade_uplift,
            exception_uplift=exception_uplift,
            change_note=change_note,
            is_active=True,
        )
        db.add(current)
        db.add(new_version)
        db.flush()

        # 在途申请不受影响，但留下"规则已换版、本单锁定旧版"的可核对记录
        pending = crud_approval_request.get_multi_by_status(db, "pending")
        for req in pending:
            db.add(ApprovalEvent(
                request_id=req.id,
                event_type="rule_version_published",
                actor="系统",
                detail=json.dumps({
                    "message": f"审批规则已换版为v{new_version_no}，本申请锁定v{req.rule_version.version}，路线不变",
                    "locked_version": req.rule_version.version,
                    "new_version": new_version_no,
                }, ensure_ascii=False),
            ))
        db.commit()
        db.refresh(new_version)
        return new_version

    # ---------------- 风险评估 / 分级路线 ----------------

    @staticmethod
    def supplier_grade(rating: Optional[float]) -> str:
        r = rating or 0.0
        if r >= 4.7:
            return "A"
        if r >= 4.5:
            return "B"
        return "C"

    @staticmethod
    def evaluate(
        db: Session,
        material: Material,
        supplier: Supplier,
        quantity: int,
        unit_price: float,
        is_exception_flag: bool,
        version: Optional[ApprovalRuleVersion] = None,
    ) -> Dict[str, Any]:
        version = version or ApprovalService.ensure_default_version(db)
        thresholds = json.loads(version.thresholds_json)
        max_level = max(t["level"] for t in thresholds)

        total_amount = round(quantity * unit_price, 2)

        capacity = crud_supply_capacity.get_by_supplier_and_material(db, supplier.id, material.id)
        detected_exception = False
        exception_basis = None
        if capacity is None:
            detected_exception = True
            exception_basis = "供应商无该物料的供货能力记录"
        elif not capacity.is_preferred:
            detected_exception = True
            exception_basis = "该供应商非此物料的优选供应商"
        is_exception = bool(is_exception_flag or detected_exception)

        grade = ApprovalService.supplier_grade(supplier.rating)

        # 金额决定基础级别
        required_level = 1
        for t in thresholds:
            if total_amount >= t["min_amount"]:
                required_level = t["level"]

        factors = [f"订单金额{total_amount:.2f}元，金额定级L{required_level}"]
        if material.is_critical:
            required_level += version.critical_uplift
            factors.append(f"关键物料，级别上调{version.critical_uplift}级")
        if grade == "C":
            required_level += version.low_grade_uplift
            factors.append(f"供应商等级C（评级{supplier.rating}），级别上调{version.low_grade_uplift}级")
        elif grade == "B":
            factors.append(f"供应商等级B（评级{supplier.rating}）")
        else:
            factors.append(f"供应商等级A（评级{supplier.rating}）")
        if is_exception:
            required_level += version.exception_uplift
            factors.append(f"例外供应商，级别上调{version.exception_uplift}级（{exception_basis or '采购员申报'}）")

        required_level = max(1, min(max_level, required_level))
        route = [t for t in thresholds if t["level"] <= required_level]

        if material.is_critical or grade == "C" or total_amount >= HIGH_AMOUNT:
            risk_level = "high"
        elif is_exception or grade == "B" or total_amount >= MEDIUM_AMOUNT:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "total_amount": total_amount,
            "grade": grade,
            "capacity": capacity,
            "is_exception": is_exception,
            "exception_basis": exception_basis,
            "risk_level": risk_level,
            "risk_factors": factors,
            "required_level": required_level,
            "route": route,
            "rule_version": version,
        }

    # ---------------- 预算 ----------------

    @staticmethod
    def budget_state(db: Session, period: str,
                     exclude_request_id: Optional[int] = None) -> Dict[str, Any]:
        period_obj = crud_budget_period.get_by_period(db, period)
        if period_obj is None:
            return {
                "period": period,
                "configured": False,
                "total_budget": None,
                "committed_amount": 0.0,
                "pending_approval_amount": 0.0,
                "available_amount": None,
            }
        committed = crud_approval_request.get_converted_amount(db, period)
        pending = crud_approval_request.get_pending_encumbrance(
            db, period, exclude_request_id=exclude_request_id
        )
        available = round(period_obj.total_budget - committed - pending, 2)
        return {
            "period": period,
            "configured": True,
            "total_budget": round(period_obj.total_budget, 2),
            "committed_amount": round(committed, 2),
            "pending_approval_amount": round(pending, 2),
            "available_amount": available,
        }

    @staticmethod
    def _budget_sufficient(state: Dict[str, Any], amount: float) -> bool:
        if not state["configured"]:
            return True
        return state["available_amount"] + 0.005 >= amount
        # 注：available 已排除本单（exclude_request_id），故比较的是本单所需额度

    # ---------------- 冻结摘要 ----------------

    @staticmethod
    def _build_snapshot(payload: Dict[str, Any]) -> Tuple[str, str]:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text, digest

    @staticmethod
    def _verify_snapshot(request: PurchaseApprovalRequest) -> None:
        digest = hashlib.sha256(request.snapshot_json.encode("utf-8")).hexdigest()
        if digest != request.snapshot_hash:
            raise ApprovalError("冻结摘要校验失败：订单摘要与发起时不一致，禁止继续审批")

    # ---------------- 事件流水 ----------------

    @staticmethod
    def _log(db: Session, request_id: int, event_type: str,
             actor: Optional[str], detail: Any = None) -> ApprovalEvent:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        event = ApprovalEvent(
            request_id=request_id, event_type=event_type,
            actor=actor, detail=detail
        )
        db.add(event)
        return event

    # ---------------- 提交审批 ----------------

    @staticmethod
    def preview_route(db: Session, *, supplier_id: int, material_id: int,
                      quantity: int, unit_price: float,
                      is_exception_supplier: bool = False,
                      budget_period: Optional[str] = None) -> Dict[str, Any]:
        supplier = db.get(Supplier, supplier_id)
        material = db.get(Material, material_id)
        if not supplier:
            raise ApprovalError("供应商不存在")
        if not material:
            raise ApprovalError("物料不存在")
        if quantity <= 0:
            raise ApprovalError("数量必须大于0")
        if unit_price < 0:
            raise ApprovalError("单价不能为负")
        version = ApprovalService.ensure_default_version(db)
        ev = ApprovalService.evaluate(
            db, material, supplier, quantity, unit_price,
            is_exception_supplier, version
        )
        period = budget_period or date.today().strftime("%Y-%m")
        budget = ApprovalService.budget_state(db, period)
        return {
            "total_amount": ev["total_amount"],
            "risk_level": ev["risk_level"],
            "risk_factors": ev["risk_factors"],
            "required_level": ev["required_level"],
            "rule_version": version.version,
            "route": ev["route"],
            "is_exception_supplier": ev["is_exception"],
            "exception_basis": ev["exception_basis"],
            "budget": budget,
        }

    @staticmethod
    def submit(db: Session, payload: ApprovalSubmitRequest) -> PurchaseApprovalRequest:
        version = ApprovalService.ensure_default_version(db)

        suggestion: Optional[PurchaseSuggestion] = None
        if payload.suggestion_id is not None:
            suggestion = crud_purchase_suggestion.get(db, payload.suggestion_id)
            if not suggestion:
                raise ApprovalError(f"采购建议不存在: {payload.suggestion_id}")
            if suggestion.status != "pending":
                raise ApprovalError(f"采购建议状态为 {suggestion.status}，不可提交审批")

        material_id = payload.material_id or (suggestion.material_id if suggestion else None)
        if material_id is None:
            raise ApprovalError("必须指定物料")
        material = db.get(Material, material_id)
        supplier = db.get(Supplier, payload.supplier_id)
        if not material:
            raise ApprovalError("物料不存在")
        if not supplier:
            raise ApprovalError("供应商不存在")

        quantity = payload.quantity if payload.quantity is not None else (
            suggestion.suggested_quantity if suggestion else None)
        if not quantity or quantity <= 0:
            raise ApprovalError("数量必须大于0")

        capacity = crud_supply_capacity.get_by_supplier_and_material(
            db, supplier.id, material.id)
        unit_price = payload.unit_price
        if unit_price is None:
            unit_price = capacity.unit_price if capacity else 0.0
        if unit_price < 0:
            raise ApprovalError("单价不能为负")

        expected_date = payload.expected_date or (
            suggestion.expected_delivery_date if suggestion else None)
        if not expected_date:
            raise ApprovalError("必须指定期望交货日期")

        period = payload.budget_period or expected_date.strftime("%Y-%m")

        # 同一订单号不得有在途申请，也不得已存在正式订单
        if crud_approval_request.get_open_by_order_no(db, payload.order_no):
            raise ApprovalError(f"订单号 {payload.order_no} 已有进行中的审批申请")
        if crud_purchase_order.get_by_order_no(db, payload.order_no):
            raise ApprovalError(f"订单号 {payload.order_no} 已存在正式采购订单")

        ev = ApprovalService.evaluate(
            db, material, supplier, quantity, unit_price,
            bool(payload.is_exception_supplier), version
        )
        if ev["is_exception"] and not (payload.exception_reason or "").strip():
            raise ApprovalError(
                "例外供应商必须填写获批理由（无供货能力记录或非优选供应商）")

        budget = ApprovalService.budget_state(db, period)
        if not ApprovalService._budget_sufficient(budget, ev["total_amount"]):
            raise ApprovalError(
                f"预算期间{period}可用额度{budget['available_amount']}元，"
                f"不足以承担本单{ev['total_amount']:.2f}元"
            )

        request = PurchaseApprovalRequest(
            suggestion_id=suggestion.id if suggestion else None,
            order_no=payload.order_no,
            supplier_id=supplier.id,
            material_id=material.id,
            quantity=quantity,
            unit_price=unit_price,
            total_amount=ev["total_amount"],
            expected_date=expected_date,
            is_exception_supplier=ev["is_exception"],
            exception_reason=payload.exception_reason if ev["is_exception"] else None,
            budget_period=period,
            risk_level=ev["risk_level"],
            required_level=ev["required_level"],
            rule_version_id=version.id,
            status="pending",
            requester=payload.requester or "采购员",
            remark=payload.remark,
        )
        db.add(request)
        db.flush()  # 取 id 生成申请编号
        request.request_no = f"APR-{date.today():%Y%m%d}-{request.id:05d}"

        snapshot_payload = {
            "request_no": request.request_no,
            "order_no": request.order_no,
            "supplier": {"id": supplier.id, "code": supplier.code, "name": supplier.name,
                         "rating": supplier.rating, "grade": ev["grade"]},
            "material": {"id": material.id, "code": material.code, "name": material.name,
                         "category": material.category, "is_critical": material.is_critical},
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": ev["total_amount"],
            "expected_date": expected_date,
            "is_exception_supplier": ev["is_exception"],
            "exception_basis": ev["exception_basis"],
            "exception_reason": request.exception_reason,
            "risk_level": ev["risk_level"],
            "risk_factors": ev["risk_factors"],
            "required_level": ev["required_level"],
            "rule_version": version.version,
            "thresholds": json.loads(version.thresholds_json),
            "route": ev["route"],
            "budget_at_submit": budget,
            "frozen_at": datetime.now().isoformat(timespec="seconds"),
        }
        snapshot_text, snapshot_hash = ApprovalService._build_snapshot(snapshot_payload)
        request.snapshot_json = snapshot_text
        request.snapshot_hash = snapshot_hash

        # 生成各级别初始节点
        for t in ev["route"]:
            db.add(ApprovalNode(
                request_id=request.id, level=t["level"], level_name=t["level_name"],
                approver=t["approver"], seq=1, status="pending",
            ))

        ApprovalService._log(db, request.id, "submitted", request.requester, {
            "message": "审批申请已提交，订单摘要已冻结",
            "risk_level": ev["risk_level"], "risk_factors": ev["risk_factors"],
            "required_level": ev["required_level"], "rule_version": version.version,
            "budget": budget, "snapshot_hash": snapshot_hash,
        })

        if suggestion is not None:
            suggestion.status = "in_approval"
            db.add(suggestion)

        db.commit()
        db.refresh(request)
        return request

    # ---------------- 节点查询与签署校验 ----------------

    @staticmethod
    def _effective_nodes(request: PurchaseApprovalRequest) -> Dict[int, ApprovalNode]:
        """每个级别取 seq 最大的非回避节点。"""
        result: Dict[int, ApprovalNode] = {}
        for node in request.nodes:
            if node.status == "recused":
                continue
            if node.level not in result or node.seq > result[node.level].seq:
                result[node.level] = node
        return result

    @staticmethod
    def _current_signable_level(request: PurchaseApprovalRequest) -> Optional[int]:
        effective = ApprovalService._effective_nodes(request)
        for level in sorted(effective):
            if effective[level].status == "pending":
                return level
        return None

    @staticmethod
    def act(db: Session, request_id: int, approver: str,
            action: str, comment: Optional[str] = None) -> PurchaseApprovalRequest:
        """批准 / 拒绝。重复签署、越级签署均被拦截并留痕。"""
        request = crud_approval_request.get(db, request_id)
        if not request:
            raise ApprovalError("审批申请不存在")
        if request.status != "pending":
            raise ApprovalError(f"申请当前状态为 {request.status}，不可签署")
        action = (action or "").strip().lower()
        if action not in ("approve", "reject"):
            raise ApprovalError("action 必须为 approve 或 reject")
        if not (approver or "").strip():
            raise ApprovalError("必须指明签署人")

        ApprovalService._verify_snapshot(request)
        effective = ApprovalService._effective_nodes(request)

        # 该签署人名下是否已有批准记录 —— 重复签署拦截
        already_approved = [n for n in request.nodes
                            if n.approver == approver and n.status == "approved"]
        target = None
        for level in sorted(effective):
            node = effective[level]
            if node.approver == approver and node.status == "pending":
                target = node
                break

        if action == "approve" and already_approved and target is None:
            ApprovalService._log(db, request.id, "duplicate_sign_blocked", approver, {
                "message": "同一签署人重复批准被拦截",
                "approved_nodes": [n.level for n in already_approved],
            })
            db.commit()
            raise ApprovalError(f"{approver} 已完成签署，不得重复签署")

        if target is None:
            if action == "reject":
                # 拒绝也必须作用于当前待签节点
                level = ApprovalService._current_signable_level(request)
                if level is not None and effective[level].approver == approver:
                    target = effective[level]
            if target is None:
                ApprovalService._log(db, request.id, "sign_out_of_route_blocked", approver, {
                    "message": "非当前路线待签人或越级签署被拦截",
                    "action": action,
                })
                db.commit()
                raise ApprovalError(f"当前没有待 {approver} 签署的节点（请按级别顺序审批）")

        # 顺序校验：低于该级别的节点必须全部批准
        for lower_level in sorted(effective):
            if lower_level >= target.level:
                break
            if effective[lower_level].status != "approved":
                ApprovalService._log(db, request.id, "sign_out_of_order_blocked", approver, {
                    "message": "越级签署被拦截",
                    "attempt_level": target.level,
                    "pending_lower_level": lower_level,
                })
                db.commit()
                raise ApprovalError("必须按级别顺序逐级审批，上级节点暂不可签")

        now = datetime.now()

        if action == "reject":
            if not (comment or "").strip():
                raise ApprovalError("拒绝必须填写理由")
            target.status = "rejected"
            target.action_comment = comment
            target.acted_at = now
            target.signed_snapshot_hash = request.snapshot_hash
            db.add(target)
            request.status = "rejected"
            db.add(request)
            ApprovalService._log(db, request.id, "rejected", approver, {
                "level": target.level, "level_name": target.level_name,
                "comment": comment, "snapshot_hash": request.snapshot_hash,
            })
            if request.suggestion_id:
                suggestion = crud_purchase_suggestion.get(db, request.suggestion_id)
                if suggestion and suggestion.status == "in_approval":
                    suggestion.status = "pending"
                    db.add(suggestion)
            db.commit()
            db.refresh(request)
            return request

        # ---- approve ----
        budget = ApprovalService.budget_state(
            db, request.budget_period, exclude_request_id=request.id)
        if not ApprovalService._budget_sufficient(budget, request.total_amount):
            ApprovalService._log(db, request.id, "budget_blocked", approver, {
                "message": "签署时预算不足，批准被阻断",
                "level": target.level, "amount": request.total_amount,
                "budget": budget,
            })
            db.commit()
            raise ApprovalError(
                f"预算期间{request.budget_period}额度已不足（可用{budget['available_amount']}元），"
                f"无法完成L{target.level}批准")

        ApprovalService._record_budget_change_if_any(db, request, budget, approver, target.level)

        target.status = "approved"
        target.action_comment = comment
        target.acted_at = now
        target.signed_snapshot_hash = request.snapshot_hash
        db.add(target)
        ApprovalService._log(db, request.id, "approved", approver, {
            "level": target.level, "level_name": target.level_name,
            "comment": comment, "snapshot_hash": request.snapshot_hash,
            "budget_after_check": budget,
        })

        # 全部有效节点批准 → 申请批准完成（但仍未创建正式订单，等待转单闸门）
        effective_now = ApprovalService._effective_nodes(request)
        all_approved = all(effective_now[l].status == "approved" for l in effective_now)
        if all_approved:
            request.status = "approved"
            db.add(request)
            ApprovalService._log(db, request.id, "all_approved", "系统", {
                "message": "全部有效批准已完成，可执行转单；正式订单尚未创建",
                "snapshot_hash": request.snapshot_hash,
            })

        db.commit()
        db.refresh(request)
        return request

    @staticmethod
    def _record_budget_change_if_any(db: Session, request: PurchaseApprovalRequest,
                                     current: Dict[str, Any], actor: str, level: int) -> None:
        """对比冻结时预算与当前预算（预算总额或已承诺金额变化即留痕），结果只记一次。"""
        if not current["configured"]:
            return
        snapshot = json.loads(request.snapshot_json)
        frozen = snapshot.get("budget_at_submit") or {}
        if not frozen.get("configured"):
            return
        frozen_total = frozen.get("total_budget")
        frozen_committed = frozen.get("committed_amount")
        if frozen_total == current["total_budget"] and frozen_committed == current["committed_amount"]:
            return
        # 与上一次预算变化记录比较，数值未再变化则不重复记录
        last = None
        for e in request.events:
            if e.event_type == "budget_changed":
                last = e
        if last:
            last_detail = json.loads(last.detail)
            prev_cur = last_detail.get("current") or {}
            if (prev_cur.get("total_budget") == current["total_budget"]
                    and prev_cur.get("committed_amount") == current["committed_amount"]):
                return
        ApprovalService._log(db, request.id, "budget_changed", actor, {
            "message": "审批期间预算发生变化，已按最新预算复核通过",
            "level_on_sign": level,
            "frozen_at_submit": {"total_budget": frozen_total,
                                 "committed_amount": frozen_committed},
            "current": {"total_budget": current["total_budget"],
                        "committed_amount": current["committed_amount"],
                        "pending_approval_amount": current["pending_approval_amount"],
                        "available_amount": current["available_amount"]},
        })

    # ---------------- 回避 ----------------

    @staticmethod
    def recuse(db: Session, request_id: int, approver: str,
               substitute_approver: str, reason: str) -> PurchaseApprovalRequest:
        request = crud_approval_request.get(db, request_id)
        if not request:
            raise ApprovalError("审批申请不存在")
        if request.status != "pending":
            raise ApprovalError(f"申请当前状态为 {request.status}，不可回避")
        if not (reason or "").strip():
            raise ApprovalError("回避必须填写理由")
        if not (substitute_approver or "").strip():
            raise ApprovalError("必须指定同级别替换审批人")

        ApprovalService._verify_snapshot(request)
        node = next((n for n in request.nodes
                     if n.approver == approver and n.status == "pending"), None)
        if node is None:
            raise ApprovalError(f"当前没有待 {approver} 处理的节点，无法回避")

        now = datetime.now()
        node.status = "recused"
        node.action_comment = reason
        node.acted_at = now
        db.add(node)

        same_level_nodes = [n for n in request.nodes if n.level == node.level]
        next_seq = max(n.seq for n in same_level_nodes) + 1
        substitute = ApprovalNode(
            request_id=request.id, level=node.level, level_name=node.level_name,
            approver=substitute_approver, seq=next_seq,
            is_substitute=True, substituted_for=approver, status="pending",
        )
        db.add(substitute)
        db.flush()

        ApprovalService._log(db, request.id, "recused", approver, {
            "level": node.level, "level_name": node.level_name,
            "reason": reason, "original_node_id": node.id,
        })
        ApprovalService._log(db, request.id, "substitute_assigned", "系统", {
            "level": node.level, "substitute_approver": substitute_approver,
            "substitute_node_id": substitute.id,
            "substituted_for": approver,
        })
        db.commit()
        db.refresh(request)
        return request

    # ---------------- 拒绝后重提 ----------------

    @staticmethod
    def resubmit(db: Session, request_id: int,
                 changes: ApprovalResubmitRequest) -> PurchaseApprovalRequest:
        parent = crud_approval_request.get(db, request_id)
        if not parent:
            raise ApprovalError("原审批申请不存在")
        if parent.status != "rejected":
            raise ApprovalError("仅被拒绝的申请可以重提")

        # 重提链深度，用于编号后缀
        depth = 1
        ancestor_id = parent.resubmitted_from_id
        root_no = parent.request_no
        while ancestor_id is not None:
            ancestor = crud_approval_request.get(db, ancestor_id)
            depth += 1
            root_no = ancestor.request_no
            ancestor_id = ancestor.resubmitted_from_id

        version = ApprovalService.ensure_default_version(db)
        supplier_id = changes.supplier_id if changes.supplier_id is not None else parent.supplier_id
        material_id = parent.material_id
        quantity = changes.quantity if changes.quantity is not None else parent.quantity
        unit_price = changes.unit_price if changes.unit_price is not None else parent.unit_price
        expected_date = changes.expected_date or parent.expected_date
        is_exception = (changes.is_exception_supplier
                        if changes.is_exception_supplier is not None
                        else parent.is_exception_supplier)
        exception_reason = (changes.exception_reason
                            if changes.exception_reason is not None
                            else parent.exception_reason)
        order_no = changes.order_no or parent.order_no

        supplier = db.get(Supplier, supplier_id)
        material = db.get(Material, material_id)
        if not supplier or not material:
            raise ApprovalError("供应商或物料不存在")
        if crud_approval_request.get_open_by_order_no(db, order_no):
            raise ApprovalError(f"订单号 {order_no} 已有进行中的审批申请")
        if crud_purchase_order.get_by_order_no(db, order_no):
            raise ApprovalError(f"订单号 {order_no} 已存在正式采购订单")

        ev = ApprovalService.evaluate(db, material, supplier, quantity, unit_price,
                                      is_exception, version)
        if ev["is_exception"] and not (exception_reason or "").strip():
            raise ApprovalError("例外供应商必须填写获批理由")

        period = changes.budget_period or expected_date.strftime("%Y-%m")
        budget = ApprovalService.budget_state(db, period)
        if not ApprovalService._budget_sufficient(budget, ev["total_amount"]):
            raise ApprovalError(
                f"预算期间{period}可用额度{budget['available_amount']}元，不足以承担本单")

        new_request = PurchaseApprovalRequest(
            suggestion_id=parent.suggestion_id,
            order_no=order_no,
            supplier_id=supplier.id,
            material_id=material.id,
            quantity=quantity,
            unit_price=unit_price,
            total_amount=ev["total_amount"],
            expected_date=expected_date,
            is_exception_supplier=ev["is_exception"],
            exception_reason=exception_reason if ev["is_exception"] else None,
            budget_period=period,
            risk_level=ev["risk_level"],
            required_level=ev["required_level"],
            rule_version_id=version.id,
            status="pending",
            resubmitted_from_id=parent.id,
            requester=changes.requester or parent.requester,
            remark=changes.remark,
        )
        db.add(new_request)
        db.flush()
        new_request.request_no = f"{root_no}-R{depth}"

        snapshot_payload = {
            "request_no": new_request.request_no,
            "order_no": order_no,
            "resubmitted_from": parent.request_no,
            "resubmit_chain_depth": depth,
            "supplier": {"id": supplier.id, "code": supplier.code, "name": supplier.name,
                         "rating": supplier.rating, "grade": ev["grade"]},
            "material": {"id": material.id, "code": material.code, "name": material.name,
                         "category": material.category, "is_critical": material.is_critical},
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": ev["total_amount"],
            "expected_date": expected_date,
            "is_exception_supplier": ev["is_exception"],
            "exception_basis": ev["exception_basis"],
            "exception_reason": new_request.exception_reason,
            "risk_level": ev["risk_level"],
            "risk_factors": ev["risk_factors"],
            "required_level": ev["required_level"],
            "rule_version": version.version,
            "thresholds": json.loads(version.thresholds_json),
            "route": ev["route"],
            "budget_at_submit": budget,
            "frozen_at": datetime.now().isoformat(timespec="seconds"),
        }
        text, digest = ApprovalService._build_snapshot(snapshot_payload)
        new_request.snapshot_json = text
        new_request.snapshot_hash = digest

        for t in ev["route"]:
            db.add(ApprovalNode(
                request_id=new_request.id, level=t["level"],
                level_name=t["level_name"], approver=t["approver"],
                seq=1, status="pending",
            ))

        ApprovalService._log(db, new_request.id, "resubmitted", new_request.requester, {
            "message": "拒绝后重新提交，原申请保留，按最新规则版本重走全路线",
            "parent_request_no": parent.request_no,
            "rule_version": version.version,
            "risk_level": ev["risk_level"],
            "required_level": ev["required_level"],
            "snapshot_hash": digest,
        })

        if parent.suggestion_id:
            suggestion = crud_purchase_suggestion.get(db, parent.suggestion_id)
            if suggestion and suggestion.status == "pending":
                suggestion.status = "in_approval"
                db.add(suggestion)

        db.commit()
        db.refresh(new_request)
        return new_request

    # ---------------- 撤销 ----------------

    @staticmethod
    def cancel(db: Session, request_id: int, operator: str) -> PurchaseApprovalRequest:
        request = crud_approval_request.get(db, request_id)
        if not request:
            raise ApprovalError("审批申请不存在")
        if request.status != "pending":
            raise ApprovalError(f"申请当前状态为 {request.status}，不可撤销")
        request.status = "cancelled"
        db.add(request)
        ApprovalService._log(db, request.id, "cancelled", operator,
                             {"message": "审批申请被撤销"})
        if request.suggestion_id:
            suggestion = crud_purchase_suggestion.get(db, request.suggestion_id)
            if suggestion and suggestion.status == "in_approval":
                suggestion.status = "pending"
                db.add(suggestion)
        db.commit()
        db.refresh(request)
        return request

    # ---------------- 转单闸门 ----------------

    @staticmethod
    def convert_to_order(db: Session, request_id: int) -> PurchaseOrder:
        """全部有效批准完成且预算足额，才创建正式采购订单。"""
        request = crud_approval_request.get(db, request_id)
        if not request:
            raise ApprovalError("审批申请不存在")
        if request.status == "converted" and request.purchase_order_id:
            raise ApprovalError("该申请已转为正式订单，不得重复转单")
        if request.status != "approved":
            raise ApprovalError(
                f"申请状态为 {request.status}，必须全部有效批准完成后方可转单")

        ApprovalService._verify_snapshot(request)
        effective = ApprovalService._effective_nodes(request)
        if not effective or any(n.status != "approved" for n in effective.values()):
            raise ApprovalError("存在未完成或被拒绝的审批节点，不得转单")
        if any(n.status not in ("approved", "recused") for n in request.nodes):
            raise ApprovalError("存在未终结的审批节点，不得转单")

        if crud_purchase_order.get_by_order_no(db, request.order_no):
            raise ApprovalError(f"订单号 {request.order_no} 的正式订单已存在")

        budget = ApprovalService.budget_state(
            db, request.budget_period, exclude_request_id=request.id)
        if not ApprovalService._budget_sufficient(budget, request.total_amount):
            ApprovalService._log(db, request.id, "budget_blocked", "系统", {
                "message": "转单时预算不足，正式订单创建被阻断",
                "amount": request.total_amount, "budget": budget,
            })
            db.commit()
            raise ApprovalError("预算额度已不足，转单被阻断，请调整预算或申请内容")

        remark_parts = [
            f"审批单号{request.request_no}全部批准后转单",
            f"审批规则v{request.rule_version.version}，风险等级{request.risk_level}",
        ]
        if request.is_exception_supplier:
            remark_parts.append(f"例外供应商获批理由：{request.exception_reason}")
        if request.remark:
            remark_parts.append(request.remark)

        order = PurchaseOrder(
            order_no=request.order_no,
            supplier_id=request.supplier_id,
            material_id=request.material_id,
            quantity=request.quantity,
            expected_date=request.expected_date,
            status="ordered",
            remark="；".join(remark_parts),
        )
        db.add(order)
        db.flush()

        request.purchase_order_id = order.id
        request.status = "converted"
        db.add(request)

        if request.suggestion_id:
            suggestion = crud_purchase_suggestion.get(db, request.suggestion_id)
            if suggestion:
                suggestion.status = "converted"
                db.add(suggestion)

        ApprovalService._log(db, request.id, "converted", "系统", {
            "message": "正式采购订单已创建",
            "purchase_order_id": order.id,
            "order_no": order.order_no,
            "budget_after_convert": ApprovalService.budget_state(db, request.budget_period),
        })
        db.commit()
        db.refresh(order)
        return order
