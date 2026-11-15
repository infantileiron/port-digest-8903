# 临港 · 港站晨报定时分发

把昨日计划到发、延误分位数与交接班晨报串起来，由真实worker到点生成站内收件箱回执，保留冻结来源。

## 场景与工作流

临港联运运营北站、南站和东港三个港站，每天约150条班次记录。现行工作流是值班员导出 `临港到发_20261114.xlsx`，分析员按日期筛选后手算延误，负责人再把表格转发给下一班。痛点是跨午夜班次归错日、未到发被当准点、重复转发后分不清同一口径。上游为港站计划/实际到发记录；本项目替代日界归档、晨报试算和定时分发；下游为站内值班室交接班。

三个核心实体：Arrival到发记录、Subscription晨报订阅、Receipt分发回执，另有用户和审计；离线补传新增ArrivalBatch批次、ArrivalBatchItem批次行与ArrivalTombstone撤销墓碑。五页：到发台账、晨报预览、订阅计划、分发回执、补传批次。回执支持值班复核与下载，原始记录修订不改变已分发晨报。

|角色|权限|典型动作|
|---|---|---|
|admin|全部；独占订阅配置/值班复核/审计|设定08:30晨报并核对回执|
|analyst|到发录入/修订、接收/完成补传批次、更正重放隔离项、晨报试算、读取计划回执、导出|补录跨午夜班次、处理离线补传|
|viewer|业务只读与回执导出|下载冻结晨报；后端拒绝写入/计算|

## 快速启动

```sh
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 [localhost:18212](http://localhost:18212)，API [localhost:19212/docs](http://localhost:19212/docs)，PostgreSQL映射17712。账号 `admin@demo.local`、`analyst@demo.local`、`viewer@demo.local`，密码均 `Demo123!`。种子为3账号、150条昨日计划到发、3条暂停订阅；不会自动对外发送。自然编号幂等，重复运行不覆盖人工修订；种子日期为首次启动的昨日，后续日需业务录入新记录。后端启动时执行 `python -m scripts.migrate`，按文件名顺序幂等应用 migrations（001 基线在库已有 arrivals 时跳过，002 增补补传表与修订列）。

## 一日业务剧本

1. 08:00 viewer 查看LG-001至LG-150，核对未到发为空。
2. 08:05 analyst 录入 `LG-151`，北站，计划2026-11-14 23:55，实际2026-11-15 00:05，交接说明为跨午夜。
3. 08:10 在晨报预览选择2026-11-14和北站，检查该班次仍归14日、延误10分钟。
4. 08:15 admin 创建“北站08:30值班晨报”，站内收件箱“北站值班室”，每日08:30，启用。
5. 演示时可把“首次到点”设为未来10秒；不需要任何手动发送按钮。
6. 08:30 worker每秒检查到点订阅，冻结昨日来源并写入回执。
7. 08:31 刷新分发回执，核对P50/P95、完成/未到发人数和来源checksum。
8. 08:35 admin 写入“跨日班次已核对”复核意见；viewer下载 `port-digest-<业务日>-<回执id>.json`。
9. 09:00 analyst 在“补传批次”接收港站离线包：坏行只隔离本行并留原因，好行照常完成；输入条数必须等于生效+过旧+重复+隔离。隔离行可更正重放，原行原文与处理结果保留可追溯。

## 离线补传、修订合并与隔离重放

**班次身份** = 港站 + 班次编号 + 上海计划日。各站同编号班次按计划日区分，同编号跨站/跨日互不覆盖；历史 `code` 唯一约束由 `(station, code, planned_day)` 取代。人工录入/修订仍可随时进行，每次编辑递增台账修订号；身份（港站/编号/计划日）可迁移，旧身份立同修订号墓碑，低修订补传不能把原班次带回来。

**批次生命周期** `receiving` → `completed`。接收阶段只落原始行并做结构校验（JSON对象、港站、编号、带偏移的计划/实际时间、3天范围、修订号、撤销标记），结构错误直接隔离并写明原因。完成阶段在单事务内逐行合并，每行必落到四类之一：

|结果|条件|
|---|---|
|生效 applied|修订高于台账当前修订，或撤销修订高于已记录撤销水印|
|过旧 stale|修订低于台账当前修订；或撤销后修订不高于撤销修订水印（旧修订不得复活班次）|
|重复 duplicate|同号同版本且内容指纹一致|
|隔离 quarantined|坏行；或同号同版本异内容；或台账缺少指纹无法判定|

**乱序**先按班次身份分组、再按来源修订号降序合并，批内高修订先落地，低修订自然记为过旧。**撤销**写入墓碑水印（`revoked_revision`），撤销行本身不产生台账记录；撤销后只接受更高的修订号：补传行需高于撤销修订，人工编辑原记录也会生成更高的台账修订并把班次恢复；人工另建同一班次仍被拒绝（须先恢复原记录）。**条数守恒**在完成时强制校验 `输入条数 = 生效 + 过旧 + 重复 + 隔离`，不平则整批回滚并不标记完成。**重试**同批次编号同内容直接返回原批次（`created:false`），同编号异内容返回409；已完成批次再次完成返回 `processed:false`，不重复写入。**并发**用 PostgreSQL 事务级 advisory lock 按班次身份串行化合并，加上行锁与唯一约束；多批次争同班次最终保留最高来源修订，低修订记为过旧。**隔离重放**只接受已隔离且原批次已完成的行，生成独立重放批次，回填原行的 `resolved_by_item_id / resolution_status / resolved_at`；同一重放批次编号同内容可幂等重试，已解决的隔离行不能再次重放。`GET /arrival-batch-items/{id}/trace` 返回原行、原批次、全部重放行和最终处理结果。

**晨报口径**只统计有效班次：撤销记录从台账读取时即排除，试算与到点回执都不计入。已分发回执payload冻结，后续补传、撤销或复核都不改变原值。`GET /arrivals?include_revoked=true` 仅admin可见撤销记录。

## 计算与调度口径

**归档**按计划到发时间转Asia/Shanghai后的自然日，而非实际到达日。后端时间输入必须有偏移（如+08:00），统一保存UTC；前端datetime-local明确按上海解释。实际可空，表示未到发。时间差超过3天拒绝，提示核对。

**延误**=max(0,实际−计划)分钟，提前到达记0。已到发才进入延误率与分位数分母；未到发单独计数。P50/P95使用排序后位置(n−1)p的线性插值；没有已到发样本时分位数、均值和延误率为null，页面显示“未定义”。输出带完整选中来源、口径版本与SHA256 checksum。

**到点调度**由worker每秒扫描UTC next_run_at，Redis锁串行化扫描，数据库用行锁和唯一约束 `(subscription_id, report_day)` 防止同订阅同报告日重复入箱。首次时间可设未来数秒进行真实调度验证；以后按Asia/Shanghai每日HH:MM安排。到点或服务中断恢复时只生成“执行时刻的最新昨日”一份，不补历史天；同日再次到点只推进next_run_at，不新增回执。暂停订阅不执行。修订订阅如果未填写首次时间，会重新安排下一个每日时刻；恢复暂停也按此规则。

晨报“送达”表示已提交站内数据库收件箱，不是SMTP邮件。回执保存冻结payload，DB提交后附加写共享产物卷；附加文件失败不影响DB回执和导出。值班复核仅管理员可做，一次复核不可覆盖，且不改变payload。支持单worker；唯一约束与锁防重复，但不声称对外消息的exactly-once。

## 工程、环境变量和开发

Python3.11/FastAPI/SQLAlchemy/PyJWT、原生ES modules/CSS、Nginx、PostgreSQL16、Redis7。恰好frontend/backend/worker/db/redis五服务，各有健康检查；backend等db/redis、worker还等backend、frontend等backend。Redis实际提供调度锁；worker心跳 `/tmp/heartbeat`，DB/Redis/产物分别命名卷。Nginx `/api/` 去前缀代理后端根路由。

`.env.example`列出COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO、NETWORK_SUBNET。显式子网10.253.113.0/24规避当前机默认地址池耗尽，可按环境调整。演示凭据应在正式部署替换。清理本项目用 `docker compose down -v --remove-orphans`。

本地开发：启动db/redis，backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET，先 `python -m scripts.migrate`，再运行 `uvicorn src.main:app --reload` 和 `python -m src.worker`；前端仍建议用Nginx同源代理。后端目录src按routes/controllers/services/models/schemas/middlewares/config/utils/constants分工；前端src按api/stores/pages/components/hooks/utils/constants/types/router分工。ORM建表用于空库，migrations是实际DDL来源，`scripts/migrate.py` 是幂等执行器。

枚举：后端 `src/constants/enums.py`、`src/schemas/business.py` 与前端 `src/constants/enums.js` 定义ROLES、STATIONS、RECEIPT_STATES、BATCH_STATES与BATCH_ITEM_STATES；TIMEZONE固定Asia/Shanghai。共用表格、表单、状态标签、晨报摘要组件；表单hooks、HTML转义和日期工具；请求ID/安全响应头两个中间件，业务审计与全局错误恢复。测试与截图在 `../../test-artifacts/gb-3113/`，不放源码内。

范围：逐条录入/完整修订，离线补传以JSON批次（最大2000行）接收，无CSV解析、删除或分页；只有三个固定港站与上海时区。补传来源修订号由上游提供，系统不生成；缺省视为修订0，因此撤销行应显式携带高于原记录的修订号。站内收件箱按名称记录，当前所有登录角色可读业务回执，无多租户邮箱隔离；不连接邮件、短信或外部IM。Google Fonts离线时字体回退。许可证MIT，见LICENSE。
