# Phase 4a 交接文档：Publish 状态机

> 生成于 2026-09-21。Phase 3 封版于 `530c0e6`，Phase 4a 在其之上。
> 测试基线：154 passed（Phase 3 的 62 项原样通过）。**X 全部 mock，未调用过任何真实 X 写接口。**

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

**154 passed**（基线 144）。全部走真实 `xagent_test` 库；X 走 `httpx.MockTransport`。

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

---

## 10. Phase 4b 前置条件

**可以进入，但有一个阻塞项。**

**阻塞**：X 账号仍 **402 credits depleted**（2026-09-20 实测）。
且 402 是在**读**端点上观察到的，因此它**完全没有为写权限提供任何证据** ——
OAuth 1.0a 凭据是否真能发帖，**至今未经验证**。

**建议顺序**：

1. 充值 / 确认 credits
2. 一次性冒烟：`POST /api/runs/{id}/publish` **只调一次**
   → 查库确认 `x_post_id` → `GET /api/runs/{id}` 确认状态
   → 人工到 X 确认**只有一条**推文
3. 失败即停手，先查 X 端，**绝不自动重试**

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
