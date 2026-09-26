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
- `POST /api/claims/finalize`：锁定最终核定结果（已维护受损标的的案件须改走结算核定）
- `GET /api/claims/<id>/settlement`：定损标的、共保份额、核定前检查、结算预览与历史版本
- `POST /api/loss-items`、`/api/loss-items/delete`、`/api/loss-items/review`：维护受损标的（保额、损失比例、残值、免赔额）并逐项复核，录入人不能复核自己的标的
- `POST /api/coinsurers`：维护共保人份额（仅监督人员）
- `POST /api/claims/settlement/approve`：结算核定。份额合计不足100%、任一标的赔款为负或尚未复核时返回 409 及具体阻断项；通过后按份额拆分生成冻结的结算明细
- `POST /api/claims/settlement/supplement`：已核定案件补证重开，调整后可再次核定生成新版本，旧版本明细保留可查

单项赔款 = 保额 × 损失比例 − 残值 − 免赔额；按共保份额拆分时，分位尾差由末位共保人承担。核定页面为 `/settlement`。

## 模块结构

- `app.py`：HTTP 接口与理赔流程编排（服务层）
- `settlement_calc.py`：赔款试算、核定前检查、份额拆分的纯计算
- `settlement_store.py`：受损标的、共保份额、结算版本/明细的 SQLite 存取
- `static/`：状态页与定损结算页

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别、角色权限，以及逐项定损、核定阻断、结算冻结与补证版本。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源；赔款金额以浮点两位小数入库，未使用定点数。
