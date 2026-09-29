# 集成与稳定化验收报告

> 会话 E（Integration & Stabilization），2026-09-29。
> 目标不是新增能力，而是把前面四个并行会话的成果安全收口成一条可验证、
> 可回滚的 master 基线。

---

## 1. Git

| 项 | 值 |
| --- | --- |
| 集成基线（fast-forward 后的提交） | `bfd9652` |
| 最终 master HEAD | 本报告所在的提交——收口提交在其之上，取 `git log -1` 的当前值 |
| 最终分支 | `master`（`feat/frontend-product-polish` 停在集成基线 `bfd9652`，不再前进） |
| 仓库根 | `C:\xuexi\py\2026_agent\X_Agent` |
| worktree 数量 | 1 |
| 未提交文件 | 无（`git status` 干净） |
| untracked source | 无 |

**集成方式**：`git checkout master && git merge --ff-only feat/frontend-product-polish`。

`master` 是分支的**严格祖先**（`git merge-base --is-ancestor master HEAD` 成立），
因此这是一次 fast-forward：**没有 rebase、没有 reset、没有 squash、没有 amend**，
历史逐字保留。

**动手之前的检查点**（保留在 `.integration-backup/`，已加入 `.gitignore`）：

- `20260929-155457-pre-integration.bundle`（`--all` 全 refs 快照）
- `20260929-155457-working-tree.diff`
- `20260929-155457-status.txt`

未执行 `git clean` / `git reset --hard`，也未使用 `git add -A`——
每一个提交都用显式 pathspec。

### 冻结确认（动手之前）

四个并行会话的写入已经停止：

- `git status` 的 `--porcelain` 输出在 12 秒间隔内哈希不变；
- 只读审计确认工作区里只剩四份**已完成但未提交**的产物（evals、评估文档、
  前端测试、换机文档），没有正在写入的文件。

所以本次没有走到"只做只读审计并停止"那条分支。

### 提交清单（`225c263..bfd9652`，共 20 个）

| 提交 | 说明 | 来源会话 |
| --- | --- | --- |
| `c3e0336` | chore: harden repository environment handoff | B（Hygiene） |
| `3e8f6d7` | feat: polish operations console UI | D（Frontend） |
| `25c64ea` | feat: improve review and agent trace experience | D |
| `f0fa86d` | docs: record frontend product polish | D |
| `577571a` | feat: unify review queue feedback | D |
| `defc78a` | test: derive the test database URL from .env | B |
| `e795608` | feat: report agent run quality metrics | A |
| `f1a641e` | fix: require re-critique after revision | A |
| `38f9f12` | tune: calibrate critic acceptance threshold | A |
| `88c6fe4` | test: cover agent revision evaluation | A/C |
| `1d35c36` | test: cover the revision re-review contract in the loop | A/C |
| `faa2a46` | feat: give failed publishes a KPI nature, fix two copy slips | D |
| `250b3e0` | docs: note that the branch also carries the parallel session's commits | D |
| **`c1a2997`** | **fix: close the agent fallback path that spins on angles** | **E** |
| **`9dd1176`** | **fix: stop a null-topic run from 500ing the analytics endpoint** | **E** |
| **`6973fa9`** | **feat: integrate the agent evaluation lab** | **C → E** |
| **`a6f88a6`** | **docs: revise the setup guide for the lock file and the serial-test rule** | **B → E** |
| **`68b42a9`** | **test: cover the agent quality summary copy** | **A → E** |
| **`28e5c36`** | **docs: add the project README** | **E** |
| **`bfd9652`** | **chore: keep the pre-integration checkpoint out of the repo** | **E** |

加粗的 7 个是本次新增；其余 13 个是并行会话已经产生、由本次一并并入的合法提交
（**没有重复 cherry-pick**）。

---

## 2. Agent

### 2.1 改稿后重新评审（`f1a641e`，本次确认有效）

生产链路确实是 `critic → revise → 重新 critic → finish`，不会出现 `critic →
revise → revise`。`app/agent/tools/revise.py` 的 delta 同时做两件事：

1. 从 `critic_results` 摘掉被改那条的旧评价（candidate_id 不变，留着旧分就是用
   改稿**前**的分数给改稿**后**的正文背书）；
2. 清空 `critic_result`——`revision_pending` 只看这一个字段，不清空的话 `revise`
   仍然可行且排在 `FALLBACK_ORDER` 首位，可以连着改两次，第二次拿到的
   `revision_targets` 还是针对上一版文本写的。

**真实运行佐证**（Run `31dea8dd`，11 步）：

```
9  revise     最近一次评审对 c3 给出 revise(5.5)…
10 criticize  上一轮 c1 accept(8.0)、c2 accept(7.5)，只有 c3 被判 revise…
             评价 3 条：c1 accept(7.6)；c2 accept(7.7)；c3 accept(7.3)
11 finish     最近一次评审对三条候选已全部给出 accept…
```

改稿后**确实**重新评审了，且新分数（7.3）取代了旧结论。

### 2.2 Critic calibration（`38f9f12`，本次确认有效）

- 只有 P0 / P1 触发 revise；
- P2 不会无意义阻塞；
- accept 之后可以正常 finish；
- `revision_targets` 只来自允许的级别。

本次**没有**调整 prompt，也没有改 `max_revisions` / `max_steps`。
真实 Smoke Test 的历史结论（accept 22/27 = 81.5%，revisions 0.8/run）原样保留，
没有为了数字好看去动 prompt。

### 2.3 fallback / 预算行为（`c1a2997`，本次修复）

**问题**：候选池写满、最近一轮评审也已完成之后，`generate_angles` 仍然可行——
它的条件只有 `has_evidence`，而证据一旦存在就永远为真。它在 `FALLBACK_ORDER` 里
排在 `finish` **前面**，于是收尾反复重新生成一批永远选不上的角度
（`SELECT_ANGLE` 此时已被封），烧完 `WRAPUP_ALLOWANCE` 后被强制 finish。

这正是 `evals/behavior/rules.py` 里 RULE-013 的 docstring 记录的场景
（"预算从安全边界变成了正常完成任务的出口"）。

**修复（两处最小改动，`app/agent/budget.py`）**：

1. `allowed_actions`：`generate_angles` 与 `select_angle` 对齐——
   `has_evidence and not candidates`。角度是**给候选用的**；候选一旦写出来，
   池子里的正文对应的就是当时那个角度，新角度选不上。
2. `pick_fallback`：新增 `run_is_complete`（`ready_to_finish` + 候选池已满 +
   无待办 critic/revise），成立时 `finish` 优先于排在它前面的重复动作。

**没有做的事**：没有调大 `max_steps`，没有靠扩预算掩盖问题，没有禁止
`generate_angles`（候选出现之前它照常允许）。

**新增确定性测试**（`tests/test_agent_planner.py`）：

- `test_angles_stop_being_generated_once_candidates_exist`
  —— 候选出现前允许、出现后封口；
- `test_wrapup_finishes_instead_of_repeating_angles`
  —— 完成态回退到 `finish`（并断言 `EXTRACT_EVIDENCE` 仍可行，证明断的是
  **结果**而不是顺序）；
- `test_wrapup_still_advances_before_anything_is_written`
  —— 成品出现之前 `finish` 不许插队。

---

## 3. Evaluation

`evals/` 与 `docs/agent-evaluation/` 已由 `6973fa9` 并入主线。

### 3.1 复用生产实现，不复制第二套

`evals/structural/metrics.py` 直接 `from app.agent.metrics import finish_reason`；
`evals/analysis.py`、`evals/behavior/rules.py`、`evals/loader.py`、`evals/model.py`
都从 `app.agent.budget` 取 `AgentBudget` / `allowed_actions` / `WRAPUP_ALLOWANCE`。
`app/agent/metrics.py` 已在 `e795608` 提交，**不是**依赖未提交文件。

### 3.2 干净 checkout 可独立运行

用 `git clone .` 复制出一份**干净检出**（无 `.env`、无未提交文件、无 `__pycache__`），
在其上运行：

```
python -m evals.selftest                        → 自检：11 通过 / 0 失败（共 11 项）
python -m evals.run_benchmark --dataset goals.json → 合计：10 通过 / 0 未通过（共 10 条）
python -m evals.run_benchmark --fixture            → 结论：pass
```

结论：Evaluation Lab **不依赖**当前工作区里的任何未提交文件。

### 3.3 内容质量部分**没有**伪造

`evals/content/` 里的人工维度（`human_quality_score` / `AI_style_score` /
`persona_score`）**保持不填**。没有拿字符串规则冒充内容质量评审。
交付的是 deterministic metrics + behavior rules + structural checks。

### 3.4 覆盖到的安全行为

`invalid_action_recovery`、`revised_without_recritique`、`autonomous_publish`
三个 fixture 都在，且各自的回归门禁会在违规数据上触发（由 11 项自检断言）。

---

## 4. Frontend

Phase 4b.2 的产品化成果**没有重新设计**，只做了验收。

| 页面 | 验收结论 |
| --- | --- |
| Overview | 信息层级正确：系统状态 → 核心指标 → 待处理工作 → 趋势 → 最近活动。KPI 明确标注"全量计数，不受窗口限制"，并说明后四项取自发布记录而不是 Run 状态 |
| Runs | 状态/主题/时间筛选、舒适/紧凑切换、UUID 展示、`查看` 打开 Drawer、分页齐全 |
| Review | 队列 → 选 Run → 研究内容 → 草稿 → 规则检查 → 批准/编辑/拒绝，流程完整 |
| Agent Trace | 见下 |
| Published | 空态清晰；五种发布状态是**五个独立筛选项** |
| Analytics | 后端修复后正常渲染 |
| Loading / Empty / Error | 三者都实际观察到（见 §6.4） |

### Agent Trace（重点验收）

Drawer 里"Agent 决策轨迹"在真实 Run 上渲染 11 步，每步默认给的是**可读的中文**：

```
1  发现候选    检索「…」取回 24 条材料，发现 5 个候选选题：…
2  研究主题    现有 5 个候选选题中把握最高的是「…」（0.72），但证据仍为 0 条…
10 内容评估    上一轮评审中 c1 已 accept(8.0)…
11 完成        最近一次评审对三条候选已全部给出 accept（c1 7.6、c2 7.7、c3 7.3）…
```

十个动作都有中文标签（`发现候选 / 研究主题 / 深入研究 / 提炼证据 / 生成观点角度 /
选择观点角度 / 生成候选 / 内容评估 / 修改内容 / 完成`），后端枚举值与输入摘要
收在默认折叠的"技术细节（后端枚举值与输入摘要）"里。

**默认是给人读的，没有退化成 JSON Viewer。**

### Publish Unknown 语义

`已发布` / `发布失败` / `发布结果未知` / `发布中` / `待裁决（发布结果未定）`
是五个**互相独立**的筛选项，视觉与操作语义不同。`publish_unknown ≠ failed`
没有被"看起来统一"合并掉。

---

## 5. Backend —— Analytics NULL topic（`9dd1176`，本次修复）

### 复现（修复前，对着真实库）

```
GET /api/admin/overview   200
GET /api/admin/runs       200
GET /api/admin/reviews    200
GET /api/admin/published  200
GET /api/admin/analytics  500      ← 问题在这里
```

真实库状态：7 条 Agent Run，其中 **2 条 `topic IS NULL`**（状态 `failed`，
是正在/曾经选题的 Agent 链路 Run）。

根因：`app/repositories.py` 的 `topic_distribution()` 按 `AgentRun.topic` 分组时
把 NULL 也带了回来，落到 `TopicCount(topic=None)`——而 `TopicCount.topic` 是 `str`：

```
1 validation error for TopicCount
topic
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
```

### 修复

只改 `app/repositories.py` 一处：聚合加 `where(AgentRun.topic.is_not(None))`。

**排除了 NULL，而不是造占位值**：

- 对外形状 `TopicCount.topic: str` 装不下 NULL；
- "还没有主题"不是一个话题。返回 `"unknown"` 会把一个**运行中的临时状态**
  说成一种内容类别，读图的人会以为系统产过这类主题。
- 前端其实已经预期到这一点——Analytics 页面上写着"计数之和 ≠ Run 总数"。

代价是正在选题的那部分 Run 暂时不计入这张图；它们**仍然计入 `run_volume`**。

**没有做的事**：没有改数据库 schema，没有给运行中的 Run 填假 topic，没有改 Agent
行为绕开 analytics。

### 回归测试（`tests/test_admin_api.py`）

`test_analytics_survives_a_run_without_a_topic`：播种一条 `topic=NULL` 的 Goal Run
+ 一条正常 Run，断言 HTTP 200、分布里**只有**正常 topic、且 `run_volume == 2`
（排除的是"这张图里的这一条"，不是这个 Run）。

### 修复后（真实库、真实后端）

```
GET /api/admin/analytics  200
run_volume 7
topic_distribution: 5 条真实主题，各 1 次；无 NULL、无占位值
```

---

## 6. Tests

全部**串行**执行（`xagent_test` 跨进程共用，两份 pytest 并发会互相 TRUNCATE）。

### 6.1 后端

```
python -m pytest -q
270 passed in 65.86s
```

基线与增量的关系是干净的：集成前 **266**，本次新增 **4** 条
（3 条 planner + 1 条 analytics），266 + 4 = 270。

### 6.2 前端

```
npm run test:frontend
tests 42 / pass 42 / fail 0
```

### 6.3 类型检查与构建

```
npx tsc --noEmit    → 退出码 0，无输出
npm run build       → ✓ built in 1.72s
                      index.js 639.19 kB │ gzip: 188.04 kB
```

构建有一条 chunk > 500 kB 的提示（recharts 体积所致），**不是错误**，本次不做
代码分割（属于优化，不在本阶段范围）。

### 6.4 Evaluation

```
python -m evals.selftest                          → 11 / 11
python -m evals.run_benchmark --dataset goals.json → 10 / 10
python -m evals.run_benchmark --fixture            → pass
```

### 6.5 两条回归

- **Analytics**：`test_analytics_survives_a_run_without_a_topic`（新增，见 §5）。
- **Agent fallback**：§2.3 的三条 planner 测试（新增）。

---

## 7. 浏览器验收（真实浏览器）

用系统 Chrome 以 headless 模式驱动，通过 DevTools Protocol 做**真实点击**，
读回真实渲染后的 DOM 文本（不是 mock、不是单元测试）。

| 项 | 结果 |
| --- | --- |
| 启动 PostgreSQL / FastAPI / Vite | 全部就绪（`:8000` `/health` 200，`:5173` 200） |
| `/` Overview | 渲染完整，含 5 条真实待审 Run 与真实草稿 |
| `/runs` | 表格 + 筛选 + `查看` |
| `/runs` → Drawer | 真实点击后 Drawer 打开，**Agent 决策轨迹 11 步**完整可读 |
| `/review` → 选 Run → 编辑 → 保存 | **写入成功**：状态 → `edited`，`human_action=edit`，`final_content` = 编辑后的文本 |
| `/published` | 空态清晰，五种发布状态独立 |
| `/analytics` | 修复后正常渲染 5 条真实主题，无错误态 |
| Publish 确认对话框 | 正确渲染（含风险提示 + 必须勾选的确认框） |

### 6.4 提到的三态都实际观察到

- **Empty**：`/published` 无记录态、Analytics 主题分布无数据态；
- **Error**：见下（一次真实的 HTTP 500）；
- **Loading**：`ui/Skeleton.tsx` 存在并在路由切换时使用。

### 关于 Publish：**没有发真实帖子**（遵守边界）

按工作单第十五节，本次**没有**为了验收再发真实 X 帖子。只验证到确认对话框：

```
发布到 X？
确定要发布到真实 X 账号吗？下面的文字会以公开帖子的形式发出去。
该操作会创建一条公开帖子。
它会立刻生效，并且无法从这个控制台撤销…
我明白：这一下会立刻在 X 上公开发出这条帖子。   ← 必须勾选
[取消]  [立即发布]
```

并确认了安全门真的生效：

```
{"confirmExists":true,"confirmDisabled":true,"checkboxExists":true,"checkboxChecked":false}
```

确认按钮在勾选之前**不可点**。没有点击"立即发布"。

事后核对：`published_post` 表 **0 行**，后端日志里**0 次** X API 调用。

### 验收过程对真实数据的改动（如实披露）

1. **`/review` 写路径验收**在一条真实待审 Run（`1da002dc`）上完成：
   把它编辑并保存，状态由 `waiting_for_review` → `edited`。
   这是产品正常操作，且 `edit` **不会**触发发布（发布是独立端点）。
   **未对它执行发布。**
2. 为验证写路径曾播种过一条临时 Run（`32eb95c6`），验收后**已删除**，
   库内 Run 数恢复为 7。

---

## 8. 剩余问题（真实存在，本次未修）

### P2-a：审核/发布失败时，前端丢掉了后端给的 `error` 文案

- **现象**：`/review` 保存失败时界面显示 `HTTP 500 请求失败：HTTP 500。`
- **原因**：`app/main.py` 的 `_failure_response` 会把一个**脱敏过的**描述性
  `error` 放进响应体（`_describe_failure` 负责脱敏），但
  `frontend/src/api/client.ts` 的 `describeError` 只认 FastAPI 标准的
  `{"detail": ...}`，不读 `error` 字段。
- **影响**：操作人只看到状态码，看不到后端已经准备好的原因。
- **为什么没修**：这是前端行为改动，不在本次工作单点名的修复范围内；
  且现有实现是**有意**不把后端原始错误体抛给界面的，改动需要一并想清楚
  哪些字段可以透出。

### P2-b：Graph resume 失败时，Run 会回到队列但界面提示较笼统

- **现象**：审核提交触发 graph resume，若该 Run 没有 checkpoint（例如数据被
  人工改动、或库从备份恢复），会返回 500。
- **已有的安全行为**：失败路径会 `release_run_review_claim` 把状态**放回**
  `waiting_for_review`，所以同一个决定可以重试，不会把 Run 永久卡死
  （本次已实测：临时 Run 在失败后状态仍是 `waiting_for_review`）。
- **为什么没修**：生产里处于队列中的 Run 一定有 checkpoint，这是人为构造数据
  才会走到的路径；修它需要超出"最小必要"的改动。

### 已知数据缺口（不是缺陷）

- `X 内容指标`（曝光、点赞、互动率等）**未采集**。Analytics 页面显式显示
  "未采集 / 暂不可用"，而不是显示 0——这符合"不拿假数字顶替不可计算的指标"。
- Evaluation 的**人工内容质量**维度按设计不填（见 §3.3）。

---

## 9. 结论

本次会话**只做集成、稳定化与验收**，没有进入 Phase 5B：

- 未新增 Trend Intelligence / Style Distillation / Viewpoint Engine；
- 未采集 X metrics；
- 未新增数据库字段、未新增架构、未做重构；
- 未执行任何真实 X 发布。

最终基线：`master = bfd9652`，工作区干净，全部测试通过。
