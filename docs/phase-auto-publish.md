# 无人值守自动发布（影子模式先行）

**范围**：让 Agent 定时、无人值守地出稿。第一版**只到"影子模式"**——自动审阅
照常判断并记录，但一条都不发，人仍然照常审；量出一致率之后才谈转正式。

**基线**：开工时 `master` HEAD `9aa04a6`。

**封版 HEAD**：**本文件所在的提交**（`git log --oneline -1` 即得）。此处刻意不写
字面哈希——文档每改一次哈希就变，写死必然过期。

**状态**：**影子期，尚未转正式。** 后端完整、916 条测试通过、真机 smoke 跑通
（`docs/phase-auto-publish-smoke.json`）。当前一致率数据为 **0 对**——影子期刚开始，
门槛全部未达（见 C 节）。

---

## A. 这个功能在做什么

复用人已经走过的**全部**路径，而不是新开一条：

```text
POST /api/runs            →  create_agent_run + graph.ainvoke
                             Graph 在 human_review interrupt
                             run.status = waiting_for_review
   ┌──────────────────────┴───────────────────────┐
   │ 人：POST /review                              │ 自动：tick()
   │   claim_run_for_review CAS                    │   claim_run_for_review CAS
   │   graph.ainvoke(Command(resume=approve))      │   graph.ainvoke(Command(resume=approve))
   │   → human_review_node 重放 → create_review    │   → 同一个节点，decided_by="auto"
   └──────────────────────┬───────────────────────┘
                          ↓
              PublishService.publish(run_id)   ← 完全没动
```

**为什么必须重放 Graph 而不是直写 review 行**（这是个真正的岔路口，四条理由）：

1. `_preflight` 校验 `state["final_content"]`。重放时节点同时写 review 行**和**
   `final_content`，两者一致；直写则 `final_content` 永不存在，校验被跳过——等于
   用更弱的保证去发布。
2. `claim_run_for_review` 是唯一把人和机器串行化的 CAS。直写要么重新实现它，
   要么就没有它——后者会重新引入 CAS 本来要防的重复 resume。
3. 直写会让 LangGraph 线程**永远停在 human_review**。本仓库**没有 checkpoint GC**，
   每次自动发布泄漏一个挂起的线程，而管理台正是从 checkpoint 读 `human_action` /
   `final_content` 的——界面和数据库会永久互相矛盾。
4. 重放天然幂等：`create_review` 是"先查后写"，重放不会插第二行。

代价是自动侧必须复刻 HTTP handler 的失败处理，所以那段抽成了共享的
`app/services/review.py::resume_review(runtime, run_id, action=, resume_value=)`，
`POST /review` 和自动侧**都调它**。

### 一条刻意的不对称

| 自动结论 | 动作 |
|---|---|
| **通过** | 才真正动作（占额度、发帖） |
| **不通过** | **什么都不做** —— run 留在 `waiting_for_review`，带理由进人工队列 |

误判代价不对称：一次错误拒绝只多一条队列记录；**一次错误批准就是一条已发出去的
烂帖**。所以自动闸门只是"快车道"，不是判决者。

这条不对称也让影子模式天然成立：不通过时两种模式行为**完全一致**，差别只在通过
那一侧——影子期量出来的行为就是正式期的行为。

---

## B. 判据（`app/services/auto_review.py`，零 IO、零 LLM）

任一不过即不通过，**无加权、无综合分**（沿用学习层的口径）。六条按顺序：

| # | 判据 | 说明 |
|---|---|---|
| 1 | `content_gate` | 调**发布路径同一个函数** `evaluate_content_gate`。各写一份迟早变成"自动放行了、发布被 422" |
| 2 | `guardrail` | 只看**非提示性**的原因码。`too_long` 是提示性的——账号是 Premium，长度是我们自己的写作目标 |
| 3 | `critic` | **交付候选**的结论必须是 `accept`。见下面这条 |
| 4 | `critic_score` | `>= min_critic_score` |
| 5 | `repetition` | a/c 层全清（b 层只记录、不拦截） |
| 6 | `volume_cap` | 当日**尝试**次数未超上限 |

每条判据的取值都写进 `auto_review.checks`，而不只是"过没过"——转正式前要回答
"哪条判据在误拦"，只知道"被拒了"是回答不了的。

### 三个容易写错的地方

**1. 判据看的必须是实际交付的那条候选，不是 `critic_result`。**
`state["critic_result"]` 是"最近一次评价里该关注的那条"，而 `finalize` 交付的是
`pick_final_candidate(state)` 选出来的**另一条**——改稿之后、有 reject 之后会不同。
直接读 `critic_result.decision` 会**批准一条 Critic 从没说过 accept 的稿**。
所以复用 `pick_final_candidate`，取它选中的 `candidate_id`，再回 `critic_results`
查裁决；**查不到裁决 → 拒绝**。

**2. 影子模式下重复检测的输入不能只有"已发布正文"。**
影子模式什么都不发，`published_post` 始终为空——只喂已发布正文的话，检测器看到的
是一份**空语料，于是放行一切**，影子模式退化成永远说"通过"的橡皮图章。
输入必须包含近期 `agent_run.topic`（所有状态）+ 近期草稿正文。

**3. 未知的 guardrail 原因码默认拦。**
方向是刻意的：将来新增一个硬性检查却忘了登记，表现是"自动发布变保守了"——一个会
被发现、可修的偏差；反过来（默认放行）则是静默的。

### 重复检测的三层（`app/style/repetition.py`）

| 层 | 手段 | 定位 |
|---|---|---|
| a. 逐字 / 近逐字 | 复用 `spans`，`SHARED_SPAN = 12`，切片前**先去掉空白** | **硬闸门** |
| b. 词汇相似 | 字符 n-gram containment，`LEXICAL_N = 8` + 长度下限 | **仅记录，不单独拦截** |
| c. 选题时效 | `agent_run.topic` 归一化后精确匹配，14 天窗口 | **硬闸门** |

b 层是最弱的一环：中文短文本即便不相关也有很高的字符重合，而字符 n-gram 建模的是
**表面形式**，对"同一个意思换个说法"恰恰是盲的——而后者正是最可能发生的失败模式。
n=3–4 误报不可用，n=12 又塌回 a 层，所以 v1 只把它记进 `checks` 供观察。
真正干活的是 c 层：重复讲同一个点的帖子，选题串几乎必然复用。

**测不出来的**（如实写在这里，因为影子和人是唯一兜底）：真·语义改写、同一主张换
证据、"同一事件换个角度"（选题串不同）。项目禁 ML 依赖，不做 embedding。

---

## C. 转正式的门槛（`GET /api/admin/auto-publish/status`）

影子期产生"配对"的前提是人真的审了自动审过的 run。**唯一的度量来源**是
`auto_review ⋈ review ⋈ agent_run`（限定 `origin='scheduled'` 且
`review.decided_by='human'`）算出的混淆矩阵。

> **`decided_by='human'` 这个过滤条件不是可选的。** 正式模式下自动侧会重放
> human_review 节点、以 `decided_by='auto'` 留下一条 `review` 行。那条行和它
> 自己写下的 `auto_review` 按 `draft_id` 天然配得上对，于是一条机器放行的 Run
> 会白送一个"一致"。方向是单向的——机器只在判通过时才动作，所以它写的每条
> review 行都必然是 approve、配上的也必然是 approve——一致率被推向 1.0，
> 而"一致率 ≥ 90%"正是门槛第 4 项。漏掉这个过滤，等于让机器拿自己的判断给
> 自己打分。影子模式下没有这条行，所以这个错**恰好从翻正那一刻开始**，
> 也就是恰好从它开始算数的时刻开始。（同一个理由，控制台 analytics 上那块
> 「审核决策分布」也只数 `decided_by='human'`。）

映射必须写进响应里（`mapping_note`），免得有人重新推一遍：

- 人 `approve` = 原样接受；人 `edit` = 不会原样接受（正式模式发的是**未编辑**原稿）；
  人 `reject` = 拦下
- **`false_approve`** = 自动通过 + 人 reject —— **最危险的那个错**
- `soft_mismatch` = 自动通过 + 人 edit
- `false_reject` = 自动不通过（但人本会发）—— 只多一条队列记录

**翻转门槛（全部满足，统计窗口自上次改配置起）**，即 `evaluate_go_live_gate` 的
七条判据：

| key | 条件 |
|---|---|
| `sample` | ≥ 20 个已决配对 **且** ≥ 7 天 |
| `false_approve` | **放行过（`auto_approvals > 0`）且 == 0**（硬阻断） |
| `soft_mismatch` | `soft_mismatch / auto_approvals ≤ 0.10` |
| `agreement` | ≥ 0.90 |
| `attribution` | 每个 `false_reject` 都能归因到**具名判据** |
| `content_gate` | 没有"自动放行 + 门禁非空"的记录（不变量，正常恒 0） |
| `stale_claims` | 没有无法解释的、缺 review 行的 `approved` 残留 |

另有 `manual_steps`（机器判断不了的 3 步）**每次都下发**，不因为自动判据全过就消失。

### 真机当前状态（2026-10-06）

```json
{"enabled": false, "mode": "shadow",
 "agreement": {"decided_pairs": 0, "agreed": 0, "false_approve": 0,
               "soft_mismatch": 0, "false_reject": 0, "auto_approvals": 0,
               "agreement_rate": null, "soft_mismatch_rate": null},
 "gate": {"ready": false,
          "criteria": {"sample": false, "false_approve": false, "soft_mismatch": false,
                       "agreement": false, "attribution": true,
                       "content_gate": true, "stale_claims": true}}}
```

注意 `agreement_rate` 是 **`null` 而不是 `0`**：库里已经有一条 `auto_review` 记录，
但人还没审那条 Run，所以**已决配对是 0**——"还没有数据"和"数据说不行"必须分得开。
同理 `false_approve` 此时是 **`false` 而不是 `true`**：一条都没被自动放行过时，
"误放行 = 0" 只说明那条路径还没被测过，不是成绩（0/0 不是成绩）。

转入正式时还要**金丝雀**：`mode: live` + `max_publishes_per_day: 1` + 抬高的
`min_critic_score`，观察 3–5 天再放松额度。

---

## D. 怎么开

改一行就够了——**配置每个 tick 重读一次**，这是这个功能唯一的刹车，
不需要重启任何东西：

```yaml
# config/automation.yaml
enabled: true        # ← 就这一行
mode: shadow         # 影子期保持 shadow，不要改
```

手工跑一轮：

```bash
python scripts/auto_publish_tick.py            # 人读的输出
python scripts/auto_publish_tick.py --json     # 结构化
python scripts/auto_publish_tick.py --status   # 只看一致率与门槛，不跑 tick、不改任何东西
```

退出码：`0` = 这一轮跑完了（**包括 disabled / locked / run_cap 这些"正常地什么都不做"**）；
`1` = 这一轮出了问题（配置读不了 / Agent 抛异常 / 超时）。

注册为计划任务（每 30 分钟）。**直接调 conda 环境里的 python.exe，不要走
`conda activate`，也不要用 `start.bat`**（那个脚本是无参数的开发栈启动器，会开三个
窗口并停在 `pause`）：

```bat
schtasks /create /tn "XAgent auto publish" /sc minute /mo 30 ^
  /tr "cmd /c cd /d C:\xuexi\py\2026_agent\X_Agent && set SSL_CERT_FILE= && C:\Users\Too_Far\miniconda3\envs\2026-agent\python.exe scripts\auto_publish_tick.py"
```

两条环境上的注意，都会以**看起来像网络故障**的方式失败：

- `set SSL_CERT_FILE=` —— 见 `start.bat` 里那段长注释。在 Git Bash 里 source
  activate 会把这个变量设成 Unix 布局的路径（`.../envs/<env>/ssl/cacert.pem`），
  那个文件在 Windows 上不存在；被后端继承之后，每一次出站 HTTPS 都会以
  `FileNotFoundError` 死掉，再被包成 `LLMError`——**看起来像 LLM 或 X API
  挂了，而配置和凭据都是好的**。清掉它，conda 的 `activate.d` 才会写入正确路径。
- **本机访问 X 需要走代理**，而 httpx 只读环境变量，所以计划任务里也要给它：

  ```bat
  set HTTPS_PROXY=http://127.0.0.1:7890
  set HTTP_PROXY=http://127.0.0.1:7890
  ```

  （端口以本机实际监听为准；直连会让 TLS 被重置。）

### 为什么是 CLI + 计划任务，不是进程内调度器

放在 FastAPI 的 lifespan 里技术上完全可行（`run.py` 无 `--reload` 无 `--workers`，
单进程），但：

- 一旦有人加上 `--reload` 或 `--workers N`，N 个调度器各自建 run、各自可能发布——
  而**重复发布是这套系统能产生的最坏结果**，且从进程内部无法可靠地检测 reload；
- 这台机器上后端是一个 cmd 窗口，关掉就停——这与"无人值守"正相反；
- 关停时若 cancel 落在 `_call_x` 中间，正好落进需要人工裁决的 `publish_unknown` 窗口。

项目自己的姿态也一样：`scripts/refresh_post_metrics.py` 记录了工单"明确禁止引入
分布式调度器"。机制保持最小、外部、无常驻。

### 影子模式下的只读守卫是**硬保证**

`mode: shadow` 时 CLI 把 `RealXApiClient._request` 包成**只读**的：非 GET 在发出去
之前就抛（与 `scripts/smoke_5b1.py` / `refresh_post_metrics.py` 同一套做法）。于是
"影子模式不发布"在这个进程里是**运行时会失败的**，而不是"代码里看起来没有那条
路径"——后者在下一次重构时就不成立了。

---

## E. 并发与失败（`app/services/auto_publish.py`）

| 场景 | 结论 |
|---|---|
| tick vs 人 同时 review | **安全** —— CAS 决定唯一赢家，败者 `rowcount == 0` |
| tick vs 人 同时 publish | **安全** —— `claim_publish` + `UNIQUE(agent_run_id)` 双保险 |
| 两个 tick 并发 | **advisory lock**（`pg_try_advisory_lock`）。仅 asyncio 锁只覆盖单进程，而 tick 有三个并发来源：计划任务 / 管理端点 / 本地 CLI |
| tick claim 后崩溃在 resume 前 | **reaper**：run 卡在 `approved` 且无 review 行 → 人审 409、发布也 409，谁都处理不了 |
| `ainvoke` 抛错但 checkpoint 其实已推进 | 释放前先 `aget_state` 确认仍停在 `human_review`；已推进则**不放回**，报 `released=False`，调用方不要重试 |

几个必须记住的实现细节：

- **advisory lock 必须在 `finally` 里显式 `pg_advisory_unlock`**。连接会带着锁回到
  池子里，下一个拿到它的人继承一把已经"被占用"的锁——一次漏放会让后续所有 tick
  永久静默失效。锁是**会话级**的（`pg_try_advisory_lock`，不是 `_xact_` 那个），
  因为它要跨一个 tick 里的多个事务存活。
- **抢不到锁直接放弃，不排队等**。排队只会让两个 tick 一前一后各建一个 Run，
  把当日额度加倍用掉，而计划任务下一轮马上又要来了。
- **额度检查在"建 Run"之前**。放在之后的话，每一轮超额的 tick 仍会付掉一整轮
  Agent 的钱（最多 12 步 LLM + 真实 X 检索）才发现该停。
- **reaper 只收自动发起的 Run**。人和机器不共享那把锁，收人的 Run 会把一个人
  "正在 resume 的那一次"重置回 `waiting_for_review`，他的 resume 随后照常写完
  review 行而状态已经变了——发布时撞上 409。
- **`list_recent_topics` 必须排除当前 Run**（`exclude_run_id`）。`finalize` 会把选定
  题目写进 `agent_run.topic`，而题目检测读的正是那一列——不排除它，判据就是拿这条
  稿子的题目跟它自己比，于是**每一次自动运行都因"最近已经写过同一个题目"被判不通过**。
  那是一种特别难查的失败：影子模式显示 100% 误拦，而理由看起来完全合理。

### 两个额度，不是一个

- `max_auto_runs_per_day`（tick 入口）管**成本与队列淹没错**。影子模式下一条都不发，
  但钱照花（每个 tick 一整轮 Agent），所以这个额度在影子模式下同样必须生效。
- `max_publishes_per_day` 管**对外可见的产出**，计入**尝试数**而不是成功数——
  `publish_unknown` 的那次可能已经发出去了，只数成功会让实际发文量悄悄越过上限。

> **注意**：`count_publish_attempts_since` 数的是**当天全库**的发布尝试，不区分
> 谁发的。也就是说，人今天手工发过一条，自动侧今天就一条都不会发。这是**刻意的**：
> 账号层面的频率风险与"谁发的"无关。副作用是自动化会被人手工发帖静默地饿死——
> 想让自动侧今天也发，就把 `max_publishes_per_day` 调到当天的真实总量之上。

---

## F. `decided_by`：HTTP 结构性地无法自称 auto

`decided_by` **刻意不在**决策 schema 里，而那个 schema 配着 `extra="ignore"`。
`human_review_node` 在 `ReviewDecisionAdapter.validate_python` **之前**读**原始**
resume dict 的这个字段，默认 `"human"`。

于是：HTTP 侧 resume 值由**已校验**的决策模型构造
（`main.py`: `decision.model_dump(exclude_none=True)`），客户端塞的 `decided_by`
被 pydantic 丢掉——**HTTP 路径在结构上无法注入 `"auto"`**，不需要额外写一段
"把客户端的 auto 改回 human"的补丁代码。补丁会被下一个人删掉，而"这个字段不在
schema 里"是一道删不掉的墙。

自动侧绕过这道墙的方式是**直接给 `Command(resume=...)` 传原始 dict**——这条路
只有本进程内部的代码走得通。resume 值随 checkpoint 持久化，进程崩了重放也保得住。

无法识别的取值一律**按人处理**。方向是刻意的：错标成 auto 的后果是某条人工 Run
被"清理自动残留"的逻辑顺手收走（**事故**）；错标成 human 的后果只是影子一致率的
统计略微失真（**噪音**）。

---

## G. 验证

```bash
# 必须先起 Docker，否则几百个假的 OperationalError；且必须串行（共享 xagent_test 库会互相清表）
python -m pytest -q          # 916 passed
```

专项：

```bash
python -m pytest tests/test_repetition.py tests/test_auto_review.py -q          # 37
python -m pytest tests/test_auto_publish_service.py tests/test_auto_publish_api.py -q  # 26
python -m pytest tests/test_review_api.py -q                                    # 19
python -m pytest tests/test_admin_api.py -q                                     # 45

cd frontend && npx tsc --noEmit && npm run test:frontend                        # 251
```

### 真机 smoke（2026-10-06，`docs/phase-auto-publish-smoke.json`）

一轮真实影子 tick，走真 DeepSeek（12 步）+ 真 X 检索（经本机代理）：

- 建了 Run `6f735b69-0914-4c12-971f-786a84a433e8`，`origin = scheduled` ✓
- `auto_review` 有行：`mode=shadow, verdict=reject, acted=false, critic_score=8.4` ✓
- Run 仍停在 `waiting_for_review` ✓
- **X 上一条都没发**：`published=false`，且 `published_post` 当天行数没有增加 ✓
- 只读守卫已安装 → 任何非 GET 都会在发出去之前抛，而这一轮没有抛 ✓
- 退出码 0 ✓

判据结论是 `reject`，理由是 `volume_cap`：当天已经有过一次发布尝试
（Phase 5B.4 那条真实帖子，02:29 UTC），而 `max_publishes_per_day: 1`。
**这不是 bug，是额度按设计生效了**——而且它恰好演示了 E 节那条注意事项。

---

## H. 控制台看到什么（最小可见性）

影子期的**全部产出**是一个配对矩阵，而配对要靠人真的去审那些自动 Run。所以界面
上必须做到两件事，一件都不能少：**人要知道哪条是机器排的**，以及**人要知道机器
说过什么**。少了第一件，人会把自动 Run 当自己排的；少了第二件，一致率永远没人看、
门槛永远翻不正。

刻意**不做**独立的自动化大盘。四个落点，全部是既有页面里的增量：

| 位置 | 看得到什么 |
|---|---|
| `/review` 队列条目 | `自动调度` 标签 + 机器的结论徽标 + **不通过的理由逐条列在条目里** |
| `/review` 右栏 · `/runs` 抽屉 | 「自动审阅」独立成节：结论、模式、Critic 分数、是否真的动作、理由、以及"这对你意味着什么" |
| `/runs` 列表 | `自动调度` 标签（当日额度、一致率分母、reaper 范围全靠这个字段界定，而这些 Run 大多数不在待审队列里） |
| `/analytics` | 「无人值守自动发布」一块：开关状态、一致率、混淆矩阵四个计数、7 项门槛逐条 + 3 步人工动作 |

三条措辞纪律（`frontend/src/utils/autoReview.ts`，24 个测试钉着，全部是否定式断言）：

1. **`null` 不等于 `0`。** 一致率没有数据时显示 `—` 并说明原因。显示 `0.0%` 会是
   一个在一个什么都没发生的系统上的吓人数字。同理 `critic_score` 为 `null` 时显示
   「未评价」——那是"交付候选没被 Critic 看过"，而判据在这种情况下是**拒绝**，
   显示 `0.0` 会把它说成"评过了，很烂"。
2. **`auto_approvals == 0` 时的"零误放行"什么也没证明**（分母是 0），所以那句话说
   的是"一次都没有放行过"，不是"表现良好"。
3. **机器的"不通过"不是拒绝。** Run 仍停在 `waiting_for_review`，人照样可以批准。
   写成"已被拒绝"会让人跳过一条明明还在队里的内容。同理影子模式下判**通过**也什么
   都不发——差别只在正式模式。

「自动审阅」与「规则检查」**并排但分开**：前者是这套功能自己那四项判据的合取
（内容闸门 + 交付候选的 Critic 裁决 + 重复检测 + 当日额度），后者是 checkpoint 里
每一步都在算的内容闸门。一块红一块绿是正常组合，并成一块就会看起来像自相矛盾。

### 为什么控制台没有「暂停」按钮

计划里写着"一个暂停/恢复开关"，实现时**刻意没做**，理由：

- **刹车已经存在，而且更好。** `config/automation.yaml` 的 `enabled: false`，每个
  tick 重读一次。后端没起来、页面打不开、数据库连不上时它照样是刹车；一个 HTTP
  端点在那三种情况下都没用。配置里那一行自己就写着"这是唯一的刹车"。
- **一个写到配置文件的端点要么毁掉那份注释，要么很脆。** `yaml.safe_dump` 会把
  它整整一篇说明（每一行阈值旁边都写着它为什么是这个数）清掉；而按行正则替换
  `enabled:` 是能在网页请求里做的事，但不该是。失败方向是安全的（配置读不了 →
  tick 跳过本轮的**一切**），可这仍然是把一个文件改写机制塞进 Web 层的理由。
- **控制台的只读边界有测试守着。** `tests/test_admin_api.py` 里那条
  `test_admin_api_writes_are_limited_to_the_proposal_review` 是一道刻意的闸门；
  为这个功能放行它，就要连带解释为什么 `client.ts` 里"控制台唯一的写端点"那句话
  不再成立。

所以实现成了**把开关状态显眼地读出来 + 告诉人去改哪一行**（`switchSentence`）。
控制台读 `/status`，从不写任何东西。要恢复操作，改回 `enabled: true` 即可，无需
重启。

---

## I. 明确不做

- **不动 `app/learning/`** —— 用户已定，且 n=1 的数据不支持（`MIN_SAMPLES_FOR_COMPARISON = 5`）
- **不改 `PublishService`** —— 发布状态机是既有设计里最精细的部分
- **不改 Graph 拓扑** —— 影子模式需要 interrupt 照常发生
- **不自动裁决 `publish_unknown`、不自动重试失败的发布** —— 既有纪律
- **不以进程内调度器为主机制** —— 重复发布风险
- **不做 embedding / 向量库** —— 项目禁 ML 依赖
- **不做独立的 Automation Dashboard** —— 前端只加最小的可见性
- **不加权、不做综合分** —— 沿用学习层"一个数代表好坏"的禁令

---

## J. 已知限制

1. **一致率的分母依赖人真的去审自动 Run。** 没人审，配对永远是 0，门槛永远不过。
   这是设计使然（影子模式的产出就是配对），但意味着"影子期"有一个**人力的下限**。
2. **重复检测测不出真·语义改写**（见 B 节末）。
3. **`recent_corpus_limit` 是有界的**，所以语料是"最近 N 条"而不是"全部"。
   三年前的帖子不在比对范围内——这本来也是想要的（重复风险来自"最近"），
   但要知道它是一条**窗口**，不是全量。
4. **`decided_by` 是"谁按的按钮"，不是"谁负的责"。** 正式模式下自动放行的帖子
   出事，X 那边看到的是账号，不是机器人。
5. **一次 tick 最多一条 Run。** 想提高产出就提高 tick 频率或
   `max_auto_runs_per_day`，而不是让一次 tick 里跑多轮。
6. **控制台没有「暂停」按钮**（见 H 节末）：刹车是 `config/automation.yaml` 的
   `enabled` 行，界面上只读它的状态并指出改哪里。
7. **控制台看不到"自动通过的 Run"在队列之外的分布。** 正式模式下被判通过的 Run
   会被自动发布、不进待审队列，所以它的 `auto_review` 只能在 `/runs` 列表（`自动调度`
   标签）和 Run 抽屉里看到。这是"不做独立大盘"的直接后果——"机器放行了多少条"
   要去 `/analytics` 的混淆矩阵看，不是去队列看。
