# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果

## 逐项定损与共保结算

案件进入复核（review）后，按「版本」维护逐项定损，核定后按共保份额自动拆成结算明细：

- `POST /api/settlements/version`：新建定损版本（补证时再次调用即生成新版本）
- `POST /api/settlements/items` / `items/review` / `items/remove`：维护受损标的（保额、损失比例、残值、免赔额）、复核、删除录错项
- `POST /api/settlements/shares`：整组维护共保人份额
- `POST /api/settlements/finalize`：核定。份额合计不足 100%、某项赔款为负（残值+免赔额超过损失额）或存在未复核项时拒绝，并在报错中点名具体缺项
- `GET /api/settlements?claim_id=N[&version=M]`、`GET /api/settlements/versions?claim_id=N`：查看当前/历史版本，旧版结算明细永久可查

核定即冻结：该版定损项、份额与结算明细不可再改。页面见 `/settlement`（状态页 `/` 有入口）。
单项赔款 = 保额 × 损失比例 − 残值 − 免赔额；分摊尾差归入份额最大的共保人，保证明细合计与赔款合计一致。

## 分层

- `settlement_calc.py`：纯计算（单项赔款、份额拆分、核定阻断清单），不依赖数据库
- `settlement_store.py`：SQLite 存储与冻结快照（settlement_* 四张表）
- `app.py`：应用服务与 HTTP 路由（角色、状态机、审计）
- `static/settlement.html`：结算页面，只调用 JSON 接口

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别、角色权限，以及逐项定损的拆分舍入、核定阻断、版本冻结与补证流程。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
