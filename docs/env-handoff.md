# 环境交接单

> 2026-09-28。**环境已完全跑通，项目能起。** 本文档前身是一份"卡在 Docker"的
> 续跑单；那些坎都已解决，现已重写为当前实况 + 怎么跑。

---

## 1. 一句话状态

依赖装齐、数据库建好、测试全绿、前后端都能起（2026-09-29 实测 **266 passed**，见 §4）。
上一轮悬置的 **langgraph 跨大版本兼容性问题，结论是能用**（见 §5）。

---

## 2. 怎么跑

三个东西，按顺序：

```bash
docker compose up -d          # Postgres，容器名 xagent-postgres，映射到 5433
alembic upgrade head          # 首次或迁移有更新时
python run.py                 # 后端 → http://127.0.0.1:8000
```

前端另开一个终端：

```bash
cd frontend && npm run dev    # 控制台 → http://127.0.0.1:5173
```

浏览器开 **http://127.0.0.1:5173**。前端是独立 Vite 服务，`/api` 由它代理到
8000（见 `frontend/vite.config.ts`），后端本身不托管静态资源。

> ⚠️ **不要直接 `uvicorn app.main:app`**。uvicorn 0.36+ 在 Windows 上会绕过
> event loop policy 选 ProactorEventLoop，psycopg 异步连接会全部失败。
> 必须走 `python run.py`（理由写在 `run.py` 和 `app/loops.py` 顶部）。

停止：

```bash
taskkill //PID <backend_pid> //F
taskkill //PID <vite_pid> //F
docker compose stop            # 别用 down -v，会删数据
```

---

## 3. 本机特有的坑（换机器不适用）

### 3.1 数据库跑在 5433，不是 5432

5432 被**另一个项目**的容器 `exchange-db` 占着（quant 项目，postgres:15，
里面只有 `exchange_db`）。它的 restart 策略是自启的，**每次 Docker 重启都会
重新抢占 5432**。

所以 `.env` 里是：

```
POSTGRES_PORT=5433
DATABASE_URL=postgresql+psycopg://xagent:xagent@localhost:5433/xagent
```

这两行**必须同步改**。若哪天报 `role "xagent" does not exist`，先怀疑端口被
打回 5432 了，而不是数据丢了。

`tests/support.py` 已改为从 `settings.database_url` 派生（只替换库名），
所以测试自动跟随 `.env`，不再有写死的端口。

### 3.2 Docker Hub 连不上

`registry-1.docker.io` 超时，`daemon.json` 也没配镜像加速。实测只有
`docker.1panel.live` 通。绕过方式（不必改 `daemon.json`、不必重启 Docker）：

```bash
docker pull docker.1panel.live/library/<image>:<tag>
docker tag  docker.1panel.live/library/<image>:<tag> <image>:<tag>
```

注意这是**第三方源**，不是官方 Docker Hub。

### 3.3 Python 环境是 conda，不是 `.venv`

本机走的是 conda 环境 `2026-agent`（路径见 §7 的 `<CONDA_ENV>`），不是 `.venv`。
`docs/setup-new-machine.md` §3 原先只写 `.venv`，2026-09-29 已改为并列两条路线、
并统一用 `<PYTHON>` 指代解释器（见 §8.1）。

### 3.4 npm 缓存目录

`<NODE_HOME>\node_cache` 整个目录不可写。症状阴险：`npm install`
返回 **exit 0 但装空**。已通过用户级 `~/.npmrc` 把 `cache` 改到
`%LOCALAPPDATA%\npm-cache` 修好。以后判断装没装上，**看 `tsc`
在不在，不要看退出码**。

---

## 4. 跑测试

```bash
PYTHONPATH="<仓库绝对路径>;<仓库绝对路径>\tests" \
  <CONDA_ENV>\python.exe -m pytest -q
```

`PYTHONPATH` **必须带 `tests`**，否则 `from support import ...` 会失败。

`tests/conftest.py` 的 `_prepare_test_db` 是 session 级 autouse，一跑就建
`xagent_test` 库，**没有 Postgres 整场起不来**。

当前结果：**266 passed，19 个测试文件，约 95 秒**（2026-09-29 实测）。测试数会随
Phase 5A.1 增长，别把它当固定值。

> ⚠️ **同一时刻只能有一份 pytest 在跑。** `session_factory` 在每个测试前
> `TRUNCATE` 全部业务表，而 `xagent_test` 是**跨进程共用**的一个库；两份 suite
> 并发时互相清表，会报出"失败集合每轮都不一样"的假故障。同一棵树实测分别得到
> 57 / 27 / 17 failed，而单跑其中一个测试文件全绿、无人打扰时 266 passed。
> 看到零散且每轮不同的失败，先确认没有另一份 pytest 在跑，再去怀疑代码。

---

## 5. langgraph 跨大版本解析：已验证

上一轮标记为"未验证风险"的那件事有结论了。

`pyproject.toml` 里写的是 `langgraph>=0.2`，实际解析到 **1.2.12**（跨大版本）。
全部测试绿（266 passed），说明 `interrupt`/`Command` 语义与 checkpointer 契约都还成立。

`checkpointer.py:24` 的 `JsonPlusSerializer(allowed_msgpack_modules=[...])`
白名单也核对过了：把 `RunState` 的完整类型闭包（含嵌套）跑出来对了一遍，
**10 个 reachable 类型全部已登记，无遗漏**。代码注释警告的"漏一个就只在真实
运行的恢复路径上炸、单测碰不到"那个雷，目前不存在。

> 新增 Agent state 字段时，仍需回 `checkpointer.py:24` 补白名单。

---

## 6. 本轮改动面

```
 M pyproject.toml            psycopg[binary] → [binary,pool]；新增 langgraph-checkpoint>=4.1
 M .gitignore                新增 *.bundle / *.zip（迁移快照与会话转录不进仓库）
?? requirements.lock.txt      67 包钉死，与实机 pip freeze 零漂移（见 §8.2）
?? docs/env-handoff.md       本文档
   .env                      POSTGRES_PORT / DATABASE_URL 改 5433（gitignored，不进仓库）
```

这四项已随 `chore: harden repository environment handoff` 提交。

另有两项**未纳入该提交**：

```
 M tests/support.py          去掉写死的 localhost:5432，改为从 settings.database_url 派生
 M docs/setup-new-machine.md §3 / §7 / §9 事实订正（锁文件、<PYTHON>、测试并发限制）
```

`tests/support.py` 会改变测试语义（测试库地址不再写死，改由 `.env` 的
`DATABASE_URL` 派生），与该提交"纯仓库/环境整洁、不动测试语义"的边界冲突，
留给主会话决定。

---

## 7. 版本速查

```
Python      3.12.14   conda 环境 2026-agent
                      <CONDA_ENV>
Node        v24.12.0 / npm 11.6.2
Docker      29.3.0
Postgres    16-alpine，容器 xagent-postgres，宿主端口 5433
langgraph   1.2.12
checkpoint  4.2.0
psycopg     3.3.6
pydantic    2.13.5
alembic     1.20.0
fastapi     0.141.1
sqlalchemy  2.1.1
```

---

## 8. 两个遗留问题的处置（2026-09-29）

### 8.1 `docs/setup-new-machine.md` 与实际不符 —— 已修订

原文两处：装依赖那条没提锁文件；`python -m venv .venv` 与实机用的 conda
`2026-agent` 不一致。**均已改**：§3 并列 venv / conda 两条路线，改为先
`pip install -r requirements.lock.txt`、再 `pip install -e . --no-deps`，并引入
`<PYTHON>` 占位符（§7 / §9 同步替换掉原先写死的 `.venv\Scripts\python.exe`）。
同轮补进一条新事实：**两份 pytest 不能并发跑**（§9，实测依据见本文 §4）。

### 8.2 `requirements.lock.txt` 要不要提交 —— 已提交

先确认它**不含** gitee 的 VCS URL：生成时已剔掉项目自身的 editable 记录，文件里
没有 `-e git+...`。再核对内容：67 个包与实机 `pip freeze` **完全一致、零漂移**。
因此**已提交进仓库**。

平台限制不变：这是 Windows / CPython 3.12 的快照，含 `psycopg-binary`、
`colorama`、`watchfiles` 等平台特有包，**换平台不能直接套用** —— 已写在文件头部注释。
