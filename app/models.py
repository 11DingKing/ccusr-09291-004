from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text, Date
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
class Material(Base):
    __tablename__ = "materials"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    spec = Column(String(200))
    unit = Column(String(20), nullable=False)
    safety_stock = Column(Integer, default=0)
    is_critical = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    bom_items = relationship("BOMItem", back_populates="material")
    supply_capacities = relationship("SupplyCapacity", back_populates="material")
    purchase_suggestions = relationship("PurchaseSuggestion", back_populates="material")
    purchase_orders = relationship("PurchaseOrder", back_populates="material")
    deliveries = relationship("Delivery", back_populates="material")
    inventory_batches = relationship("InventoryBatch", back_populates="material")
    alternative_materials = relationship("AlternativeMaterial", 
                                         foreign_keys="AlternativeMaterial.material_id", 
                                         back_populates="material")
    alternative_for = relationship("AlternativeMaterial",
                                   foreign_keys="AlternativeMaterial.alternative_material_id",
                                   back_populates="alternative_material")

class VehicleModel(Base):
    __tablename__ = "vehicle_models"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    priority = Column(Integer, default=5)
    description = Column(Text)
    status = Column(String(20), default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    bom_items = relationship("BOMItem", back_populates="vehicle_model")
    production_batches = relationship("ProductionBatch", back_populates="vehicle_model")
    alternative_restrictions = relationship("AlternativeMaterialRestriction", back_populates="vehicle_model")

class BOMItem(Base):
    __tablename__ = "bom_items"
    id = Column(Integer, primary_key=True, index=True)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    remark = Column(String(200))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vehicle_model = relationship("VehicleModel", back_populates="bom_items")
    material = relationship("Material", back_populates="bom_items")

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    contact = Column(String(50))
    phone = Column(String(30))
    address = Column(String(300))
    rating = Column(Float, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supply_capacities = relationship("SupplyCapacity", back_populates="supplier")
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    deliveries = relationship("Delivery", back_populates="supplier")

class SupplyCapacity(Base):
    __tablename__ = "supply_capacities"
    id = Column(Integer, primary_key=True, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    daily_capacity = Column(Integer, nullable=False)
    delivery_days = Column(Integer, nullable=False)
    pass_rate = Column(Float, nullable=False)
    current_stock = Column(Integer, default=0)
    unit_price = Column(Float, default=0)
    is_preferred = Column(Boolean, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    supplier = relationship("Supplier", back_populates="supply_capacities")
    material = relationship("Material", back_populates="supply_capacities")

class PurchaseSuggestion(Base):
    __tablename__ = "purchase_suggestions"
    id = Column(Integer, primary_key=True, index=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    suggested_quantity = Column(Integer, nullable=False)
    reason = Column(String(300))
    priority = Column(Integer, default=5)
    suggested_supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    expected_delivery_date = Column(Date)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    material = relationship("Material", back_populates="purchase_suggestions")

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String(50), unique=True, index=True, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    expected_date = Column(Date, nullable=False)
    actual_date = Column(Date)
    status = Column(String(20), default="ordered")
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supplier = relationship("Supplier", back_populates="purchase_orders")
    material = relationship("Material", back_populates="purchase_orders")
    deliveries = relationship("Delivery", back_populates="purchase_order")
    delay_impacts = relationship("DelayImpact", back_populates="purchase_order")

class Delivery(Base):
    __tablename__ = "deliveries"
    id = Column(Integer, primary_key=True, index=True)
    delivery_no = Column(String(50), unique=True, index=True, nullable=False)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    delivery_date = Column(Date, nullable=False)
    batch_no = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase_order = relationship("PurchaseOrder", back_populates="deliveries")
    supplier = relationship("Supplier", back_populates="deliveries")
    material = relationship("Material", back_populates="deliveries")
    inspection = relationship("Inspection", back_populates="delivery", uselist=False)
    inventory_batch = relationship("InventoryBatch", back_populates="delivery", uselist=False)

class Inspection(Base):
    __tablename__ = "inspections"
    id = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False)
    sample_size = Column(Integer, nullable=False)
    defective_count = Column(Integer, default=0)
    pass_rate = Column(Float, nullable=False)
    result = Column(String(20), nullable=False)
    inspector = Column(String(50))
    inspection_date = Column(Date, nullable=False)
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    delivery = relationship("Delivery", back_populates="inspection")

class InventoryBatch(Base):
    __tablename__ = "inventory_batches"
    id = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    available_quantity = Column(Integer, nullable=False)
    is_quarantined = Column(Boolean, default=False)
    quarantine_reason = Column(String(300))
    location = Column(String(100))
    expire_date = Column(Date)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    delivery = relationship("Delivery", back_populates="inventory_batch")
    material = relationship("Material", back_populates="inventory_batches")

class AlternativeMaterial(Base):
    __tablename__ = "alternative_materials"
    id = Column(Integer, primary_key=True, index=True)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    alternative_material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    priority = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    material = relationship("Material", foreign_keys=[material_id], back_populates="alternative_materials")
    alternative_material = relationship("Material", foreign_keys=[alternative_material_id], back_populates="alternative_for")
    restrictions = relationship("AlternativeMaterialRestriction", back_populates="alternative")

class AlternativeMaterialRestriction(Base):
    __tablename__ = "alternative_restrictions"
    id = Column(Integer, primary_key=True, index=True)
    alternative_id = Column(Integer, ForeignKey("alternative_materials.id"), nullable=False)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    is_allowed = Column(Boolean, default=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    alternative = relationship("AlternativeMaterial", back_populates="restrictions")
    vehicle_model = relationship("VehicleModel", back_populates="alternative_restrictions")

class ProductionBatch(Base):
    __tablename__ = "production_batches"
    id = Column(Integer, primary_key=True, index=True)
    batch_no = Column(String(50), unique=True, index=True, nullable=False)
    vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    plan_date = Column(Date, nullable=False)
    status = Column(String(20), default="planned")
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vehicle_model = relationship("VehicleModel", back_populates="production_batches")
    delay_impacts = relationship("DelayImpact", back_populates="production_batch")

class DelayImpact(Base):
    __tablename__ = "delay_impacts"
    id = Column(Integer, primary_key=True, index=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    production_batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    impact_level = Column(String(20), nullable=False)
    estimated_delay_days = Column(Integer, default=0)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    purchase_order = relationship("PurchaseOrder", back_populates="delay_impacts")
    production_batch = relationship("ProductionBatch", back_populates="delay_impacts")

class SupplierConfirmation(Base):
    __tablename__ = "supplier_confirmations"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_no = Column(String(50), unique=True, index=True, nullable=False)
    purchase_suggestion_id = Column(Integer, ForeignKey("purchase_suggestions.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    requested_quantity = Column(Integer, nullable=False)
    committed_quantity = Column(Integer, nullable=False)
    committed_delivery_date = Column(Date)
    shortage_quantity = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    confirmation_note = Column(Text)
    confirmed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supplier = relationship("Supplier")
    material = relationship("Material")
    purchase_suggestion = relationship("PurchaseSuggestion")
    batches = relationship("SupplierConfirmationBatch", back_populates="confirmation", cascade="all, delete-orphan")
    shortage_impacts = relationship("SupplierShortageImpact", back_populates="confirmation", cascade="all, delete-orphan")

class SupplierConfirmationBatch(Base):
    __tablename__ = "supplier_confirmation_batches"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_id = Column(Integer, ForeignKey("supplier_confirmations.id"), nullable=False)
    batch_no = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    planned_date = Column(Date, nullable=False)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    confirmation = relationship("SupplierConfirmation", back_populates="batches")

class SupplierShortageImpact(Base):
    __tablename__ = "supplier_shortage_impacts"
    id = Column(Integer, primary_key=True, index=True)
    confirmation_id = Column(Integer, ForeignKey("supplier_confirmations.id"), nullable=False)
    production_batch_id = Column(Integer, ForeignKey("production_batches.id"), nullable=False)
    affected_vehicle_model_id = Column(Integer, ForeignKey("vehicle_models.id"), nullable=False)
    shortage_material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    shortage_quantity = Column(Integer, nullable=False)
    impact_level = Column(String(20), nullable=False)
    estimated_delay_days = Column(Integer, default=0)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    confirmation = relationship("SupplierConfirmation", back_populates="shortage_impacts")
    production_batch = relationship("ProductionBatch")
    vehicle_model = relationship("VehicleModel")
    material = relationship("Material")


# ==================== 采购订单分级审批工作流 ====================

class ApprovalRuleVersion(Base):
    """审批规则版本：每次规则换版新增一行，历史版本永久保留，已发起的申请锁定其发起时的版本。"""
    __tablename__ = "approval_rule_versions"
    id = Column(Integer, primary_key=True, index=True)
    version = Column(Integer, unique=True, index=True, nullable=False)
    thresholds_json = Column(Text, nullable=False)  # 金额分级阈值与级别定义
    critical_uplift = Column(Integer, default=1)    # 关键物料上调级别数
    low_grade_uplift = Column(Integer, default=1)   # C级供应商上调级别数
    exception_uplift = Column(Integer, default=1)  # 例外供应商上调级别数
    change_note = Column(String(300))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class BudgetPeriod(Base):
    """采购预算期间（按月），金额单位元。"""
    __tablename__ = "budget_periods"
    id = Column(Integer, primary_key=True, index=True)
    period = Column(String(7), unique=True, index=True, nullable=False)  # YYYY-MM
    total_budget = Column(Float, nullable=False, default=0)
    remark = Column(String(300))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class PurchaseApprovalRequest(Base):
    """采购转单审批申请：正式采购订单在全部有效批准完成前不得创建。"""
    __tablename__ = "purchase_approval_requests"
    id = Column(Integer, primary_key=True, index=True)
    request_no = Column(String(50), unique=True, index=True)  # 取到自增id后立即生成，业务唯一
    suggestion_id = Column(Integer, ForeignKey("purchase_suggestions.id"))
    order_no = Column(String(50), nullable=False)            # 预期正式订单号，冻结
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False, default=0)
    total_amount = Column(Float, nullable=False, default=0)
    expected_date = Column(Date, nullable=False)
    is_exception_supplier = Column(Boolean, default=False)   # 例外供应商：无供货能力记录或非优选
    exception_reason = Column(Text)                          # 例外供应商获批理由（必填留痕）
    budget_period = Column(String(7), nullable=False)

    # 风险评估结果（申请时固化）
    risk_level = Column(String(10), nullable=False)          # low/medium/high
    required_level = Column(Integer, nullable=False)         # 最终需要的最高审批级别
    rule_version_id = Column(Integer, ForeignKey("approval_rule_versions.id"), nullable=False)

    # 冻结摘要（flush 取 id 后、commit 前赋值）
    snapshot_json = Column(Text)               # 签署人看到的订单摘要全文
    snapshot_hash = Column(String(64))        # SHA-256，防篡改

    status = Column(String(20), default="pending")           # pending/approved/rejected/cancelled/converted
    resubmitted_from_id = Column(Integer, ForeignKey("purchase_approval_requests.id"))
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"))  # 转单成功后回填
    requester = Column(String(50), default="采购员")
    remark = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    supplier = relationship("Supplier")
    material = relationship("Material")
    rule_version = relationship("ApprovalRuleVersion")
    nodes = relationship("ApprovalNode", back_populates="request",
                         cascade="all, delete-orphan",
                         order_by="ApprovalNode.id")
    events = relationship("ApprovalEvent", back_populates="request",
                          cascade="all, delete-orphan",
                          order_by="ApprovalEvent.id")
    purchase_order = relationship("PurchaseOrder", foreign_keys=[purchase_order_id])
    resubmitted_from = relationship("PurchaseApprovalRequest", remote_side="PurchaseApprovalRequest.id")


class ApprovalNode(Base):
    """审批节点：一条路线按级别顺序排列；回避时原节点保留(recused)，替换审批人新增节点，保证可核对。"""
    __tablename__ = "approval_nodes"
    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("purchase_approval_requests.id"), nullable=False)
    level = Column(Integer, nullable=False)
    level_name = Column(String(50), nullable=False)
    approver = Column(String(50), nullable=False)
    seq = Column(Integer, nullable=False)                    # 同级别内的签署序号（回避替换会递增）
    is_substitute = Column(Boolean, default=False)           # 是否回避替换人
    substituted_for = Column(String(50))                     # 被替换（回避）的原审批人
    status = Column(String(20), default="pending")           # pending/approved/rejected/recused
    action_comment = Column(Text)
    acted_at = Column(DateTime(timezone=True))
    # 该审批人签署时看到的冻结摘要哈希，事后可核对"所见即所签"
    signed_snapshot_hash = Column(String(64))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    request = relationship("PurchaseApprovalRequest", back_populates="nodes")


class ApprovalEvent(Base):
    """审批事件流水：只增不改，所有动作（提交/批准/拒绝/回避/预算变化/重复签署拦截/换版/转单）均留痕。"""
    __tablename__ = "approval_events"
    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("purchase_approval_requests.id"), nullable=False)
    event_type = Column(String(30), nullable=False)
    actor = Column(String(50))
    detail = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    request = relationship("PurchaseApprovalRequest", back_populates="events")
