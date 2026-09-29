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
    ):
        self._raw = raw if raw is not None else SAMPLE_X_RESPONSE
        self._error = error
        self._post_id = post_id
        self._post_error = post_error
        self.created: list[str] = []
        # 每次检索的 (query, max_results, start_time, end_time)。
        # 扫描窗口是 Phase 5B.1 的一等公民（"最近"必须有定义），所以测试要能
        # 断言它确实被传下去了，而不是被悄悄丢掉。
        self.searches: list[dict] = []

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
        ids = source_ids_in(prompt)
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


def angles_payload(count: int = 3):
    def build(prompt: str) -> str:
        fids = fact_ids_in(prompt)
        return json.dumps(
            {
                "angles": [
                    {
                        "claim": f"观点 {i}：这件事的真正意义被低估了",
                        "why_it_matters": "它改变了默认做法",
                        "evidence_ids": fids[:1],
                        "risk": "可能过度解读",
                        "confidence": round(0.8 - i * 0.1, 2),
                    }
                    for i in range(1, count + 1)
                ]
            },
            ensure_ascii=False,
        )

    return build


def candidates_payload(count: int = 3, content: str = "这是一条候选正文。"):
    labels = ["直接观点", "信息密集", "反共识"]

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
                   revision_targets=("第一句改成直接给结论",)):
    """整批候选的评价。

    默认让**每一条**候选都得到同一个结论——这样"聚焦到哪一条"就由
    `focus` 或内部规则决定，测试不必依赖字典序之类的偶然。
    """

    def build(prompt: str) -> str:
        cids = candidate_ids_in(prompt)
        results = [
            {
                "candidate_id": cid,
                "score": score,
                "flags": [] if decision == "accept" else ["有问题"],
                "revision_targets": list(revision_targets) if decision == "revise" else [],
                "decision": decision,
            }
            for cid in cids
        ]
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
