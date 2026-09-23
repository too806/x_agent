# Phase 4a 交接文档：Publish 状态机

> 生成于 2026-09-21；2026-09-23 据实机验证结果同步事实。
> Phase 3 封版于 `530c0e6`，Phase 4a 在其之上。
> 测试基线：157 passed（Phase 3 的 62 项原样通过）。
> **2026-09-23：真实 `POST /2/tweets` 已执行 2 次并成功落库（见第 10 节）。
> 单元测试仍然全部 mock，不产生外部副作用。**

---

## 1. 本阶段做了什么

V1 最小闭环的最后两环：

```
Topic → Research → Generate → Guardrail → Human Review → [Publish] → Record
                                                          ^^^^^^^^^
                                                          Phase 4a
```

`published_post` 表从 initial schema 起就存在，但**从未被应用代码写入过**（dev 库实测 0 行），
也没有状态字段或唯一约束。X API client 原先只有 `GET /2/tweets/search/recent`，
没有任何发帖能力。本阶段把「发布」这个**带外部副作用、不可幂等**的动作，
接入一个可恢复、可审计、失败时不重复发推的状态机。

---

## 2. 已锁定的设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **Publish 不做成 LangGraph 节点**，走独立 REST | 发生 `interrupt()` 的节点每次 resume 都会**从头重放**。把不可幂等的推文放进设计上就会重放的执行器，是结构性风险。Graph / state / nodes / checkpointer **一行未改**。 |
| D2 | **`publish_unknown` 保守人工裁决**，不做自动 reconciliation | 自动改成 `published` 或 `failed` 都会让后续处理做出错误决定。 |
| D3 | **内容事实来源是 review 表**，不是 checkpoint | `approve → review.original_content`，`edit → review.edited_content`。**绝不读 `draft.body`** —— edit 路径下那是没人批准过的原稿。checkpoint 的 `final_content` 只做一致性校验：无值→放行，有值但不等→409。 |
| D4 | **不引入新基础设施** | 只用现有 FastAPI + PostgreSQL + SQLAlchemy + LangGraph checkpoint。 |
| D5 | **`finished_at` 重定义为「整个 Run 真正终结」** | 只有 `published / rejected / failed / completed` 才写。`approved` / `edited` 还要走发布，**不是终态**，`finished_at` 保持为空。 |

---

## 3. 状态机

**两级状态机，同一个事务内写两份**：`agent_run.status` 是给运维查询用的镜像，
`published_post.status` 才是发布的权威记录。

```
approved / edited
      │  claim_publish()：同一事务内
      │    ① CAS agent_run: approved|edited → publishing, finished_at=NULL
      │    ② 复用 status=failed 的行 UPDATE，或 INSERT 新行（UNIQUE 兜底）
      │  commit 成功后才允许调 X
      ▼
  publishing ──── X API ────┬─ 2xx + data.id ──► published
                            │                      （agent_run 一并 → published, finished_at=now）
                            ├─ 4xx / 连接未达 ───► failed
                            │                      （agent_run 还原 → approved / edited）
                            └─ 5xx / 读超时 / 无 id ─► publish_unknown
                                                     （agent_run → publish_unknown）
                                                            │
                                                    人工裁决 /publish/resolve
```

### 四个状态的语义

| 状态 | 我们手上有什么信息 | 自动重试 |
|---|---|---|
| `publishing` | 尝试已提交，本地**一无所知**（崩溃 / 被 kill / 仍在飞行中） | ❌ |
| `publish_unknown` | 尝试**已经结束**，拿到了信息却判不出结果 | ❌ |
| `published` | 确凿，`x_post_id` 必非空 | — 终态 |
| `failed` | 确定没发出去 | ✅ 可**显式**重试 |

`UNRESOLVED_STATUSES = (publishing, publish_unknown)` —— 停留在其中任何一个时，
**任何自动路径都不允许再次调用 X**。

**为什么不能只用 `x_post_id IS NULL` 表达状态**：它无法区分 `failed`（可重试）
与「结果未知」（绝不能重试）—— 而这两者的处置方式**相反**。
这正是本阶段唯一的安全属性，隐式编码会丢掉它。

---

## 4. 关键不变量（改动这些必须有测试失败）

1. **抢占先于调用 X，且单独提交**（`claim_publish` 自带 commit）。
   失败方向是刻意的：宁可留下「以为发出去了」的痕迹交给人工裁决，
   也不能没有痕迹 —— 后者恢复时会当成「从没发过」而**重复发推**。
2. **不对称写入，证据压过猜测**：
   - `mark_published` 接受 `status IN (publishing, failed)` —— X 的 2xx 是事实，
     能纠正人工基于过期观察做的猜测。
   - `mark_publish_failed` 只接受 `status = publishing` —— 猜测不能覆盖事实。
3. **发布失败不是流水线失败**：`agent_run` 只会被还原成 `approved` / `edited`，
   **永远不写 `failed`**。还原值由 `_revert_run_to_review_outcome()` 从 review 表推导，
   不信任调用方（HTTP 请求可能早已被取消）。
4. **Run 状态与审核结论必须互相钉死**（`run.status != REVIEW_STATUS_TO_RUN_STATUS[review.status]` → 409）。
   `claim_run_for_review` 先改 Run 状态、之后才 resume 去 `create_review`，
   窗口期内**上一次残留的 review 行**会被当成这次的决定 ——
   照它发布就会发出一个人这次并没有批准过的东西。
5. **`list_published_contents()` 必须带 `WHERE x_post_id IS NOT NULL`** ——
   只有真正上过 X 的记录才参与 Guardrail 判重。

### 关于 UNIQUE(agent_run_id)：第二道防线，不是承重结构

采纳 agent_run 层 CAS 之后，**并发的主要串行化点是 agent_run 的条件 UPDATE**
（两个请求改同一行 → 行锁排队 → READ COMMITTED 重检 WHERE → 后来者拿 0 行）。

变异验证实测：**现在去掉 UNIQUE 不会让两个并发测试失败**，只有
`test_one_record_per_run_is_enforced_by_the_schema`（直接二次 INSERT 断言 `IntegrityError`）能抓到它。
它守的是「一个 Run 最多一条发布记录」这个 schema 级不变量，
防的是应用层绕过 CAS 的写入，**而不是并发本身**。

> 历史注记：在 agent_run CAS 引入之前，去掉 UNIQUE 会导致三个并发 `claim_publish`
> 全部返回 `True`（`[True,True,True]`）、三个请求都去调 X。该观察现已过时。

---

## 5. 不可消除的窗口（必须知情）

```
[标记已提交] ──网络──> [X 已创建推文] ──回程──> [本地保存 post_id]
     ▲                                                  ▲
     └──── 崩溃落在这段，本地永远不知道结果 ──────────────┘
```

PostgreSQL 的事务管不到网络调用，X API v2 **没有幂等键**。
因此 **exactly-once 发布在现有原语下不可实现**。

本设计把它**收窄并显式化**：崩溃 → 残留 `publishing` → `GET` 可见 → 人工裁决
（`POST /api/runs/{id}/publish/resolve`）。**不自动重试、不自动对账。**

---

## 6. API

### `POST /api/runs/{run_id}/publish`

| 前置 | 行为 |
|---|---|
| `approved` / `edited` + 审核结论一致 | 发布 → `published` 200 |
| 已 `published` | **409 + 已有结果**，绝不再次调 X |
| `publishing` / `publish_unknown` | **409 + 当前状态**，绝不自动调 X |
| `waiting_for_review` / `rejected` / 其他 | 409 |
| run.status 与 review 结论不匹配 | 409（resume 窗口保护） |
| 内容超 `persona.max_length` | 422，**不发起 HTTP 请求** |

响应体统一带 `status` / `x_post_id` / `detail` ——「已经发过了」和「结果未知需要人工裁决」
都是 409，但处置方式完全不同，只给状态码不够。

### `POST /api/runs/{run_id}/publish/resolve`

判别联合（复用 `ReviewDecision` 的模式）：

- `{"action":"mark_published","x_post_id":"..."}` → `published`
- `{"action":"retry","confirm_maybe_duplicate":true}` → 只把状态改回 `failed`，**本请求不发帖**

真正重发必须另调一次 `POST /publish`（两道确认），误触代价从「多一条推文」
降为「状态改错（还能改回来）」。

---

## 7. 失败 / 未知分类

| X 的实际结果 | 落库 | 自动重试 |
|---|---|---|
| 4xx（含 402 / 429） | `failed`，Run 还原 `approved`/`edited` | 允许**显式**重试 |
| `ConnectError` / `ConnectTimeout` | 同上（请求没送达） | 允许**显式**重试 |
| 5xx | `publish_unknown` | ❌ |
| 读超时 / 连接中断 | `publish_unknown` | ❌ |
| 2xx 但无 `data.id` | `publish_unknown` | ❌ |
| 未识别的 `XApiError` | `publish_unknown`（兜底方向永远是 unknown） | ❌ |
| 进程崩溃 / 被 kill | 残留 `publishing`（`claim_publish` 会拒绝后续发布） | ❌ |

`XApiError` 全分支在 `PublishService._call_x` 内被吞掉，
**绝不走 `_fail_run` / `_failure_response`** —— 那两个会把已审核通过的 Run 打成 `failed`。

---

## 8. 测试与变异验证

**157 passed**（Phase 4a 基线 154）。全部走真实 `xagent_test` 库；X 走 `httpx.MockTransport`。

2026-09-23 新增 3 项，围绕实机暴露的诊断缺陷（见第 10 节）：
`test_uninformative_transport_error_reports_its_cause`、
`test_cause_chain_is_redacted_too`、`test_cause_chain_walk_is_bounded`。
前两项在修复前**确实失败**（实测），断言的是行为而不是实现。

**幂等断言的正确方式是调用计数，不是 HTTP 状态**：mock X 客户端记录 `call_count`，
每个幂等测试断言 `x.call_count == 1`（或 `== 0`）。
只断言返回 200 的测试是恒真的。

### 变异验证 6/6 命中

| # | 变异 | 失败测试数 |
|---|---|---|
| M1 | `claim_publish` 恒返回 `True` | 4 |
| M2 | 去掉 run CAS 的 `rowcount != 1` 检查 | 1 |
| M3 | 去掉 run ↔ review 交叉校验 | 1 |
| M4 | `finished_at` 恢复旧语义（approve 即写） | 1 |
| M5 | 发布失败不回退 Run | 2 |
| M6 | `note_publish_unknown` 不改 Run 状态 | 2 |

---

## 9. 剩余风险与后续项（不影响 V1 正确性，V1 明确接受）

1. **崩溃窗口内 exactly-once 不可实现** —— 见第 5 节。原语层面的限制，非实现缺陷。
2. **`publish_unknown` / `publishing` 的残留需要人工上 X 看过之后裁决，
   V1 没有告警机制**。刻意的非目标（不引入新基础设施），但上线前应知道
   「没人看就会一直挂着」。
3. **`published_post` 的 FK 仍是 `ON DELETE CASCADE`** —— 删 Run 会连带删掉发布证据。
   当前无删除接口，风险 0；将来加清理任务必须先处理这里。
4. **重试走 UPDATE 故 `created_at` 不刷新** —— 重试过的行「卡了多久」不可判断。
5. **`body` 与 `draft_id` 可能内容不一致**（edit 路径下 body 是人工改写稿）——
   这一行不能自证来源。已在 `PublishRecord` 里同时返回两者；
   完整审计链（引入 `review_id` 关联或 attempt 表）留后续。
6. `_revert_run_to_review_outcome` 中 `target is None` 分支**不可达**
   （preflight 已保证 `review.status ∈ {approve, edit}`）。真发生时 `logger.error`
   并**什么都不做** —— Run 停在发布态被 409 挡住，而不是猜一个状态放行一次本不该发生的发布。
7. Phase 3 遗留：`tests/test_review_api.py:14` 的 `draft_json` 是未使用的 import —— **已于 commit `3e49aa9` 清理**。

### 2026-09-23 实机验证新增的已知限制

以下都**不是缺陷**，是设计边界。记录在此以免将来被误当成 bug 去「修」：

8. **本地库是发布审计记录，不是 X 当前内容的实时镜像。**
   系统没有、也不该有 X 侧回读同步：知道「我们做过什么」和知道「X 上现在有什么」
   是两个问题，只解决前者。
9. **已发布后被人工删除的帖子，本地仍保持 `published`。** 实测发生过：
   推文 `2102660514166005810` 在 X 上被人工删除，`published_post` 行
   `f17ea6ec-…` 仍是 `status='published'`。代码里**没有任何删除路径**能把这条记录
   改成别的值 —— `resolve` 的两种操作（`mark_published` / `retry`）都不适用于
   「已发布后又删除」。
10. **被删除的 X 内容仍可能参与历史去重。** `list_published_contents()` 的条件是
    `WHERE x_post_id IS NOT NULL`（第 4 节不变量 5），不检查内容是否还在线上。
    因此上面那条已删除推文的正文**仍在 Guardrail 判重池里**。
11. **中文 forbidden terms 检查不完整。** `guardrail.FORBIDDEN_TERMS` 目前全是英文，
    对中文内容**永远命中不了**任何一条 —— 中文语境下这项检查等于空转。
12. **本地 `len()` 不是 X 的精确字符权重计数器。** X 对 CJK 与 emoji 按 **2** 计权、
    拉丁字母与标点按 1 计。实测一条 93 字的中文草稿，本地 `len()=93`，
    X 侧权重 **144/280** —— 两者不同。
    当前靠 `persona.max_length: 140` 兜住：因为 `权重 ≤ 2 × len` 恒成立，
    140 字的中文最多折算 280 权重，**可证明不会超**。
    但这是余量换来的安全，不是精确计数；真要放开长度必须实现权重计数器。

---

## 10. Phase 4b 前置条件

**原阻塞项（402 credits / 写权限无证据）已于 2026-09-23 解除。**

### 10.1 实机验证结果

| 项 | 结论 |
|---|---|
| 真实 `POST /2/tweets` | **已执行 2 次，均成功** |
| OAuth 1.0a 写权限 | **已真实验证**（此前只有读端点证据，写权限零证据） |
| X credits | **已可完成真实写操作**，402 未复现 |
| 落库 | 2 条 `published_post`，`status='published'`，`x_post_id` 均非空 |
| 幂等 | 失败行经条件 UPDATE **被复用**，未产生第二行（实测） |

两次真实发布：

| Run | topic | 推文 ID | 时间 (UTC) |
|---|---|---|---|
| `6b9f62c9-…` | AI coding agents | `2102660514166005810` | 07:25:07 |
| `1c5db96b-…` | the future of AI | `2102664104339226711` | 07:39:23 |

第一条的 `published_post` 行 `created_at=06:49:01`、`updated_at=07:25:07` ——
**36 分钟的时差就是重试复用路径的实证**：第一次尝试失败留下的行被条件 UPDATE
复用，行数始终为 1。

中文内容另做了一次 X 字符权重验证：本地 `len()` 与 X 实际权重**不同**（见第 9 节第 12 条）。

### 10.2 实机暴露并已修复的缺陷

`PublishService._call_x` 原先用 `redact(str(exc))` 取诊断，但 httpx 在连接失败时抛的是
`ConnectError("")` —— 真正的原因在 `__cause__` 里。实机落库的记录是：

```
X API request could not be sent:
```

冒号后什么都没有，而这是真实发布失败时**唯一**能拿到的线索。

已修（commit `7864749`）：`_describe()` 沿 `__cause__` / `__context__` 取信息
（有深度上限、防循环），`str()` 为空时退化成类名，拼好的结果**整体过 `redact()`**。
错误分类逐字未动，四个状态、`may_have_reached_x`、4xx/5xx 分支的语义全部保持。

### 10.3 进入 Phase 4b 前的建议

1. 冒烟已通过，无需重复；再发之前先确认 X 端实际内容与本地记录一致。
2. 手工删除过推文后，本地行不会跟着变（第 9 节第 9、10 条）。
3. 失败即停手，先查 X 端，**绝不自动重试**。

---

## 11. 运维备忘（Windows）

```bash
docker compose up -d          # 必须先起 Postgres，否则整个 suite 要跑 4~5 分钟才报 22 errors
PYTHONPATH="E:/2026_agent/X_Agent;E:/2026_agent/X_Agent/tests" \
  .venv/Scripts/python.exe -m pytest -q
```

- 应用必须用 `python run.py` 启动（uvicorn 0.36+ 不读 event loop policy，
  win32 会选 ProactorEventLoop，psycopg 异步连接全部失败）。
- `alembic revision --autogenerate` 每次都会把 LangGraph 的 4 张 checkpoint 表
  （`checkpoints` / `checkpoint_writes` / `checkpoint_blobs` / `checkpoint_migrations`）
  误判为「已删除」并生成 `drop_table` —— **必须手工剔掉**，否则 migrate 会删光所有会话断点。
  本次 `updated_at` 的迁移干脆用 `alembic revision -m` 手写，完全绕开 autogenerate。
- `tests/conftest.py` 除补列外还**必须用 `pg_constraint` 探测补 UNIQUE 约束**
  （ADD CONSTRAINT 没有 IF NOT EXISTS）。

---

## 附：本阶段文件清单

**新增**：`app/services/publish.py`、`app/services/x_oauth.py`、`app/redaction.py`、
`alembic/versions/0b17346696c6_phase4_publish_state.py`、
`alembic/versions/c8e3f54d2a56_publish_updated_at.py`、
`tests/test_publish_{state,service,api}.py`、`tests/test_x_oauth.py`

**修改**：`app/repositories.py`（CAS 核心）、`app/services/x_api.py`（`create_post` + OAuth 头）、
`app/main.py`、`app/schemas.py`、`app/models/published_post.py`、`app/models/mixins.py`、
`app/config.py`、`app/services/x_service.py`、`.env.example`、`pyproject.toml`、
`tests/{conftest,fakes,support,test_guardrail,test_review_api,test_x_api_client}.py`

**明确未动**：`app/agent/**`、`app/db/checkpointer.py` —— Phase 3 Graph 一行未改。
