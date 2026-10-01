from sqlalchemy.orm import Session
from typing import List, Optional
from app.crud.base import CRUDBase
from app.models import (
    ApprovalRuleVersion, BudgetPeriod, PurchaseApprovalRequest,
    ApprovalNode, ApprovalEvent, PurchaseOrder
)
from app.schemas import ApprovalRuleVersionCreate, BudgetPeriodCreate, BudgetPeriodUpdate


class CRUDApprovalRuleVersion(CRUDBase[ApprovalRuleVersion, ApprovalRuleVersionCreate, dict]):
    def get_active(self, db: Session) -> Optional[ApprovalRuleVersion]:
        return db.query(ApprovalRuleVersion).filter(
            ApprovalRuleVersion.is_active == True
        ).order_by(ApprovalRuleVersion.version.desc()).first()

    def get_by_version(self, db: Session, version: int) -> Optional[ApprovalRuleVersion]:
        return db.query(ApprovalRuleVersion).filter(
            ApprovalRuleVersion.version == version
        ).first()

    def get_max_version(self, db: Session) -> int:
        obj = db.query(ApprovalRuleVersion).order_by(
            ApprovalRuleVersion.version.desc()
        ).first()
        return obj.version if obj else 0


crud_approval_rule_version = CRUDApprovalRuleVersion(ApprovalRuleVersion)


class CRUDBudgetPeriod(CRUDBase[BudgetPeriod, BudgetPeriodCreate, BudgetPeriodUpdate]):
    def get_by_period(self, db: Session, period: str) -> Optional[BudgetPeriod]:
        return db.query(BudgetPeriod).filter(BudgetPeriod.period == period).first()


crud_budget_period = CRUDBudgetPeriod(BudgetPeriod)


class CRUDApprovalRequest(CRUDBase[PurchaseApprovalRequest, dict, dict]):
    def get_by_request_no(self, db: Session, request_no: str) -> Optional[PurchaseApprovalRequest]:
        return db.query(PurchaseApprovalRequest).filter(
            PurchaseApprovalRequest.request_no == request_no
        ).first()

    def get_by_order_no(self, db: Session, order_no: str) -> Optional[PurchaseApprovalRequest]:
        return db.query(PurchaseApprovalRequest).filter(
            PurchaseApprovalRequest.order_no == order_no
        ).order_by(PurchaseApprovalRequest.id.desc()).first()

    def get_open_by_order_no(self, db: Session, order_no: str) -> Optional[PurchaseApprovalRequest]:
        """进行中（pending）的申请，用于防止同一订单号重复走审批。"""
        return db.query(PurchaseApprovalRequest).filter(
            PurchaseApprovalRequest.order_no == order_no,
            PurchaseApprovalRequest.status == "pending"
        ).first()

    def get_multi_by_status(self, db: Session, status: Optional[str] = None) -> List[PurchaseApprovalRequest]:
        q = db.query(PurchaseApprovalRequest)
        if status:
            q = q.filter(PurchaseApprovalRequest.status == status)
        return q.order_by(PurchaseApprovalRequest.id.desc()).all()

    def get_pending_encumbrance(self, db: Session, period: str,
                                exclude_request_id: Optional[int] = None) -> float:
        """该预算期间内尚未转为正式订单的预占用金额合计。

        包含两类申请：
        - pending：审批进行中；
        - approved：全部有效批准已完成、但尚未执行转单（额度须继续冻结至转单）。
        """
        q = db.query(PurchaseApprovalRequest).filter(
            PurchaseApprovalRequest.budget_period == period,
            PurchaseApprovalRequest.status.in_(["pending", "approved"])
        )
        if exclude_request_id is not None:
            q = q.filter(PurchaseApprovalRequest.id != exclude_request_id)
        return sum(r.total_amount for r in q.all())

    def get_converted_amount(self, db: Session, period: str) -> float:
        """该期间已转为正式订单（并已创建采购订单）的金额合计。"""
        rows = db.query(PurchaseApprovalRequest).filter(
            PurchaseApprovalRequest.budget_period == period,
            PurchaseApprovalRequest.status == "converted",
            PurchaseApprovalRequest.purchase_order_id.isnot(None)
        ).all()
        return sum(r.total_amount for r in rows)


crud_approval_request = CRUDApprovalRequest(PurchaseApprovalRequest)


class CRUDApprovalNode(CRUDBase[ApprovalNode, dict, dict]):
    def get_by_request(self, db: Session, request_id: int) -> List[ApprovalNode]:
        return db.query(ApprovalNode).filter(
            ApprovalNode.request_id == request_id
        ).order_by(ApprovalNode.level, ApprovalNode.seq, ApprovalNode.id).all()

    def get_active_node_for_level(self, db: Session, request_id: int,
                                  level: int) -> Optional[ApprovalNode]:
        """某级别当前有效（非回避）节点。"""
        return db.query(ApprovalNode).filter(
            ApprovalNode.request_id == request_id,
            ApprovalNode.level == level,
            ApprovalNode.status != "recused"
        ).order_by(ApprovalNode.seq.desc()).first()


crud_approval_node = CRUDApprovalNode(ApprovalNode)


class CRUDApprovalEvent(CRUDBase[ApprovalEvent, dict, dict]):
    def get_by_request(self, db: Session, request_id: int) -> List[ApprovalEvent]:
        return db.query(ApprovalEvent).filter(
            ApprovalEvent.request_id == request_id
        ).order_by(ApprovalEvent.id).all()


crud_approval_event = CRUDApprovalEvent(ApprovalEvent)
