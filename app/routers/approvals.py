from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date

from app.database import get_db
from app.services.approval import ApprovalService, ApprovalError
from app.crud.approval import (
    crud_approval_rule_version, crud_budget_period, crud_approval_request,
)
from app.schemas import (
    ApprovalSubmitRequest, ApprovalActionRequest, ApprovalRecuseRequest,
    ApprovalResubmitRequest,
    ApprovalRequestSchema, ApprovalRoutePreview,
    ApprovalRuleVersionCreate, ApprovalRuleVersionSchema,
    BudgetPeriodCreate, BudgetPeriodUpdate, BudgetPeriod, BudgetUsage,
)

router = APIRouter(prefix="/approvals", tags=["采购分级审批"])


@router.post("/preview", response_model=ApprovalRoutePreview)
def preview_route(
    payload: ApprovalSubmitRequest,
    db: Session = Depends(get_db),
):
    """提交前试算分级路线、风险因子与预算占用，不落库。"""
    from app.crud.purchase import crud_purchase_suggestion
    from app.crud.supplier import crud_supply_capacity

    suggestion = None
    if payload.suggestion_id is not None:
        suggestion = crud_purchase_suggestion.get(db, payload.suggestion_id)
        if not suggestion:
            raise HTTPException(status_code=400, detail="采购建议不存在")

    material_id = payload.material_id or (suggestion.material_id if suggestion else None)
    if material_id is None:
        raise HTTPException(status_code=400, detail="必须指定 material_id 或 suggestion_id")

    quantity = payload.quantity or (suggestion.suggested_quantity if suggestion else None)
    if not quantity:
        raise HTTPException(status_code=400, detail="必须指定数量")

    unit_price = payload.unit_price
    if unit_price is None:
        capacity = crud_supply_capacity.get_by_supplier_and_material(
            db, payload.supplier_id, material_id)
        unit_price = capacity.unit_price if capacity else 0.0

    expected_date = payload.expected_date or (
        suggestion.expected_delivery_date if suggestion else None)
    period = payload.budget_period or (
        expected_date.strftime("%Y-%m") if expected_date
        else date.today().strftime("%Y-%m"))

    try:
        result = ApprovalService.preview_route(
            db,
            supplier_id=payload.supplier_id,
            material_id=material_id,
            quantity=quantity,
            unit_price=unit_price,
            is_exception_supplier=payload.is_exception_supplier,
            budget_period=period,
        )
        return result
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/requests", response_model=ApprovalRequestSchema, status_code=201)
def submit_request(payload: ApprovalSubmitRequest, db: Session = Depends(get_db)):
    try:
        return ApprovalService.submit(db, payload)
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/requests", response_model=List[ApprovalRequestSchema])
def list_requests(status: Optional[str] = None, db: Session = Depends(get_db)):
    return crud_approval_request.get_multi_by_status(db, status)


@router.get("/requests/{request_id}", response_model=ApprovalRequestSchema)
def get_request(request_id: int, db: Session = Depends(get_db)):
    req = crud_approval_request.get(db, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="审批申请不存在")
    return req


@router.post("/requests/{request_id}/act", response_model=ApprovalRequestSchema)
def act_on_request(
    request_id: int,
    approver: str,
    payload: ApprovalActionRequest,
    db: Session = Depends(get_db),
):
    try:
        return ApprovalService.act(db, request_id, approver,
                                   payload.action, payload.comment)
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/requests/{request_id}/recuse", response_model=ApprovalRequestSchema)
def recuse_request(
    request_id: int,
    approver: str,
    payload: ApprovalRecuseRequest,
    db: Session = Depends(get_db),
):
    try:
        return ApprovalService.recuse(
            db, request_id, approver,
            payload.substitute_approver, payload.reason
        )
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/requests/{request_id}/resubmit",
             response_model=ApprovalRequestSchema, status_code=201)
def resubmit_request(
    request_id: int,
    payload: ApprovalResubmitRequest,
    db: Session = Depends(get_db),
):
    try:
        return ApprovalService.resubmit(db, request_id, payload)
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/requests/{request_id}/cancel", response_model=ApprovalRequestSchema)
def cancel_request(request_id: int, operator: str, db: Session = Depends(get_db)):
    try:
        return ApprovalService.cancel(db, request_id, operator)
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/requests/{request_id}/convert")
def convert_request_to_order(request_id: int, db: Session = Depends(get_db)):
    """转单闸门：全部有效批准完成且预算足额，才创建正式采购订单。"""
    try:
        order = ApprovalService.convert_to_order(db, request_id)
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "message": "正式采购订单已创建",
        "approval_request_id": request_id,
        "purchase_order_id": order.id,
        "order_no": order.order_no,
    }


# ---------------- 规则版本管理 ----------------

@router.get("/rules", response_model=List[ApprovalRuleVersionSchema])
def list_rule_versions(db: Session = Depends(get_db)):
    from app.models import ApprovalRuleVersion
    ApprovalService.ensure_default_version(db)
    return db.query(ApprovalRuleVersion).order_by(
        ApprovalRuleVersion.version.desc()).all()


@router.post("/rules", response_model=ApprovalRuleVersionSchema, status_code=201)
def publish_rule_version(
    payload: ApprovalRuleVersionCreate,
    db: Session = Depends(get_db),
):
    import json
    from app.services.approval import DEFAULT_THRESHOLDS
    thresholds = None
    if payload.thresholds_json:
        try:
            thresholds = json.loads(payload.thresholds_json)
        except ValueError:
            raise HTTPException(status_code=400, detail="thresholds_json 不是合法 JSON")
    try:
        return ApprovalService.publish_new_version(
            db,
            thresholds=thresholds or DEFAULT_THRESHOLDS,
            critical_uplift=payload.critical_uplift,
            low_grade_uplift=payload.low_grade_uplift,
            exception_uplift=payload.exception_uplift,
            change_note=payload.change_note,
        )
    except ApprovalError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- 预算期间管理 ----------------

@router.get("/budgets", response_model=List[BudgetPeriod])
def list_budgets(db: Session = Depends(get_db)):
    return crud_budget_period.get_multi(db, limit=1000)


@router.put("/budgets/{period}", response_model=BudgetPeriod)
def upsert_budget(
    period: str,
    payload: BudgetPeriodUpdate,
    db: Session = Depends(get_db),
):
    existing = crud_budget_period.get_by_period(db, period)
    if existing:
        return crud_budget_period.update(db, db_obj=existing, obj_in=payload)
    created = crud_budget_period.create(db, obj_in=BudgetPeriodCreate(
        period=period,
        total_budget=payload.total_budget if payload.total_budget is not None else 0,
        remark=payload.remark,
    ))
    return created


@router.get("/budgets/{period}/usage", response_model=BudgetUsage)
def budget_usage(period: str, db: Session = Depends(get_db)):
    state = ApprovalService.budget_state(db, period)
    if not state["configured"]:
        raise HTTPException(status_code=404,
                            detail=f"预算期间 {period} 尚未配置")
    return state
