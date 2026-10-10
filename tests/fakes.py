import json
import re
from types import SimpleNamespace

SAMPLE_X_RESPONSE = {
    "data": [
        {
            "id": "1001",
            "text": "AI coding agents are transforming how teams ship software.",
            "author_id": "u1",
            "created_at": "2026-09-18T10:00:00.000Z",
            "public_metrics": {
                "retweet_count": 5,
                "reply_count": 3,
                "like_count": 42,
                "quote_count": 1,
            },
        },
        {
            "id": "1002",
            "text": "Here is how we use agents for code review.",
            "author_id": "u2",
            "created_at": "2026-09-17T08:30:00.000Z",
            "public_metrics": {
                "retweet_count": 2,
                "reply_count": 1,
                "like_count": 10,
                "quote_count": 0,
            },
        },
    ],
    "includes": {
        "users": [
            {"id": "u1", "name": "Ada", "username": "ada_dev"},
            {"id": "u2", "name": "Bob", "username": "bob_codes"},
        ]
    },
    "meta": {"result_count": 2, "newest_id": "1001", "oldest_id": "1002"},
}


class FakeXApiClient:
    """假的 HTTP 客户端层：读（search）与写（create_post）都覆盖。

    publish 的写路径错误走 `post_error` 单独注入，避免一处出错两种能力全废。
    `created` 记录每次发布的正文——发布测试必须精确断言调用次数。
    """

    def __init__(
        self,
        raw=None,
        error=None,
        post_id: str = "1900000000000000001",
        post_error: BaseException | None = None,
        lookup_raw: dict | None = None,
        lookup_error: BaseException | None = None,
    ):
        self._raw = raw if raw is not None else SAMPLE_X_RESPONSE
        self._error = error
        self._post_id = post_id
        self._post_error = post_error
        # Phase 5B.4：按 id 读公开指标的假响应。默认空（"一条都没返回"）——
        # 测试要显式给出 data，才能断言"缺席 ≠ 0"。
        self._lookup_raw = lookup_raw if lookup_raw is not None else {}
        self._lookup_error = lookup_error
        self.created: list[str] = []
        # 每次检索的 (query, max_results, start_time, end_time)。
        # 扫描窗口是 Phase 5B.1 的一等公民（"最近"必须有定义），所以测试要能
        # 断言它确实被传下去了，而不是被悄悄丢掉。
        self.searches: list[dict] = []
        # 每次按 id 抓取请求的 id 列表。
        self.lookups: list[list[str]] = []

    async def search_recent_tweets(
        self,
        query: str,
        max_results: int = 10,
        *,
        start_time=None,
        end_time=None,
    ) -> dict:
        self.searches.append(
            {
                "query": query,
                "max_results": max_results,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
        if self._error is not None:
            raise self._error
        return self._raw

    async def create_post(self, text: str) -> str:
        self.created.append(text)
        if self._post_error is not None:
            raise self._post_error
        return self._post_id

    async def get_posts_by_ids(self, ids: list[str]) -> dict:
        self.lookups.append(list(ids))
        if self._lookup_error is not None:
            raise self._lookup_error
        return self._lookup_raw


class FakeLLMService:
    def __init__(self, text=None, error=None):
        self._text = text
        self._error = error
        self.calls: list[dict] = []

    async def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        self.calls.append({"prompt": prompt, "json_mode": json_mode})
        if self._error is not None:
            raise self._error
        return self._text


# ---------------------------------------------------------------------------
# Phase 5A：Agent 循环的假 LLM
#
# Agent 每一步要的结构都不同（选题候选 / 证据包 / 观点 / 正文 / 评价），
# 所以 `FakeLLMService`（固定返回一条文本）在第一轮就会崩，必须按 prompt 分派。
#
# 分派的依据是**模板里的特征句**。这是与 prompts/agent/*.txt 的一处显式耦合：
# 谁改了模板，`test_agent_prompt_markers_match_templates` 就会指出来，
# 而不是让人对着一句"没有为 xxx 配脚本"发懵。
# ---------------------------------------------------------------------------

PROMPT_MARKERS: dict[str, str] = {
    "planner": "你是这个内容 Agent 的 Planner",
    "discover": "话题侦察员",
    "evidence": "证据整理员",
    "angles": "观点策划",
    "candidates": "写法明显不同",
    "critic": "内容团队的评审",
    "revise": "这是你需要修改的那条候选",
}

_UUID_RE = re.compile(r"\[([0-9a-fA-F-]{36})\]")
_FACT_ID_RE = re.compile(r"\[(f\d+)\]")
_CANDIDATE_ID_RE = re.compile(r"\[(c\d+)\]")
#: 材料在 discover prompt 里的短编号。让模型照抄 36 位 UUID 会被抄错（真机上
#: 一整轮 7 个簇全部核验不上），所以发现那一步给的是 `m1`、`m2`…（见
#: app/agent/tools/discover.py::_material_aliases）。
_MATERIAL_ALIAS_RE = re.compile(r"\[(m\d+)\]")


def source_ids_in(prompt: str) -> list[str]:
    """prompt 里出现过的 research_item id，按出现顺序去重。

    假 LLM 靠它给出**真实存在**的 source_ids——测试不该预先知道那些 id
    （它们是运行时生成的 UUID），而"引用一个真实 id"正是证据可追溯的前提。
    """
    seen: list[str] = []
    for match in _UUID_RE.finditer(prompt):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def material_aliases_in(prompt: str) -> list[str]:
    """discover prompt 里出现过的材料短编号（`m1`、`m2`…），按顺序去重。

    与 `source_ids_in` 是同一个角色，只是发现那一步的 prompt 里印的是短编号。
    假 LLM 必须引用**prompt 里真实出现过**的编号，工具才会把它们翻回真 id；
    照着 prompt 里的东西抄，正是这条测试要模拟的行为。
    """
    seen: list[str] = []
    for match in _MATERIAL_ALIAS_RE.finditer(prompt):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def fact_ids_in(prompt: str) -> list[str]:
    return _FACT_ID_RE.findall(prompt)


def candidate_ids_in(prompt: str) -> list[str]:
    return _CANDIDATE_ID_RE.findall(prompt)


def decision(action: str, *, reason: str = "因为这是此刻最该做的一步", target=None, **arguments) -> dict:
    """Planner 的一条决策。"""
    return {"action": action, "reason": reason, "target": target, "arguments": arguments}


class ScriptedLLMService:
    """按 prompt 分派、planner 按脚本走的假 LLM。

    `decisions` 是 planner 的**逐次**返回值（Agent 的自主性正体现在这里：
    测试要能精确控制每一步选什么，否则验不了路由与预算）。
    `responses` 是其他步骤的返回值，值可以是 JSON 字符串、dict、异常实例、
    callable(prompt) -> str（用于"引用 prompt 里真实出现的 id"这类场景），
    或者**列表**——列表表示按调用次序取值，用于同一个步骤在不同轮次要有不同
    回答的场景（例如 Critic 先要求改稿、改完再放行）。

    脚本用完还要再调 planner 时**直接抛错**，而不是返回一个默认值：
    "多调了一次 planner"往往意味着预算或终止条件出了 bug，静默兜底会把它藏起来。
    """

    def __init__(self, *, decisions=(), responses=None, default=None):
        self.decisions = list(decisions)
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[dict] = []
        self._planner_calls = 0
        # 列表型返回值 = 按次序消费的队列（见类文档）
        self._queues = {
            kind: list(value)
            for kind, value in self.responses.items()
            if isinstance(value, list)
        }

    def kind_of(self, prompt: str) -> str:
        for kind, marker in PROMPT_MARKERS.items():
            if marker in prompt:
                return kind
        raise AssertionError(f"无法识别这是哪个 prompt：{prompt[:120]!r}")

    def planner_calls(self) -> int:
        return self._planner_calls

    async def complete(self, prompt: str, *, json_mode: bool = False) -> str:
        kind = self.kind_of(prompt)
        self.calls.append({"kind": kind, "prompt": prompt, "json_mode": json_mode})

        if kind == "planner":
            if self._planner_calls >= len(self.decisions):
                raise AssertionError(
                    f"planner 被调用了第 {self._planner_calls + 1} 次，"
                    f"但脚本只给了 {len(self.decisions)} 条决策"
                )
            value = self.decisions[self._planner_calls]
            self._planner_calls += 1
        elif kind in self._queues:
            queue = self._queues[kind]
            if not queue:
                raise AssertionError(f"{kind} 的脚本队列已经用完")
            value = queue.pop(0)
        else:
            value = self.responses.get(kind, self.default)
            if value is None:
                raise AssertionError(f"没有为 {kind} 配置脚本返回值")

        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(prompt)
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


# --- 各步骤的默认返回值 ---
#
# 全部写成 callable(prompt) -> str：内容里要引用的 id 只能从 prompt 里读，
# 因为它们是运行时生成的。这也顺带验证了一件事——工具**确实把 id 印进了 prompt**。
# 假 LLM 若在 prompt 里找不到 id，会返回空引用，测试立刻失败。


def topics_payload(count: int = 5):
    """发现阶段的假聚类结果。

    **第一组引用全部材料**：`SAMPLE_X_RESPONSE` 的两条帖子来自两位不同作者，
    于是第一组同时满足"作者数 ≥2、可核验来源 ≥2"，能真的被判成 qualified；
    其余各组只引用最后一条（单一作者），会被单一来源上限压住、判成 weak_support。

    这个不平均是刻意的：它让一条 fixture 同时覆盖"达标 / 不达标"两条支路，
    也让 `select_topic` 该选哪个有唯一正确答案——全是同分的话，
    "选对了"就退化成"碰巧排序站在这一边"。
    """
    def build(prompt: str) -> str:
        # 发现那一步的 prompt 印的是 m1/m2… 短编号，不是 UUID。
        ids = material_aliases_in(prompt)
        topics = [
            {
                "title": f"候选选题 {i}",
                "description": f"第 {i} 组材料在讲同一件事",
                "keywords": [f"关键词{i}"],
                "why_now": "刚刚发生，讨论还在早期",
                "unknowns": [f"还没查到第 {i} 组的某个数字"],
                "source_ids": ids[:2] if i == 1 else ids[-1:],
            }
            for i in range(1, count + 1)
        ]
        return json.dumps({"topics": topics}, ensure_ascii=False)

    return build


def evidence_payload_with_context(context=None):
    """证据包 + 话题背景。

    `context` 原样写进返回体（`None` 就不带这个键，用来覆盖"模型漏了背景"那条路径）：
    与 `evidence_payload(*extra_facts)` 注入坏事实同一个用意——测试要能精确控制
    模型返回到字段这一级，才验得了工具是不是真的在清洗它。
    """

    def build(prompt: str) -> str:
        ids = source_ids_in(prompt)
        body = {
            "facts": [{"fact": "事实一：有明确出处", "source_ids": ids[:1]}],
            "consensus": [],
            "counterpoints": [],
            "unknowns": [],
        }
        if context is not None:
            body["context"] = context
        return json.dumps(body, ensure_ascii=False)

    return build


def evidence_payload(*extra_facts):
    """证据包。额外的 fact 直接写进返回体（用来注入编造出处的坏数据）。"""

    def build(prompt: str) -> str:
        ids = source_ids_in(prompt)
        facts = [{"fact": "事实一：有明确出处", "source_ids": ids[:1]}]
        if len(ids) > 1:
            facts.append({"fact": "事实二：有明确出处", "source_ids": ids[1:2]})
        facts.extend(extra_facts)
        return json.dumps(
            {
                "facts": facts,
                "consensus": ["材料之间互相印证的说法"],
                "counterpoints": ["与主流相左的说法"],
                "unknowns": ["查不到的地方"],
            },
            ensure_ascii=False,
        )

    return build


def angles_payload(count: int = 3, *, rich_first: bool = True):
    """观点草稿——Phase 5B.3 的契约，**没有任何分数字段**。

    `evidence_ids` 只能从 prompt 里读（fact_id 是工具在运行时赋的），这同时验证了
    工具**确实把 fact_id 印进了 prompt**：假 LLM 若找不到 id，会交回空引用，
    `generate_angles` 会把整条草稿丢掉，测试立刻失败。

    两处刻意的设计：

    1. 三句话必须**真的不同**。5B.3 会用 n-gram 检测同批观点的互相重复，
       三条只差一个字的"观点"会被全判成同一个观点（novelty 全归零），
       于是"三条候选"退化成一条被数了三遍。
    2. 第一条多绑一条事实。它于是是分数最高的那个，"选中的是 a1"才有唯一
       正确答案，而不是"碰巧字典序站在这一边"。
    """

    claims = (
        "定价权正在从卖方转向买方",
        "默认的计费方式正在被改写",
        "真正被改变的是采购决策",
    )

    def build(prompt: str) -> str:
        fids = fact_ids_in(prompt)
        return json.dumps(
            {
                "angles": [
                    {
                        "claim": claims[i % len(claims)],
                        "reasoning": "两家在同一周做了同一件事，且都指向企业客户。",
                        "why_it_matters": "它改变了默认做法",
                        "evidence_ids": fids[:2] if (i == 0 and rich_first) else fids[:1],
                        "risk": "可能把一次促销读成一次转向",
                        "counterpoints": ["也可能只是季度促销"],
                        "counterpoint_evidence_ids": fids[:1],
                        "assumptions": ["两家调整的不是同一类套餐"],
                        "implications": ["采购方会重新谈价"],
                        "claim_scope": "segment" if i == 0 else "case",
                        "stance": "cautious",
                        "novelty_note": "把两家的动作连成一条线来看",
                    }
                    for i in range(count)
                ]
            },
            ensure_ascii=False,
        )

    return build


def candidates_payload(count: int = 3, content: str = "这是一条候选正文。"):
    # 与 `prompts/agent/candidates.txt` 要求的三条路一致（`ROLE_ORDER`）。
    # label 不参与任何分支，只是让假数据与真实契约的形状对得上——
    # 对不上的话，"Critic 按 label 判有没有走对路"这类测试就会假通过。
    labels = ["鲜明观点型", "事实与反差型", "讨论与不同视角型"]

    def build(prompt: str) -> str:
        return json.dumps(
            {
                "candidates": [
                    {"label": labels[i % len(labels)], "content": f"{content}（第 {i + 1} 条）"}
                    for i in range(count)
                ]
            },
            ensure_ascii=False,
        )

    return build


def critic_payload(decision: str = "accept", *, focus=None, score: float = 8.0,
                   revision_targets=("第一句改成直接给结论",), context_fit=None):
    """整批候选的评价。

    默认让**每一条**候选都得到同一个结论——这样"聚焦到哪一条"就由
    `focus` 或内部规则决定，测试不必依赖字典序之类的偶然。

    `context_fit` 给了就写进返回体（没有这个键时输出与加它之前**逐字相同**，
    盯住这段的旧测试不受影响）。
    """

    def build(prompt: str) -> str:
        cids = candidate_ids_in(prompt)
        results = []
        for cid in cids:
            entry = {
                "candidate_id": cid,
                "score": score,
                "flags": [] if decision == "accept" else ["有问题"],
                "revision_targets": list(revision_targets) if decision == "revise" else [],
                "decision": decision,
            }
            if context_fit is not None:
                entry["context_fit"] = dict(context_fit)
            results.append(entry)
        return json.dumps(
            {"results": results, "focus_candidate_id": focus or (cids[0] if cids else "")},
            ensure_ascii=False,
        )

    return build


def revise_payload(content: str = "改好的正文。"):
    return lambda prompt: json.dumps({"label": "直接观点", "content": content}, ensure_ascii=False)


class FakePublishingXService:
    """记录发布调用的假 X 服务。

    发布相关的测试必须**精确断言调用次数**：只断言返回值/状态的测试在
    "重复发推"这类 bug 面前是恒真的——发两次的返回值和发一次一模一样。
    """

    def __init__(self, *, post_id: str = "1900000000000000001", error: BaseException | None = None):
        self._post_id = post_id
        self._error = error
        self.published: list[str] = []

    async def publish(self, content: str) -> str:
        self.published.append(content)
        if self._error is not None:
            raise self._error
        return self._post_id


class StubGraph:
    """只实现 aget_state 的 Graph 替身，用来喂 final_content 给发布服务。"""

    def __init__(self, values: dict | None = None):
        self._values = values or {}
        self.state_reads = 0

    async def aget_state(self, config):
        self.state_reads += 1
        return SimpleNamespace(values=self._values)


def draft_json(
    content: str = "AI coding agents ship faster.",
    hook_type: str = "statement",
    topic: str = "AI coding agent",
) -> str:
    return json.dumps({"content": content, "hook_type": hook_type, "topic": topic})
