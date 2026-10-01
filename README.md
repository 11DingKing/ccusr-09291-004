# 零部件供应协同系统

这是一个以 FastAPI 提供 HTTP 接口、以 SQLite 保存业务数据的 Python 服务端项目。项目覆盖多个相互关联的业务边界，所有验收都可在单个 Linux 应用容器中离线完成，不依赖浏览器、设备或额外运行服务。

## 本地测试

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q tests
```

## 编译检查

```bash
python -m compileall -q .
```

## 容器运行

```bash
docker build -t partforge-service .
docker run --rm -p 8000:8000 partforge-service
```

启动后可访问 `GET /health` 或根路径确认服务状态。运行测试会使用临时或本地 SQLite 文件，不需要外部数据库。

## 采购订单分级审批

审计整改后，**采购员不得直接转单或手工创建正式采购订单**：`POST /purchase/suggestions/{id}/convert`
与 `POST /purchase/orders` 均返回 403。所有正式订单必须经分级审批，由闸门统一创建。

分级规则（`approval_rule_versions` 按版本永久留痕）：

- 金额定基础级别：`<5万` 采购主管(L1)、`≥5万` 采购经理(L2)、`≥20万` 财务总监(L3)、`≥50万` 总经理(L4)；
- 关键物料、C级供应商（评级<4.5）、例外供应商（无供货能力记录或非优选）各上调 1 级（L4 封顶）；
- 例外供应商必须填写获批理由，理由随冻结摘要与正式订单备注留痕。

流程接口（前缀 `/api/v1/approvals`）：

| 接口 | 说明 |
| --- | --- |
| `POST /preview` | 提交前试算金额、风险等级、分级路线与预算占用，不落库 |
| `POST /requests` | 提交申请：冻结订单摘要（JSON + SHA-256），锁定当前规则版本与预算快照 |
| `POST /requests/{id}/act?approver=` | 按级别顺序 `approve`/`reject`；拒绝必填理由 |
| `POST /requests/{id}/recuse?approver=` | 审批人回避（必填理由），指定同级别替换人 |
| `POST /requests/{id}/resubmit` | 被拒绝后重提：旧单保留，新单关联父单，按最新规则重走全路线 |
| `POST /requests/{id}/cancel` | 撤销在途申请 |
| `POST /requests/{id}/convert` | **转单闸门**：全部有效批准完成且预算足额，才创建正式采购订单 |
| `GET /rules`、`POST /rules` | 查询 / 发布规则版本（换版只对新申请生效，在途申请锁定旧版） |
| `PUT /budgets/{YYYY-MM}`、`GET /budgets/{period}/usage` | 维护期间预算并查询占用 |

控制与留痕要点：

- 签署人看到的摘要在提交时冻结；每次签署、回避、转单前重新校验哈希，篡改即阻断；节点记录签署时的摘要哈希。
- 全部有效批准完成前申请停留在 `pending/approved`，不会产生正式订单；越级、重复签署被拦截。
- 回避保留原 `recused` 节点，替换人以新节点（`is_substitute`）承接，路线连续可核对。
- 审批期间预算总额或已承诺金额变化会写 `budget_changed` 流水；预算不足在签署与转单环节均阻断。
- 所有动作写入只增事件表 `approval_events`（提交/批准/拒绝/回避/替换/预算变化/重复签署拦截/换版告知/转单）。

