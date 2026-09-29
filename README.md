# X AI Content Agent

从"一个话题"到"一条可发布的中文 X 帖子"的流水线，外加一个人工审核控制台。

Agent 自己发现选题、检索材料、提炼证据、形成观点、写出候选正文、自我评审，
最后**停下来等人决定**。发布到 X 永远需要人再确认一次。

---

## 核心流程

```
话题 / 目标
   ↓
discover_topics      发现候选选题
research_topic       检索主题材料        ← 真实 X 只读检索
deepen_research      深入研究
extract_evidence     提炼证据（可追溯）
generate_angles      生成观点角度
select_angle         选定观点
generate_candidates  生成候选正文
criticize            内容评估（accept / revise / reject）
revise               按评审意见改稿（改完必须重新评审）
finish               交付
   ↓
人类审核（批准 / 编辑 / 拒绝）
   ↓
发布到 X（需再次确认）
```

两条链路并存：

- **话题链路**（Phase 1–4a）：调用方给定 `topic`，Agent 直接围绕它工作。
- **Agent 链路**（Phase 5A 起）：调用方只给 `goal`，选题由 Agent 自己发现。
  `topic` 在 Agent 选定之前是 `NULL`，这是正常运行状态而不是脏数据。

预算（步数 / 检索次数 / 候选数 / 改稿次数）在 `config/agent.yaml` 里配置，
由 `app/agent/budget.py` 用纯 Python 强制，**不依赖模型自觉遵守**。

---

## 项目结构

```
app/
  agent/            Agent 决策与执行
    budget.py         预算与「下一步能不能做」的确定性判定
    graph.py          LangGraph 编排
    nodes/            planner / research / generate / guardrail / human_review / finalize
    tools/            discover, research, evidence, angles, candidates, critic, revise
    metrics.py        运行质量指标（被 evals 复用）
  api/admin.py      只读运营控制台 API（/api/admin/*）
  services/         LLM、X API、发布、OAuth
  db/               SQLAlchemy 会话 + LangGraph checkpointer
  models/           数据模型
  repositories.py   数据访问（含 analytics 聚合）
  main.py           FastAPI 应用
frontend/           React + Vite 运营控制台
  src/pages/        Overview / Runs / Review / Published / Analytics
evals/              Agent 评估实验室（离线、不调真实 X / LLM）
docs/               设计、handoff、评估说明
tests/              后端测试
config/             agent.yaml（预算）、persona.yaml（人格）
prompts/            Agent 与写作 prompt
alembic/            数据库迁移
```

> `evals/` 与 `app/` 共用生产实现（例如 `app/agent/budget.py`、`app/agent/metrics.py`），
> 不复制第二套逻辑——评估的对象必须是**生产那份**判定。

---

## 环境要求

- Python **3.12**
- Node.js 20+（前端用）
- PostgreSQL 16（Docker 或本机均可）

---

## .env

复制模板后填写：

```bash
cp .env.example .env
```

关键项：

| 变量 | 说明 |
| --- | --- |
| `POSTGRES_*` | 数据库账号/库名/端口 |
| `DATABASE_URL` | 应用连接串（`postgresql+psycopg://...`） |
| `LANGGRAPH_CHECKPOINT_DATABASE_URL` | 留空则由 `DATABASE_URL` 推导 |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | OpenAI 兼容接口 |
| `X_API_BEARER_TOKEN` | X 只读检索（search 端点） |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | 发帖必须用 OAuth 1.0a user context，Bearer token 发不了帖 |

**注意**：`POSTGRES_PORT` 与 `DATABASE_URL` 里的端口必须一致，共两处。
若本机 5432 已被别的项目占用，把两处一起改成 5433 之类的空闲端口。

`.env` 已在 `.gitignore` 里，不会被提交。

---

## 启动 PostgreSQL

```bash
docker compose up -d postgres
```

数据库迁移：

```bash
<PYTHON> -m alembic upgrade head
```

（Docker Hub 不可达时，可先经镜像站拉取 `postgres:16-alpine` 再本地 retag。）

---

## 启动 Backend

```bash
<PYTHON> run.py
```

监听 `127.0.0.1:8000`。

> 不要直接 `uvicorn app.main:app`：uvicorn 0.36+ 在 Windows 上会选用
> `ProactorEventLoop`，而 psycopg 的异步连接在它上面会全部失败。
> `run.py` 已显式指定 `app.loops:selector_loop_factory`。

## 启动 Frontend

```bash
cd frontend
npm install
npm run dev
```

监听 `127.0.0.1:5173`，并把 `/api` 代理到后端 8000。
控制台页面：`/`、`/runs`、`/review`、`/published`、`/analytics`。

---

## 运行测试

```bash
<PYTHON> -m pytest -q                       # 后端（270）
cd frontend && npm run test:frontend         # 前端
cd frontend && npx tsc --noEmit              # 类型检查
cd frontend && npm run build                 # 构建
```

> **⚠️ 同一时刻只能有一份 pytest 在跑。** `tests/conftest.py` 在每个测试前
> `TRUNCATE` 全部业务表，而测试库 `xagent_test` 是**跨进程共用**的一个库。
> 两份 suite 并发时互相清表，会出现"失败集合每轮都不一样"的假故障——
> 看到零散且每轮不同的失败，先确认没有另一份 pytest 在跑。

测试全程 Mock X API 与 LLM，不消耗真实 credits，也不碰 `xagent` 主库。

---

## 运行 Evaluation

Agent 评估实验室是**离线**的：读 fixture / state 快照，不调真实 X、不调 LLM。

```bash
<PYTHON> -m evals.selftest                        # 评估器自检
<PYTHON> -m evals.run_benchmark --fixture         # 跑全部 fixture
<PYTHON> -m evals.run_benchmark --dataset goals.json   # 按基准目标判定
```

评估覆盖三类东西：

- **结构指标**：步数、检索次数、改稿次数、预算是否耗尽……
- **行为规则**：改稿后必须重新评审、非法动作的恢复、不得自主发布……
- **内容质量**：人工维度**暂不填写**——不拿字符串规则冒充内容评审。

`--live` 被显式拒绝：对真实环境跑评估不可复现。

---

## Human-in-the-loop / Publish 安全边界

这几条是有意为之的硬约束，改动它们之前请先读 `docs/` 里的设计文档：

1. **Agent 不会自己发帖。** 跑到 `finish` 就停下，状态进入 `waiting_for_review`。
2. **审核是独立的一步。** 批准或编辑之后，内容**仍然**需要单独确认才会发到 X。
3. **发布对话框要求显式勾选**"我明白这一步会立刻公开发出"，否则确认按钮不可点。
4. **`publish_unknown` 不等于失败。** 请求超时/结果不明时状态是 `publish_unknown`
   （界面写作"发布结果未定"），必须人工裁决，系统不会自行重发或当作失败。
5. **发布状态以 `published_post` 为准**，Run 自己的状态只是它的镜像视图。
   控制台是审计记录，不是 X 的实时镜像——在 X 上删帖不会同步回这里。
6. **改稿必须重新评审。** 改完稿会作废该候选的旧评价，`finish` 会因此被挡住，
   直到新一版被重新评价过（`app/agent/tools/revise.py`）。

---

## 相关文档

- `docs/setup-new-machine.md` —— 换一台机器跑起来的完整步骤
- `docs/env-handoff.md` —— 环境与依赖细节
- `docs/phase-5a-agent-brain-design.md` —— Agent 决策链路设计
- `docs/agent-evaluation/` —— 评估实验室说明
- `docs/frontend-product-polish.md` —— 控制台产品化记录
- `docs/integration-stabilization.md` —— 集成与稳定化验收报告
