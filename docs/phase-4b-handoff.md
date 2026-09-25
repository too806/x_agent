# Phase 4b 交接文档：运营控制台

> 生成于 2026-09-25。**4b.1（中文化 + 产品验收 + 小问题收口）后已更新。**
> 基线：Phase 4a 封版于 `fd5f191`（157 tests 绿），本阶段在其之上，**后端核心业务一行未改**。
> 测试基线：**186 passed**（4a 的 157 项原样通过）。
> 前端产物：`npm run build` 通过（tsc + vite）。

本阶段分两个子阶段：

| 子阶段 | 内容 | 后端改动 |
|---|---|---|
| **4b** | 只读 Admin API + 运营控制台五个页面（英文界面） | 新增 `app/api/admin.py`、`app/services/admin.py` |
| **4b.1** | 界面全中文化、审核/发布路径的可用性收口、文档 | **只有一处注释**（`app/models/review.py`），见第 11 节 |

---

## 1. 本阶段做了什么

目标是消灭两类"必须绕路"的操作：

> 让我不需要 Swagger、不需要直接查询 PostgreSQL，就能管理 Run、审核内容、处理 Publish、
> 查看历史和分析系统运行情况。

4b.1 之后追加的一条验收标准：

> 打开 `127.0.0.1:5173`，可以完全用中文完成一次 Run → Review → Edit/Reject → 查看结果，
> 不需要 Swagger，也不需要直接查数据库。

交付形态是一个跑在本机的 React 单页控制台，覆盖五个页面：

| 页面 | 路径 | 解决什么 |
|---|---|---|
| 系统总览 | `/` | 系统快照：KPI、日趋势、两个**待办队列** |
| 运行记录 | `/runs` | 全部 Run 的检索与分页，右侧抽屉看单条全生命周期 |
| 内容审核 | `/review` | 审核工作台：左队列右材料，连续决策不跳页 |
| 发布记录 | `/published` | 发布记录（四态全在）+ 发布入口 + 待裁决处理 |
| 数据分析 | `/analytics` | 系统自身运行分析（**不含** X 内容表现） |

在此之前，`/api/admin/*` 只存在于设计里：控制台能看到的每一个状态，要么走 Swagger，
要么手写 SQL。

---

## 2. 已锁定的设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **Admin API 只读**，一个写端点都没有 | 见第 4 节不变量 1。前端复用既有 `POST /api/runs/{id}/review`、`/publish`、`/publish/resolve`。两套写路径一定会漂移，而漂移的那一套多半会漏掉 Phase 4a 用条件 UPDATE 换来的并发保护。 |
| D2 | **前端不碰数据库，也不碰 SQL** | 前端只发 HTTP；后端所有查询在 `app/repositories.py`，聚合在 `app/services/admin.py`，`app/api/admin.py` 里没有一行 SQL。 |
| D3 | **控制台不新增任何返回凭据的接口** | 前端构建产物里没有 Bearer / OAuth / LLM key / proxy 凭据（已扫，见第 8 节）。 |
| D4 | **`unresolved` 在前端没有任何自动路径** | `publishing` / `publish_unknown` 下界面只给人两个动作，且都不发帖。 |
| D5 | **这是审计记录，不是 X 当前状态的镜像** | 因此**没有删除按钮，也没有"标记删除"**。人工在 X 上删了推文，本地行不变，也不该变。 |
| D6 | **算不出来的指标标"暂不可用"，不返回 0** | Analytics 里 X 侧六项指标既不给数也不给 0；平均耗时的样本覆盖如实报"取自 N 个 Run，共 M 个"。 |
| D7 | **每个数字自带口径** | 窗口内 vs 全量、per Run vs per attempt、本地 `len()` vs X 加权字符数，全都在界面上写明。 | 
| ~~D7-old~~ | ~~口径说明由后端随数据下发，前端不自己编~~ | **4b.1 推翻了这一条**：后端下发的口径文案是英文常量且被 `tests/test_admin_api.py` 断言着，中文化时不能改后端。改为**前端按同一份语义用中文重写**，后端常量原样保留（见第 11 节）。代价是同一套口径有两份表述，改口径要改两处。 |

> D7-old 之所以单独留一行划掉、而不是删掉：它是 4b 当时的真实决策，删掉会让"为什么
> Analytics 里那段说明是前端写的"变成一个查不到出处的结果。

---

## 3. 边界与数据流

```
浏览器
  │  只发同源相对路径 /api/...  （无 CORS、无后端地址、无凭据）
  ▼
Vite dev server :5173  ──proxy /api──►  FastAPI :8000
                                            │
                              app/api/admin.py      仅 GET，无 SQL
                                            ▼
                              app/services/admin.py 聚合 + 口径文案
                                            ▼
                              app/repositories.py   全部 SQL
                                            ▼
                                      PostgreSQL
                                            ▲
                                            │ 只有一条路径读它：
                              GET /api/admin/runs/{id}  →  LangGraph checkpoint
                                                          （guardrail / human_action / final_content）
```

**checkpoint 是唯一"可能读不到"的来源**，所以它单独走一条路径，而且读不到时返回
`null` 而不是近似值——前端渲染成 `—` 并说明"这表示未知，不表示通过"。

---

## 4. 不变量（改动这些必须先把对应测试弄红）

1. **`/api/admin/*` 只有 GET。** 由 `test_admin_api_exposes_no_write_endpoints` 直接从
   `app.openapi()` 读路由表断言。它守的是架构边界而不是功能：将来想给控制台加写端点，
   这条会先红。
2. **KPI 的发布四项取自 `published_post`，不取自 `agent_run.status`。**
   Run 可以停在 `approved` 而它的发布记录是 `failed` —— 后者才是事实。
   测试：`test_overview_kpis_come_from_published_post_not_run_status`。
3. **没有发布记录就是 `null`，不得用 Run 状态顶替。**
   测试：`test_overview_does_not_invent_publish_status`。
4. **`unresolved` 与 `status` 取交集，不互相覆盖。** 传 `status=published&unresolved=true`
   得到空页，而不是让其中一个悄悄失效。测试：`test_published_unresolved_filter_intersects_with_status`。
5. **待裁决队列排序按紧急度，不按时间。** `publish_unknown` 在 `publishing` 之前——
   前者"可能已经发出去了却没人知道"，是唯一会随时间变得更糟的状态。
   测试：`test_published_unresolved_filter_orders_by_urgency`（变异验证见第 7 节）。
6. **发布记录列表的日期筛选走 `created_at`，不是 `published_at`。** 后者只有成功的行才有值，
   用它筛会让 `failed` / `unknown` 既不在窗口内也不在窗口外。左闭右开。
   测试：`test_published_date_range_is_half_open_and_intersects_with_unresolved`。
7. **编辑稿字数上限由后端下发**（`ReviewQueuePage.content_max_length`），前端不写死 140。
   它同时是发布前的硬校验，抄一份到界面就会在配置改动后开始说谎。
   测试：`test_review_queue_reports_the_runtime_max_length`（用 137 这个配置里不存在的值）。
8. **审核决定的 `other` 桶必须吸收未知状态**，四项之和恒等于审核记录总数。
   测试：`test_analytics_review_other_bucket_absorbs_unknown_statuses`（变异验证见第 7 节）。

---

## 5. 读写路径

### 只读（本阶段新增，全部 GET）

| 端点 | 用途 |
|---|---|
| `GET /api/admin/overview?days=` | 首页一次性拿全：KPI + 两条趋势 + 两个待办队列 |
| `GET /api/admin/runs?status=&topic=&date_from=&date_to=&page=&page_size=` | Run 列表，服务端分页 |
| `GET /api/admin/runs/{run_id}` | 单条全生命周期（含 checkpoint 只读部分） |
| `GET /api/admin/reviews?page=&page_size=` | 待审队列 + `content_max_length` |
| `GET /api/admin/published?status=&unresolved=&date_from=&date_to=&page=&page_size=` | 发布记录 |
| `GET /api/admin/analytics?days=` | 系统运行分析 |

`days` 一律 `ge=1, le=365`；`page_size` 一律 `ge=1, le=100`。

### 写入（**全部复用 4a 既有端点，没有新增**）

| 界面动作 | 端点 | 确认强度 |
|---|---|---|
| 批准 | `POST /api/runs/{id}/review` | 一次确认框 |
| 编辑 | 同上，`{"action":"edit","content":...}` | 计数超限时提交按钮禁用 |
| 拒绝 | 同上，必须有理由 | 理由必填，未填时提示"请输入拒绝原因" |
| 发布（首次 / failed 后重发） | `POST /api/runs/{id}/publish` | **一个**确认勾选 + 明确写出"确定要发布到真实 X 账号吗？" |
| 裁决"标记为已发布" | `POST /api/runs/{id}/publish/resolve` `mark_published` | 必填 x_post_id（人从 X 抄回） |
| 裁决"确认可能重复后重试" | 同上，`retry` + `confirm_maybe_duplicate: true` | **两个**确认勾选 |

发布确认框的三句话是 4b.1 按验收要求逐字写定的，**不要为了界面简洁压掉**：
"确定要发布到真实 X 账号吗？"、"该操作会创建一条公开帖子。"、
"它会立刻生效，并且无法从这个控制台撤销"。

最后一行是刻意加重的一道门：它是系统里唯一能把"可能已经存在的推文"变成"可以再发一次"的
操作，勾错一个就是重复推文。而且它**只改状态**，真要重发必须再按一次发布按钮。

### 前端零凭据（已扫）

`frontend/src` 全文搜索 `bearer|oauth|token|secret|api_key|password|proxy|127.0.0.1|localhost|http://`
只命中两处：`main.tsx` 的 `import './styles/tokens.css'`，以及 `client.ts` 里用
`x_post_id` 拼出的公开链接 `https://x.com/i/web/status/<id>`（派生值，**不存库**）。

---

## 6. 每个页面必须处理的三种状态

五页都实现了 Loading / Empty / Error，数据区域**不会白屏**。空态是分开写的，因为"空"有
不同的含义：

| 场景 | 文案的区别（4b.1 后的中文） |
|---|---|
| 待审队列空 | "没有待审核的内容" —— 这是好状态 |
| 待裁决队列空 | "没有需要裁决的发布" —— 这是好状态 |
| 筛选没匹配上 | "没有符合筛选条件的发布记录" —— 要动的是筛选，不是别人 |
| Run 从未发布过 | "这个 Run 没有发布记录" —— 明确说 **运行状态不能读作发布结果** |
| guardrail 读不到 | `—` + "这不是通过" / "这**不等于**通过" |
| 指标算不出 | "暂不可用" + 缺数据的原因 |

---

## 7. 测试与变异验证

**186 passed**（4a 基线 157）。全部走真实 `xagent_test` 库；X 侧走 mock。
4b.1 之后重跑仍是 **186 passed**——这轮**没有新增后端测试**，也**没有修改任何测试**，
因为后端唯一改动是一行注释（第 11.3 节第 10 项）。

本阶段新增 29 项，全部在 `tests/test_admin_api.py`（该文件共 29 项）；`tests/support.py`
里另有一处构造签名同步（`Runtime(..., persona=...)`），不是测试。

前端没有测试框架（见第 8 节第 7 项），所以 UI 改动**不可能**被后端测试覆盖到——
`pytest` 全绿只证明后端没被碰坏，不证明界面是对的。界面的验证手段只有
`tsc --noEmit` + `vite build` + 人工验收。

### 变异验证 3/3 命中

| # | 变异 | 结果 |
|---|---|---|
| M1 | 删掉待裁决队列的紧急度排序，退回 `created_at.desc()` | **测试红**（`['publishing',…] != ['publish_unknown',…]`） |
| M2 | `other` 桶恒为 0 | **测试红**（`other` 期望 2 实得 0） |
| M3 | `published` 日期右端改成闭区间（`<=`） | **测试红**（右端点上的行被放进窗口） |

三条测试都刻意构造成"两种可能的实现给出不同结果"，否则删掉被测代码它也会通过。

> M1 的构造值得记一笔：最初把 `publishing` 造在前、`publish_unknown` 造在后，于是
> "按时间倒序"与"按紧急度"恰好一致，测试是恒真的。把创建顺序反过来才有区分度。

---

## 8. 已记录、本阶段**未修**的发现

按"发现的非必要问题先记录，不要顺手修复"处理：

1. **research item 没有有意义的顺序。** `list_research_items` 用
   `ORDER BY created_at, id`，而同一事务内插入的行 `created_at` 完全相同、`id` 是
   `uuid.uuid4` 随机值 → 两次打开同一个 Run，research 顺序可能不一样。
   只影响展示顺序，不影响内容。（同一个 Run 详情测试曾因此间歇性失败，已改为按
   `platform_post_id` 查找，而不是依赖位置。）
2. **`AdminErrorResponse`（`app/schemas.py:453`）定义了但没有任何路由使用** —— 死代码。
3. **`DURATION_PARTIAL_REASON`（`app/services/admin.py:130`）定义了但未使用** —— 死代码。
   部分覆盖的说明改由前端用 `sampled_runs` / `total_runs` 两个数字自行表述，
   没有新增后端字段。
4. ~~`app/models/review.py:21` 的注释与实现不符~~ → **4b.1 已修**（只改注释，见第 11 节）。
5. **前端产物 614 kB / 175 kB gzip**，Vite 报 chunk 超过 500 kB。Recharts 是大头。
   没有做代码分割（本阶段没有引入额外的打包配置）。
6. **筛选条件变化时会多发一次请求。** `useAsync` 的依赖变化与 `setPage(1)` 不在同一个
   render 里生效，于是会先按"旧页码 + 新筛选"发一次。由 generation guard 丢弃，
   无用户可见影响，只是多一次往返。
7. **前端没有测试框架。** 前端验证 = `tsc --noEmit` + `vite build` + 与真实 API 契约的
   人工核对（逐字段对照线上响应）。引入 vitest 属于本阶段明确没做的事。
8. **侧栏待办计数与首页各请求一次 overview。** 计数放在 context 里跨页共享，但
   系统总览页面自己还会再请求一次（窗口可能不同）。属可接受的重复。
9. **运维现状：机器上有两个 `run.py` 进程，只有一个在监听。**
   `127.0.0.1:8000` 的属主是 PID 20524（`E:\ItInstallationPath\Python\python.exe run.py`）；
   另有一个 PID 19632（项目 `.venv` 的 python）活着但没有端口。
   重启前先按端口确认属主，别按名字杀。

---

## 9. 启动方式与访问地址（Windows）

```bash
# 1) Postgres
docker compose up -d

# 2) 后端（必须在仓库根目录）
HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 \
  .venv/Scripts/python.exe run.py
#   → http://127.0.0.1:8000        （/docs 是 Swagger，控制台不再需要它）

# 3) 前端（开发模式，带 HMR）
cd frontend && npm run dev
#   → http://127.0.0.1:5173        ← 浏览器开这个

# 冒烟
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:5173/api/admin/overview?days=7
```

**浏览器访问地址：<http://127.0.0.1:5173>**

要点：

- **`HTTPS_PROXY` 是必须的**：审核后的发布要访问 x.com，不设代理会失败。
  只读页面不需要它。
- 后端必须用 `python run.py` 启动，不要直接 `uvicorn`：uvicorn 0.36+ 不读 event loop
  policy，win32 上会选 ProactorEventLoop，psycopg 异步连接全部失败。
- Vite 配了 `strictPort`，5173 被占用时**直接失败**而不是静默换端口——换端口会让人
  对着一个旧进程调试。
- 生产构建：`cd frontend && npm run build` 产出 `frontend/dist/`，把 `dist` 交给任意
  反代即可，前端代码里没有任何需要跟着改的后端地址。

跑测试：

```bash
PYTHONPATH="E:/2026_agent/X_Agent;E:/2026_agent/X_Agent/tests" \
  .venv/Scripts/python.exe -m pytest -q
```

---

## 10. 未实现 / 明确的非目标

**本阶段没做（按需求刻意不做）：**

- ❌ 认证 / 多用户 / RBAC —— 控制台只监听 127.0.0.1，本机单人使用
- ❌ 删除 / 标记删除 —— 审计记录语义（D5）
- ❌ X 侧的 impressions / likes / replies / reposts / bookmarks / engagement rate
- ❌ 自动重试、X 回读同步、已删除推文的同步
- ❌ 实时推送（无 WebSocket / SSE）—— 刷新靠按钮，写操作后自动重取当前视图
- ❌ Redux / Zustand / Next.js / 组件库 / Tailwind —— 只用 React + 原生 fetch
- ❌ CSV / 报表导出
- ❌ 告警机制（4a 遗留：`publishing` / `publish_unknown` 挂着不会主动通知）

**已知的功能边界：**

- Overview 的两个队列各只预览 10 条（`OVERVIEW_QUEUE_PREVIEW`），完整列表在各自页面。
- 一次请求最多为 50 条待审 Run 读 checkpoint 拿 guardrail 结论
  （`GUARDRAIL_READ_LIMIT`）；超出部分一律报 `—`，**不猜值**。
- 列表里的正文预览截断到 200 字符（`PREVIEW_CHARS`），完整正文在详情抽屉里。

---

## 11. 4b.1：中文化

### 11.1 中文状态映射（唯一词典）

**后端的状态值一个都没改，仍然是英文枚举。** 中文只存在于展示层：`frontend/src/utils/format.ts`
的四张映射表 + `StatusBadge`。界面上的每一个状态徽标都从这里取词，所以同一个状态不可能
出现两种译法。

三个枚举**分开映射**，因为 `agent_run.status` 与 `published_post.status` 有四个**同名**的值
而含义不同：

| 后端值 | 运行状态（`agent_run`） | 审核决定（`review`） | 发布状态（`published_post`） |
|---|---|---|---|
| `running` | 运行中 | — | — |
| `waiting_for_review` | 待审核 | — | — |
| `approved` | 已批准 | — | — |
| `edited` | 已编辑 | — | — |
| `rejected` | 已拒绝 | — | — |
| `failed` | 失败 | — | **发布失败** |
| `completed` | 已完成 | — | — |
| `publishing` | 发布中 | — | 发布中 |
| `publish_unknown` | 发布结果未知 | — | 发布结果未知 |
| `published` | 已发布 | — | 已发布 |
| `approve` | — | 批准 | — |
| `edit` | — | 编辑 | — |
| `reject` | — | 拒绝 | — |
| `pending` | — | 待审核 | — |
| `other` | — | 其他 | — |

`failed` 一行是这套表存在的理由：运行状态下的"失败"和发布状态下的"发布失败"是两个不同的
事实，同名不同义。`StatusBadge` 因此把 `kind` 做成**必填**参数，调用方必须说明自己在显示哪一个。

`completed` 不在 4b.1 指定的词典里，但它是真实的 Run 状态（`types/api.ts` 的 `RUN_STATUSES` 有它），
不补就会在筛选下拉里冒出一个英文项，所以补成"已完成"。

**查不到时的行为**：原样返回大写原文（`status.toUpperCase()`），不返回"未知"。
一个没翻译的英文枚举是**看得见的缺口**，一句"未知"会把缺口藏起来。

### 11.2 界面文案

| 区域 | 中文 |
|---|---|
| 导航 | 系统总览 / 运行记录 / 内容审核 / 发布记录 / 数据分析 |
| KPI | 总运行数 / 待审核 / 已发布 / 发布失败 / 发布结果未知 / 发布中 |
| 按钮 | 查看 / 批准 / 编辑 / 拒绝 / 发布 / 重试 / 裁决 / 取消 / 保存 / 刷新 |
| 抽屉字段 | 运行状态 / 审核决定 / 发布状态 / 运行时长 / 创建时间 / 开始时间 / 完成时间 / Run ID |
| 发布记录列 | 主题 / 发布内容 / 发布状态 / X 帖子 ID / 发布时间 / 创建时间 / 更新时间 / 最后错误 |

**刻意保留英文的**（都是技术标识，翻译反而让人对不上后端和日志）：`X`、`API`、`Post ID`、
`Run ID`、`LLM`、`HTTP`、`outcome`、`checkpoint`、`LangGraph`、`GET /api/...` 路径、
`started_at` / `finished_at` / `published_at` 等字段名、以及 `<code>` 里的内容。

### 11.3 这轮改了什么（除了换词）

| # | 改动 | 为什么 |
|---|---|---|
| 1 | `utils/format.ts` 收口全部状态文案 | 约 20 处徽标共用一个词典，译法不会漂 |
| 2 | 运行记录 / 发布记录的状态筛选下拉改为显示中文、**值仍是后端枚举** | 原先直接渲染原始枚举（下拉里是 `waiting_for_review`） |
| 3 | 审核成功后按决定给不同提示 | 原先只有一句通用的；编辑的人要知道改稿存下来了，拒绝的人要知道这条不会再发 |
| 4 | 编辑模式显示**原始内容 / 编辑后的内容 / `93 / 140 字`** | 原先只有编辑框，看不到改了什么。计数标注"不是 X 的加权字数" |
| 5 | 拒绝理由为空时按钮可点、点了提示"请输入拒绝原因" | 原先按钮禁用，人只能盯着它猜为什么点不动 |
| 6 | 发布确认框写出"确定要发布到真实 X 账号吗？""该操作会创建一条公开帖子。" | 验收要求逐字写定，见第 5 节 |
| 7 | `publish_unknown` 的裁决区明确写"系统无法确认 X 是否已经创建帖子。自动重试可能导致重复发布。" | 这是唯一会随时间变得更糟的状态，不能用普通的失败样式糊过去 |
| 8 | 发布记录页加 `查看运行` 按钮（打开详情抽屉）与 `创建时间` 列 | 字段本来就下发，只是没显示；从记录回到 Run 原先没有入口 |
| 9 | `RunDetailDrawer` 增加可选的 `onSettled`，发布记录页与运行记录页传入自己的 `reload` | 抽屉里发布/裁决成功后，背后那张表原本是旧的 |
| 10 | `app/models/review.py:21` 注释修正 | 注释写 `approved / edited / rejected`，实现写的是 `approve / edit / reject`。**只改注释** |

### 11.4 明确没有做的事

- **没有为中文改任何后端字段名、状态值或英文常量。** Analytics 的三段口径说明
  （`PUBLISH_OUTCOME_NOTE` / `DURATION_UNAVAILABLE_REASON` / `X_PERFORMANCE_UNAVAILABLE_REASON`）
  被 `tests/test_admin_api.py` 的三条断言锁着，后端保持原样；中文版由前端按同一语义写。
- **没有为验证发布而额外发帖。** `publish_unknown` 也没有被人为制造出来。
- **没有引入 i18n 框架。** 单一语言、单一词典文件，`react-i18next` 那一层在这个规模上
  只是多一层间接。

---

## 附：本阶段文件清单

**后端新增**：`app/api/admin.py`、`app/services/admin.py`、`tests/test_admin_api.py`

**后端修改**：`app/main.py`（`Runtime` 增加 `persona` 字段并挂载路由）、
`app/schemas.py`、`app/repositories.py`（新增只读查询与过滤子句，**未改任何写路径的语义**）、
`tests/support.py`

**后端明确未动**：`app/agent/**`、`app/services/publish.py`、`app/services/x_*.py`、
`app/db/checkpointer.py`、`app/services/publish.py` 的四个状态与错误分类、
Guardrail 规则、Research、Generate。

**前端新增**：`frontend/` 整个目录（Vite + React + TS，见第 3 节结构），
其中 `src/pages/*` 五页、`src/components/*` 十个组件、`src/hooks/*` 六个 hook、
`src/api/client.ts`、`src/types/api.ts`、`src/utils/format.ts`、`src/styles/*`。

**按子阶段提交**（每个阶段一个 commit，未 squash）：

| commit | 子阶段 |
|---|---|
| `76ad021` | 4b-1 只读 Admin API + 前端骨架 |
| `157357c` | 4b-1 控制台外壳 |
| `1e104d5` | 4b-2 Overview + Runs + 详情抽屉 |
| `6f6ffac` | 4b-3 审核工作台 |
| `c7ef59b` | 4b-4 发布记录页 + 发布/裁决 |
| `522c8ad` | 4b-5 分析页 |
| `feat: localize operations console` | 4b.1 全中文化 + 可用性收口 + 注释修正 |
| `docs: add phase-4b ux review` | 4b.1 UX 走查文档 + 本文档更新 |
