# 在另一台机器上跑起本项目

> 写于 2026-09-26，对应 `master = 60be14b`（Phase 5A 收口）。
> 2026-09-29 按 `225c263` 修订 §3 / §7 / §9：依赖安装改走 `requirements.lock.txt`、
> 解释器路径统一为 `<PYTHON>`、补上"测试不能并发跑"这条限制。
> 这份文档的目标读者是"换一台机器的人"，不是第一次读代码的人。

---

## 1. 装什么

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | **3.12+** | `pyproject.toml` 写死了 `requires-python = ">=3.12"` |
| Node.js | **18+** | 前端 Vite 5 的要求；开发机实测 v24 可用 |
| Docker Desktop | 任意近期版本 | 只用来跑 PostgreSQL 16 |
| Git | 任意 | |

---

## 2. 拉代码

```bash
git clone <仓库地址> X_Agent
cd X_Agent
```

> **⚠️ 如果 `git clone` 报证书错误**（`error setting certificate verify
> locations`），说明这台机器的 Git 被移动过目录、而安装路径被烧进了系统级
> `etc/gitconfig`。修法是在仓库里写一条本地配置指向真实的 CA 文件：
> ```bash
> git config http.sslCAInfo "<Git安装目录>/mingw64/ssl/certs/ca-bundle.crt"
> ```
> 用 `git --exec-path` 可以查到 Git 的真实安装目录。

---

## 3. Python 环境

```bash
# 路线 A：venv
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps

# 路线 B：conda（开发机实测走的是这条，环境名 2026-agent）
conda create -n 2026-agent python=3.12
%CONDA_PREFIX%\python.exe -m pip install -r requirements.lock.txt
%CONDA_PREFIX%\python.exe -m pip install -e . --no-deps
```

**先装锁文件，再装项目本身**，顺序不能反。下文用 `<PYTHON>` 指代你选的那条
路线的解释器（`.venv\Scripts\python.exe` 或 `%CONDA_PREFIX%\python.exe`）。

- `requirements.lock.txt` 是验证过的依赖快照（67 包）。`pyproject.toml` 的约束
  全是 `>=`、没有上界，只按它装会拿到"当下最新"——`langgraph>=0.2` 现已解析到
  1.2.12，跨大版本。
- `--no-deps` 是关键：项目本身由 `-e .` 装，别让它把锁好的依赖重新解一遍。
- 锁文件已含 `[dev]`（pytest / pytest-asyncio 在其中），不必再装 `.[dev]`。
- 锁文件是 Windows / CPython 3.12 快照，含平台特有包（`psycopg-binary`、
  `colorama`、`watchfiles`），换平台不能直接套用；见 `docs/env-handoff.md` §8.2。

---

## 4. 前端依赖

```bash
cd frontend
npm install
cd ..
```

---

## 5. `.env`（**唯一必须手工搬运的文件**）

`.env` 被 `.gitignore` 挡住，**不在仓库里**，必须从旧机器手工拷到新机器的仓库根目录。

它包含 5 组真凭据：`LLM_API_KEY`、`X_API_BEARER_TOKEN`、以及 X OAuth 1.0a 的
`X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET`。

> **传输方式**：U 盘 / 密码管理器 / 加密压缩包。
> **不要**走聊天工具、邮件，**不要**贴进任何 AI 对话（转录会被留存）。
> **不要**为了省事把它提交进 git。

字段含义见仓库里的 `.env.example`。两处容易踩的：

- `POSTGRES_PORT` 改了的话，`DATABASE_URL` 里的端口要**跟着一起改**。
- `LANGGRAPH_CHECKPOINT_DATABASE_URL` 留空即可，会自动从 `DATABASE_URL` 推导。

---

## 6. 数据库

### 6a. 起 PostgreSQL

```bash
docker compose up -d
```

如果这台机器上 5432 已被占用，改 `.env` 的 `POSTGRES_PORT`（同时改 `DATABASE_URL`）。

### 6b. 两种数据来源，二选一

**路线 A：从旧机器搬来数据**（含 Run 历史与 LangGraph 断点）

```bash
docker cp xagent.dump xagent-postgres:/tmp/
docker exec xagent-postgres pg_restore -U xagent -d xagent --clean --if-exists /tmp/xagent.dump
```

dump 里已含 `alembic_version`，**不需要**再跑 `alembic upgrade head`。

**路线 B：全新空库**

```bash
alembic upgrade head
```

业务表由迁移建；LangGraph 的 4 张 checkpoint 表由应用启动时
`AsyncPostgresSaver.setup()` 幂等自建（`app/db/checkpointer.py`），不用手工建。

> **⚠️ 不要对 LangGraph 的表跑 `alembic revision --autogenerate`**：它会把
> checkpoints / checkpoint_writes / checkpoint_blobs / checkpoint_migrations
> 判为"已删除"并生成 `drop_table`，跑下去会删光所有会话断点。见
> `alembic/versions/0b17346696c6_phase4_publish_state.py` 的模块注释。

---

## 7. 启动（两个终端）

**后端** —— Windows 上**必须**用 `python run.py`：

cmd：

```bat
set HTTPS_PROXY=http://127.0.0.1:7890
set HTTP_PROXY=http://127.0.0.1:7890
set NO_PROXY=api.deepseek.com,localhost,127.0.0.1
<PYTHON> run.py
```

PowerShell（Win11 默认终端）：

```powershell
$env:HTTPS_PROXY="http://127.0.0.1:7890"
$env:HTTP_PROXY="http://127.0.0.1:7890"
$env:NO_PROXY="api.deepseek.com,localhost,127.0.0.1"
<PYTHON> run.py
```

**前端**：

```bash
cd frontend
npm run dev
```

控制台地址 **<http://127.0.0.1:5173>**（8000 是 API，`/docs` 仍在但已不需要）。

---

## 8. 三个已知坑

### 8.1 代理（最容易误诊的一条）

本机**直连 `api.x.com` 不通**，必须走本地代理（开发机上是 Clash，监听
`127.0.0.1:7890`）。新机器的代理端口大概率不同，也可能直连就通。

**代理不通的表现是 Research 报 502，错误信息看起来像凭据错了。**
先做这个对比再怀疑代码：

```bash
curl -x http://127.0.0.1:7890 https://api.x.com/2/tweets/search/recent   # 走代理
curl https://api.x.com/2/tweets/search/recent                            # 直连
```

`NO_PROXY` 里带上 `api.deepseek.com`，让本来直连就通的 LLM 绕开代理。

### 8.2 Windows 的 event loop

必须 `python run.py`（内部指定了 `loop="app.loops:selector_loop_factory"`）。
直接 `uvicorn app.main:app` 在 Windows 上会选到 ProactorEventLoop，psycopg
异步连接**全部失败**。这条只在 Windows 上是坑。

### 8.3 服务没有进程守护

被托管的后台 shell 一被回收，服务就静默消失，**没有任何提示**，用户侧只表现为
浏览器连不上。判断方法：

```bash
curl -s -m 5 http://127.0.0.1:8000/health
netstat -ano | grep :8000
```

另外控制台**有两层依赖**：后端活了但 Vite 没起，页面照样打不开。

---

## 9. 跑测试

需要 Docker 里 PostgreSQL 已启动，否则连接超时、整个 suite 会跑很久才报错。

```bash
set PYTHONPATH=<仓库绝对路径>;<仓库绝对路径>\tests
<PYTHON> -m pytest -q
```

测试库 `xagent_test` 由 `tests/conftest.py` 自行建库建表并逐测清表，**不碰 `xagent` 主库**。
X API 与 LLM 一律 Mock，不消耗真实 credits。

> **⚠️ 同一时刻只能有一份 pytest 在跑。** `tests/conftest.py` 的 `session_factory`
> 在每个测试前 `TRUNCATE` 全部业务表，而 `xagent_test` 是**跨进程共用**的一个库。
> 两份 suite 并发时两边互相清表，会出现"失败集合每轮都不一样"的假故障：同一棵树
> 实测分别得到 57 / 27 / 17 failed，单跑其中一个测试文件却全绿，无人打扰时
> **266 passed**。看到这种零散且每轮不同的失败，先确认没有另一份 pytest 在跑。

---

## 10. 相关文档

- `docs/phase-5a-handoff.md` —— Agent Brain V1（当前 HEAD）
- `docs/phase-4b-handoff.md` —— 运营控制台
- `docs/phase-4a-handoff.md` —— 发布状态机
