# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额、恢复保费、份额台账、逐家分摊和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 份额台账与批单

- 绑单（`bind`）必须录入份额台账：`{"underwriter_id":"...","shares":[{"reinsurer":"SwissRe","share_pct":0.6,"limit":400000}, ...]}`。份额合计必须等于100%且再保险人不得重复，否则绑单无法确认。
- 计算（`calculate`）按当前份额生成逐家摊回明细（`recovery_breakdown`）：应摊金额、额度、覆盖金额；超出该家额度的部分标为未覆盖（`uncovered`），合计记入`uncovered_amount`。
- 结算（`settle`）将当时的份额与逐家账单固化到`settlement_snapshot`，财务据此向各家再保险人开具账单。
- 批单（`endorse`，仅承保人角色）用于调整份额：`{"reason":"...","shares":[...]}`，原因必填，版本号`endorsement_version`递增并保留历史`endorsements`。未结算案件（`calculated`）按最新批单自动重算明细，已结算记录的账单快照不受影响。
- 详情接口与演示页展示当前份额、逐家金额、未覆盖金额和批单版本历史。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
