# 电网事故应急与恢复调度系统

标准库 Python 3.11+ + SQLite。系统管理停运事故、重要用户、备用容量、恢复步骤及安全依赖；接受现场离线报告并区分已合并、版本冲突和受保护记录，异常遥测单独隔离。重要用户保供步骤必须登记备用电源检查（检查时刻、可持续时长、截止时间、备用容量和用电需求），检查失效或余量不足时步骤不能确认；用电需求或检查结果更新后原确认失效，需要重新核验。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8215`。身份使用 `X-Actor` 和 `X-Role`，角色为 `dispatcher`、`operator`、`field`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/assets`、`POST /api/facilities`：登记线路资产和医院等重要用户。
- `POST /api/outages`：创建或幂等接收同一事故。
- `POST /api/telemetry`：记录并隔离错误遥测。
- `POST /api/plans`、`/submit`、`/approve`、`/activate`：创建、提交、审批并启用安全恢复计划。步骤可带 `facility_id` 表示该步骤保供的重要用户（必须属于步骤资产）。
- `POST /api/plans/{id}/change`：在不修改已确认步骤的前提下创建新计划版本。
- `POST /api/field-reports`：合并现场离线报告，重复客户端编号不会重复写入；保供步骤检查不过时，报告只附带 `backup_gap` 缺口说明，不能替代检查。
- `POST /api/plans/{id}/backup-checks`：调度员登记备用电源检查（`step_no`、`checked_at`、`sustainable_minutes`、`backup_mw`、可选 `demand_mw`，默认取步骤需求），截止时间由检查时刻加可持续时长计算。
- `POST /api/plans/{id}/backup-demand`：更新某保供步骤的用电需求，沿用最近一次检查；需求或检查结果更新后，依据旧记录的确认自动失效。
- `POST /api/plans/{id}/confirm`：调度员确认步骤，依赖未满足、备用检查失效或余量不足时拒绝。
- `POST /api/status`：发布当前恢复状态；发布"恢复完成"前，受影响区域内每个重要用户都必须有对应的有效检查，否则返回 409 并列出卡口。
- `GET /api/plans/{id}`、`GET /api/state`、`GET /api/health`：详情（含每步余量、剩余时间、确认有效性和 `stuck_reasons` 卡住原因）、状态和健康检查。

## 分层

- `backup.py`：纯判定层，只做截止时间、余量计算，不碰数据库。
- `backup_checks` 表与登记接口：检查记录层，追加留痕、不覆盖历史。
- `plan_detail` 与首页计划页：展示层，组装余量、剩余时间和卡住原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为原型：容量和依赖是静态安全模型，不包含潮流计算、SCADA/EMS 协议、实时遥测质量码或生产级多实例锁；离线合并通过客户端编号和计划版本完成。
