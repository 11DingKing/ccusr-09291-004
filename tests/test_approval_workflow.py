import pytest
from datetime import date, timedelta
from tests.test_data_factory import DataFactory

from app.services.approval import ApprovalService, ApprovalError, DEFAULT_THRESHOLDS
from app.crud.approval import (
    crud_approval_request, crud_budget_period,
)
from app.crud.purchase import crud_purchase_order, crud_purchase_suggestion
from app.schemas import (
    ApprovalSubmitRequest, ApprovalActionRequest, ApprovalRecuseRequest,
    ApprovalResubmitRequest, ApprovalRuleVersionCreate,
    BudgetPeriodCreate,
)


def make_submit(factory, *, order_no, supplier_code, material_code=None,
                suggestion=None, quantity=100, unit_price=None,
                expected_date=None, is_exception=False, exception_reason=None,
                budget_period="2026-10", requester="测试采购员"):
    return ApprovalSubmitRequest(
        suggestion_id=suggestion.id if suggestion else None,
        order_no=order_no,
        supplier_id=factory.suppliers[supplier_code].id,
        material_id=factory.materials[material_code].id if material_code else None,
        quantity=quantity,
        unit_price=unit_price,
        expected_date=expected_date or date(2026, 10, 20),
        is_exception_supplier=is_exception,
        exception_reason=exception_reason,
        budget_period=budget_period,
        requester=requester,
    )


def approve_all(db, req, approvers):
    for ap in approvers:
        ApprovalService.act(db, req.id, ap, "approve")


def event_types(req):
    return [e.event_type for e in req.events]


class TestTieredRoute:
    """按订单金额、物料风险、供应商等级生成分级审批路线。"""

    def test_low_amount_noncritical_single_level(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        # TM003 非关键，TS002 优选、B级，单价80 → 8000元
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-LOW-1", supplier_code="TS002",
            material_code="TM003", quantity=100, unit_price=80))
        assert req.required_level == 1
        assert [n.level for n in req.nodes] == [1]
        assert req.risk_level == "medium"  # B级供应商

    def test_amount_escalation_levels(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        # 非关键 TM003 + B级优选，仅金额驱动：8000→L1, 80000→L2, 240000→L3, 800000→L4
        for qty, expect in [(100, 1), (1000, 2), (3000, 3), (10000, 4)]:
            req = ApprovalService.submit(db_session, make_submit(
                f, order_no=f"AP-AMT-{expect}", supplier_code="TS002",
                material_code="TM003", quantity=qty, unit_price=80))
            assert req.required_level == expect, f"数量{qty}金额{qty*80}应定级L{expect}"

    def test_critical_material_uplift(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        # TM001 关键，TS001 A级优选 单价1500：15000元本为L1，关键物料+1→L2
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-CRIT-1", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))
        assert req.required_level == 2
        assert req.risk_level == "high"
        import json
        factors = json.loads(req.snapshot_json)["risk_factors"]
        assert any("关键物料" in x for x in factors)
        # 高金额 + 关键：300000元本L3，+1→L4封顶
        req2 = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-CRIT-2", supplier_code="TS001",
            material_code="TM001", quantity=200, unit_price=1500))
        assert req2.required_level == 4

    def test_c_grade_supplier_uplift(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        f.create_supplier("TS009", "测试低评级供应商", rating=4.0)
        f.create_supply_capacity("TS009", "TM003", daily_capacity=100,
                                 delivery_days=9, pass_rate=0.9, current_stock=10,
                                 unit_price=80, is_preferred=True)
        # 8000元本L1，C级+1→L2
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-CGRADE-1", supplier_code="TS009",
            material_code="TM003", quantity=100, unit_price=80))
        assert req.required_level == 2
        assert "C" in req.snapshot_json

    def test_exception_supplier_detected_and_uplift(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        # TS001 对 TM003 无供货能力记录 → 自动识别为例外供应商，需理由
        with pytest.raises(ApprovalError, match="例外供应商"):
            ApprovalService.submit(db_session, make_submit(
                f, order_no="AP-EXC-1", supplier_code="TS001",
                material_code="TM003", quantity=100, unit_price=80))
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-EXC-2", supplier_code="TS001",
            material_code="TM003", quantity=100, unit_price=80,
            exception_reason="指定供应商独家定制，特批"))
        assert req.is_exception_supplier is True
        assert req.exception_reason == "指定供应商独家定制，特批"
        # 8000本L1 + 例外1级 → L2
        assert req.required_level == 2

    def test_non_preferred_supplier_is_exception(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        # 新增一个对 TM003 有产能但非优选的供应商
        f.create_supplier("TS010", "测试非优选供应商", rating=4.6)
        f.create_supply_capacity("TS010", "TM003", daily_capacity=800,
                                 delivery_days=9, pass_rate=0.98, current_stock=100,
                                 unit_price=80, is_preferred=False)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-NP-1", supplier_code="TS010",
            material_code="TM003", quantity=100, unit_price=80,
            exception_reason="优选供应商交期不足，改用非优选"))
        assert req.is_exception_supplier is True
        assert req.required_level == 2


class TestFrozenSnapshot:
    """签署人看到的订单摘要必须冻结。"""

    def test_snapshot_hash_and_signature_anchored(self, db_session):
        import hashlib
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-FRZ-1", supplier_code="TS002",
            material_code="TM003", quantity=100, unit_price=80))
        digest = hashlib.sha256(req.snapshot_json.encode("utf-8")).hexdigest()
        assert digest == req.snapshot_hash
        ApprovalService.act(db_session, req.id, "采购主管", "approve")
        db_session.refresh(req)
        signed = [n for n in req.nodes if n.status == "approved"][0]
        assert signed.signed_snapshot_hash == req.snapshot_hash

    def test_tampered_snapshot_blocks_signing(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-FRZ-2", supplier_code="TS002",
            material_code="TM003", quantity=100, unit_price=80))
        # 模拟有人篡改冻结摘要正文
        req.snapshot_json = req.snapshot_json.replace("80", "800")
        db_session.commit()
        with pytest.raises(ApprovalError, match="冻结摘要校验失败"):
            ApprovalService.act(db_session, req.id, "采购主管", "approve")


class TestApprovalGate:
    """全部有效批准完成前不得创建正式订单。"""

    def test_cannot_convert_before_all_approvals(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-GATE-1", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))  # L2
        # 未批准
        with pytest.raises(ApprovalError):
            ApprovalService.convert_to_order(db_session, req.id)
        # 仅L1批准
        ApprovalService.act(db_session, req.id, "采购主管", "approve")
        with pytest.raises(ApprovalError, match="全部有效批准"):
            ApprovalService.convert_to_order(db_session, req.id)
        assert crud_purchase_order.get_by_order_no(db_session, "AP-GATE-1") is None

    def test_convert_after_full_approval_creates_order(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-GATE-2", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))
        approve_all(db_session, req, ["采购主管", "采购经理"])
        db_session.refresh(req)
        assert req.status == "approved"
        order = ApprovalService.convert_to_order(db_session, req.id)
        assert order.order_no == "AP-GATE-2"
        db_session.refresh(req)
        assert req.status == "converted"
        assert req.purchase_order_id == order.id
        # 例外理由等写入订单备注
        # 重复转单被拦截
        with pytest.raises(ApprovalError, match="已转为正式订单"):
            ApprovalService.convert_to_order(db_session, req.id)

    def test_exception_reason_propagates_to_order_remark(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-GATE-3", supplier_code="TS001",
            material_code="TM003", quantity=100, unit_price=80,
            exception_reason="独家定制特批理由XYZ"))
        approve_all(db_session, req, ["采购主管", "采购经理"])
        order = ApprovalService.convert_to_order(db_session, req.id)
        assert "独家定制特批理由XYZ" in order.remark

    def test_direct_conversion_service_blocked(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        from app.services.purchase import PurchaseService
        suggestions = PurchaseService.generate_purchase_suggestions(db_session)
        sug = next(s for s in suggestions if s.material.code == "TM003")
        with pytest.raises(PermissionError):
            PurchaseService.convert_suggestion_to_order(
                db_session, sug.id, "BYPASS-1")

    def test_direct_order_http_endpoints_blocked(self, db_session, override_get_db):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        # 手工直接建订单
        r = client.post("/api/v1/purchase/orders", json={
            "order_no": "HTTP-BYPASS-1", "supplier_id": 1, "material_id": 1,
            "quantity": 1, "expected_date": "2026-10-20"})
        assert r.status_code == 403
        # 建议直接转单
        r2 = client.post("/api/v1/purchase/suggestions/1/convert",
                         params={"order_no": "HTTP-BYPASS-2"})
        assert r2.status_code == 403
        assert crud_purchase_order.get_by_order_no(db_session, "HTTP-BYPASS-1") is None


class TestRecusal:
    """审批人回避：原节点保留、替换人另起节点。"""

    def test_recuse_preserves_node_and_adds_substitute(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-REC-1", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))  # L2
        ApprovalService.recuse(db_session, req.id, "采购主管",
                               substitute_approver="代理主管王", reason="与供应商有利害关系")
        db_session.refresh(req)
        level1_nodes = [n for n in req.nodes if n.level == 1]
        original = next(n for n in level1_nodes if n.approver == "采购主管")
        sub = next(n for n in level1_nodes if n.approver == "代理主管王")
        assert original.status == "recused"
        assert "利害关系" in original.action_comment
        assert sub.is_substitute is True
        assert sub.substituted_for == "采购主管"
        assert sub.seq == 2
        # 回避动作与替换分配均留痕
        assert "recused" in event_types(req)
        assert "substitute_assigned" in event_types(req)
        # 替换人批准后可继续走L2并转单
        ApprovalService.act(db_session, req.id, "代理主管王", "approve")
        ApprovalService.act(db_session, req.id, "采购经理", "approve")
        order = ApprovalService.convert_to_order(db_session, req.id)
        assert order.order_no == "AP-REC-1"

    def test_recuse_requires_reason(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-REC-2", supplier_code="TS002",
            material_code="TM003", quantity=100, unit_price=80))
        with pytest.raises(ApprovalError, match="回避必须填写理由"):
            ApprovalService.recuse(db_session, req.id, "采购主管",
                                   substitute_approver="代理人", reason="  ")


class TestBudgetControl:
    """审批期间预算变化必须留痕，预算不足阻断。"""

    def _set_budget(self, db, period, amount):
        existing = crud_budget_period.get_by_period(db, period)
        if existing:
            return crud_budget_period.update(
                db, db_obj=existing, obj_in={"total_budget": amount})
        return crud_budget_period.create(db, obj_in=BudgetPeriodCreate(
            period=period, total_budget=amount))

    def test_budget_insufficient_blocks_submit(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        self._set_budget(db_session, "2026-10", 5000)
        with pytest.raises(ApprovalError, match="预算"):
            ApprovalService.submit(db_session, make_submit(
                f, order_no="AP-BUD-1", supplier_code="TS002",
                material_code="TM003", quantity=100, unit_price=80))  # 8000 > 5000

    def test_budget_reduction_mid_approval_blocks_and_logs(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        self._set_budget(db_session, "2026-10", 100000)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-BUD-2", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))  # 15000，L2
        ApprovalService.act(db_session, req.id, "采购主管", "approve")
        # 审批期间预算被削减到不足以承担本单
        self._set_budget(db_session, "2026-10", 10000)
        with pytest.raises(ApprovalError, match="预算"):
            ApprovalService.act(db_session, req.id, "采购经理", "approve")
        db_session.refresh(req)
        assert "budget_blocked" in event_types(req)

    def test_budget_change_recorded_when_still_sufficient(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        self._set_budget(db_session, "2026-10", 100000)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-BUD-3", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))
        ApprovalService.act(db_session, req.id, "采购主管", "approve")
        # 追加预算（仍足额）→ 应留 budget_changed 痕迹且不阻断
        self._set_budget(db_session, "2026-10", 120000)
        ApprovalService.act(db_session, req.id, "采购经理", "approve")
        db_session.refresh(req)
        assert "budget_changed" in event_types(req)

    def test_inflight_encumbrance_blocks_second_request(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        self._set_budget(db_session, "2026-10", 20000)
        # 第一张 15000（关键TM001 L2），提交后冻结占用
        req1 = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-BUD-5", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))
        assert req1.total_amount == 15000
        # 第二张 8000，合计 23000 > 20000 → 提交即被预算拦截
        with pytest.raises(ApprovalError, match="预算"):
            ApprovalService.submit(db_session, make_submit(
                f, order_no="AP-BUD-6", supplier_code="TS002",
                material_code="TM003", quantity=100, unit_price=80))
        # 第一张全批但未转单（approved）期间，额度仍被冻结
        ApprovalService.act(db_session, req1.id, "采购主管", "approve")
        ApprovalService.act(db_session, req1.id, "采购经理", "approve")
        db_session.refresh(req1)
        assert req1.status == "approved"
        with pytest.raises(ApprovalError, match="预算"):
            ApprovalService.submit(db_session, make_submit(
                f, order_no="AP-BUD-7", supplier_code="TS002",
                material_code="TM003", quantity=100, unit_price=80))
        # 第一张转单后，剩余5000额度仍不足8000，依然拦截
        ApprovalService.convert_to_order(db_session, req1.id)
        with pytest.raises(ApprovalError, match="预算"):
            ApprovalService.submit(db_session, make_submit(
                f, order_no="AP-BUD-8", supplier_code="TS002",
                material_code="TM003", quantity=100, unit_price=80))

    def test_budget_blocks_conversion_if_depleted(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        self._set_budget(db_session, "2026-10", 100000)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-BUD-4", supplier_code="TS002",
            material_code="TM003", quantity=100, unit_price=80))  # 8000 L1
        ApprovalService.act(db_session, req.id, "采购主管", "approve")
        # 批准后、转单前预算被削减到不足以承担本单（本单8000元）
        self._set_budget(db_session, "2026-10", 7000)
        with pytest.raises(ApprovalError, match="预算"):
            ApprovalService.convert_to_order(db_session, req.id)
        db_session.refresh(req)
        assert req.status == "approved"  # 未被转单


class TestRejectAndResubmit:
    """拒绝后重提：旧单保留、新单关联、重走全路线。"""

    def test_reject_requires_comment_and_reopens_suggestion(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        suggestions = PurchaseService_generate(db_session)
        sug = next(s for s in suggestions if s.material.code == "TM003")
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-REJ-1", supplier_code="TS002",
            material_code="TM003", quantity=100, unit_price=80,
            suggestion=sug))
        db_session.refresh(sug)
        assert sug.status == "in_approval"
        with pytest.raises(ApprovalError, match="拒绝必须填写理由"):
            ApprovalService.act(db_session, req.id, "采购主管", "reject")
        ApprovalService.act(db_session, req.id, "采购主管", "reject",
                            comment="价格偏高，重新议价")
        db_session.refresh(req)
        db_session.refresh(sug)
        assert req.status == "rejected"
        assert sug.status == "pending"

    def test_resubmit_creates_linked_new_request(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-REJ-2", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))  # 15000 L2
        ApprovalService.act(db_session, req.id, "采购主管", "reject",
                            comment="数量过多")
        # 重提，减量到 15000→改为 5件*1500=7500，仍关键，定级L1（金额L1+关键1=L2）
        new_req = ApprovalService.resubmit(db_session, req.id, ApprovalResubmitRequest(
            quantity=3, unit_price=1500))  # 4500 L1 + 关键 → L2
        assert new_req.id != req.id
        assert new_req.resubmitted_from_id == req.id
        assert new_req.request_no.endswith("-R1")
        # 原单保留为 rejected
        db_session.refresh(req)
        assert req.status == "rejected"
        # 新单重走全路线
        assert [n.status for n in new_req.nodes] == ["pending", "pending"]
        assert "resubmitted" in event_types(new_req)
        approve_all(db_session, new_req, ["采购主管", "采购经理"])
        order = ApprovalService.convert_to_order(db_session, new_req.id)
        assert order.quantity == 3


class TestDuplicateAndOrderSigning:
    """重复签署与越级签署应被拦截并留痕。"""

    def test_duplicate_approval_blocked(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        # 多级路线（关键物料 L2）：L1 签完后整单仍 pending，L1 再签应触发重复签署拦截
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-DUP-1", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))
        ApprovalService.act(db_session, req.id, "采购主管", "approve")
        with pytest.raises(ApprovalError, match="重复签署"):
            ApprovalService.act(db_session, req.id, "采购主管", "approve")
        db_session.refresh(req)
        assert "duplicate_sign_blocked" in event_types(req)
        # 原批准仍有效，L2 可正常完成
        ApprovalService.act(db_session, req.id, "采购经理", "approve")
        db_session.refresh(req)
        assert req.status == "approved"

    def test_out_of_order_signing_blocked(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        ApprovalService.ensure_default_version(db_session)
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-ORD-1", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))  # L2
        # 采购经理在采购主管之前签 → 越级拦截
        with pytest.raises(ApprovalError):
            ApprovalService.act(db_session, req.id, "采购经理", "approve")
        db_session.refresh(req)
        types = event_types(req)
        assert "sign_out_of_route_blocked" in types or "sign_out_of_order_blocked" in types
        # 节点仍未被签署
        assert all(n.status == "pending" for n in req.nodes)


class TestRuleVersioning:
    """规则换版：历史版本保留，在途申请锁定旧版。"""

    def test_new_version_applies_forward_only(self, db_session):
        f = DataFactory(db_session)
        f.setup_basic_supply_chain()
        v1 = ApprovalService.ensure_default_version(db_session)
        # 在途申请（TM003 非关键 B级优选 8000元 → v1 下 L1）
        req = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-VER-1", supplier_code="TS002",
            material_code="TM003", quantity=100, unit_price=80))
        assert req.rule_version_id == v1.id
        assert req.required_level == 1

        # 换版 v2：金额阈值提高到 100000 才 L2（8000仍L1），并关闭关键物料上调
        new_thresholds = [
            {"level": 1, "level_name": "采购主管", "min_amount": 0, "approver": "采购主管"},
            {"level": 2, "level_name": "采购经理", "min_amount": 100000, "approver": "采购经理"},
        ]
        v2 = ApprovalService.publish_new_version(
            db_session, thresholds=new_thresholds,
            critical_uplift=0, low_grade_uplift=0, exception_uplift=0,
            change_note="放宽审批权限试点")
        assert v2.version == 2
        db_session.refresh(req)
        # 在途申请仍锁定 v1，路线不变，且有换版告知流水
        assert req.rule_version_id == v1.id
        assert req.required_level == 1
        assert "rule_version_published" in event_types(req)

        # 新申请适用 v2：关键物料 TM001 大额单在 v2 下仅按金额定级
        req2 = ApprovalService.submit(db_session, make_submit(
            f, order_no="AP-VER-2", supplier_code="TS001",
            material_code="TM001", quantity=10, unit_price=1500))  # 15000
        assert req2.rule_version_id == v2.id
        assert req2.required_level == 1  # 阈值10万、关键不上调

    def test_rule_versions_history_retained(self, db_session):
        ApprovalService.ensure_default_version(db_session)
        ApprovalService.publish_new_version(db_session, change_note="第二次换版")
        versions = crud_approval_rule_version_all(db_session)
        assert [v.version for v in versions] == [2, 1]
        assert versions[1].is_active is False
        assert versions[0].is_active is True


def PurchaseService_generate(db_session):
    from app.services.purchase import PurchaseService
    return PurchaseService.generate_purchase_suggestions(db_session)


# 便捷查询补充
def crud_approval_rule_version_all(db):
    from app.models import ApprovalRuleVersion
    return db.query(ApprovalRuleVersion).order_by(
        ApprovalRuleVersion.version.desc()).all()
