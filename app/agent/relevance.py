"""候选选题与用户目标的相关性闸门（确定性，无 LLM、无 IO）。

## 这个模块解决一个真实失败

Run `20ab360c` 的目标是「告诉 Agent 今天想关注香港」。Planner 发出的检索词是
`香港 热点 新闻 讨论`，X 回来的三条材料分别是 @hhp121222 的 FF 发布会、
@CheckingDog 的越狱提示词、@JohnSonupop 的一条纯链接——**三条里没有任何一条
出现过「香港」**。系统照旧把它们聚成了两个候选（t1 40.0 分、t2 26.3 分），
`qualified = 0`，然后按"分数最高的"选了 t1，写成了一篇关于贾跃亭的分析。

问题不在打分：`trends.py` 的资格判定从头到尾没有"目标"这个概念，它只看材料
自身的作者数/来源数/时间/互动。所以一个与目标毫无关系的簇，只要结构上够热闹，
就能拿到同样的分数、走上同样的路。

## 判定方式：目标是实词，材料必须真的出现过

不用 LLM 判"相不相关"——问模型"这跟香港有什么关系"，它一定会写出一段
「香港是国际金融中心，而贾跃亭是中概股人物……」的多跳推理来讨好提问者，
而那正是要禁止的东西（工作单第四节：多跳推导不能当目标匹配）。

这里用的是最笨、最不可操纵的一条：**把目标里携带话题的词抽出来，要求候选的
材料正文里真的出现过**。模型给出的标题、描述**不参与判定**——标题是模型对
"这簇在讲什么"的声明，而"声明相关性"正是要防的动作。

## 什么时候不判定（fail open）

目标里一个话题词都抽不出来时（"帮我找个有意思的"），或者目标本身就是一句话
委托而实词全被拆掉时，**不判定、全部放行**，并在结论里写明 `checked=False`。
放行的理由不是"它相关"，而是"这次没有依据可以否定它"——把判不出来当成不相关，
会让"目标写得随便一点"直接变成"什么都发现不了"，而那不是用户要的。

放行的代价（模型可以把一个远相关说成相关）写在 `goal_relevance_gate` 的注释里，
不假装这个闸门能证明相关性：它是一个**地板**，不是一张证明。

## 抽词规则

1. 去掉"委托用语"：目标是一句对 Agent 说的话，`今天/帮我/关注/一个/话题/值得/
   讨论/并/形成/一条` 这些词不携带任何话题内容。它们被当作**分隔符**（不是
   简单删除），所以「今天想关注香港」会切成 `想` / `香港` 两段，而不是剩下
   一个读不出边界的 `想香港`。
2. 「告诉 Agent」「让你」这类"对着 Agent 说话"的开头整段去掉：否则 `agent`
   会变成一个话题词，而材料讲 AI Agent 时就会被误判成"与目标相关"。
3. 切出来的段：ASCII 词要求长度 ≥2；中文段取**整段**加上它的 2-gram。
   取 2-gram 是因为中文没有空格，「香港资本市场」整段几乎不可能在另一句话里
   逐字出现，只留整段等于几乎不判定。代价是"资本市场"这类较长的词会命中
   只讲资本市场（不含香港）的材料——这是**放行方向**的误差，与"宁可继续找"
   一致，写在这里以备核对。
"""

import re

#: 对着 Agent 说话的动词 + 称谓，整段去掉（含空格）。
#:
#: 只去掉"动词 + 你/我/Agent"这种组合，不去掉单独的 `agent`：「找一个 AI Agent
#: 话题」里的 agent 是话题本身，去掉它就把用户真正想看的东西删了。
_ADDRESSING = re.compile(
    r"(告诉|让|请|叫|给|帮|麻烦)\s*(我|你|咱|它|他|她|agent|Agent|AI\s*Agent)+",
    re.IGNORECASE,
)

#: 委托用语与功能词：不携带话题内容，只当分隔符。
#:
#: 全部 ≥2 字。单字词（在、的、想、来、找）**刻意不收**：它们会切开
#: 「理想」「未来」「存在」这类实词，把话题切碎成无法核对的碎片。
_NON_TOPIC = (
    "今天",
    "明天",
    "最近",
    "近期",
    "这周",
    "本周",
    "帮我",
    "给我",
    "找一下",
    "找个",
    "找一个",
    "找几个",
    "一个",
    "几个",
    "一些",
    "有点",
    "值得",
    "值得看",
    "讨论",
    "聊聊",
    "关注",
    "看看",
    "想知道",
    "了解",
    "话题",
    "主题",
    "题目",
    "内容",
    "帖子",
    "中文帖子",
    "并形成",
    "形成",
    "一条",
    "尽量",
    "简单些",
    "简单一点",
    "明确",
    "观点",
    "关于",
    "相关",
    "有关",
    "方面",
    "方向",
    "事情",
    "东西",
    "什么",
    "怎么",
    "如何",
    "可以",
    "希望",
    "想要",
    "要求",
    "需要",
)

#: 分隔符：标点、空白、以及上面那些功能词。长词优先（正则交替按顺序尝试，
#: 所以 "找一个" 必须排在 "一个" 前面）。
_SEPARATOR = re.compile(
    r"[\s，。、；：！？（）【】《》「」『』“”‘’·,.;:!?()\[\]{}<>\"'/\\|*~—\-]+"
    r"|"
    + "|".join(re.escape(word) for word in sorted(_NON_TOPIC, key=len, reverse=True)),
    re.IGNORECASE,
)

#: 话题词的最短长度。**只对 ASCII 词生效**：中文按 2-gram 切，每片天然是 2 字。
#:
#: 1 个字符的 ASCII 词（`X`、`a`）不构成话题：`X` 是平台名，让它当话题词意味着
#: 任何一条带 X 的材料都算"与目标相关"。
MIN_TERM_LENGTH = 2

#: 中文最短片长。1 字的碎片（"想"、"圈"）不构成可核对的话题。
MIN_CJK_TERM_LENGTH = 2


def _is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def goal_terms(goal: str) -> tuple[str, ...]:
    """目标里携带话题的词。抽不出来时返回空元组（调用方据此 fail open）。

    返回顺序保持"在目标里出现的先后"，方便人核对；重复的只留第一次。
    """
    text = _ADDRESSING.sub(" ", goal or "").lower()
    terms: list[str] = []
    for chunk in _SEPARATOR.split(text):
        token = "".join(chunk.split())
        if not token:
            continue
        if _is_ascii(token):
            if len(token) >= MIN_TERM_LENGTH:
                terms.append(token)
            continue
        if len(token) >= MIN_CJK_TERM_LENGTH:
            # 整段 + 全部 2-gram。整段留着是为了让"逐字出现"这种最强证据优先被
            # 命中（reason 里会说明命中的是哪一个），2-gram 是为了中文没有空格。
            terms.append(token)
        if len(token) > MIN_CJK_TERM_LENGTH:
            terms.extend(
                token[index : index + MIN_CJK_TERM_LENGTH]
                for index in range(len(token) - MIN_CJK_TERM_LENGTH + 1)
            )
    return tuple(dict.fromkeys(terms))


def matched_terms(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    """`text` 里真的出现了哪些话题词（逐字，ASCII 不区分大小写）。

    返回命中的词本身而不是布尔值：写进 summary 之后，"凭什么判断它相关"
    是可以逐字核对的，而不是一句"系统认为相关"。
    """
    haystack = (text or "").lower()
    if not haystack:
        return ()
    return tuple(term for term in terms if term in haystack)


def judge(material_text: str, terms: tuple[str, ...]) -> tuple[bool | None, str]:
    """判定一批材料与目标的相关性。返回 `(相关与否, 为什么)`。

    `None` 表示**不判定**（目标里没有可核对的话题词）：不是"相关"，也不是
    "不相关"，而是"这次没有依据"。调用方必须把这个区别带到 summary 里，
    否则界面上"没判定"和"判定为相关"长得一模一样。

    判定的输入只有**材料正文**：候选的标题与描述是模型写的，让它们参与判定，
    等于允许模型用一句"香港科技圈本周热点"把一批无关材料说成相关（工作单第
    四节要禁止的正是这个）。
    """
    if not terms:
        return None, "目标里没有可核对的话题词，本次不判定相关性"
    hits = matched_terms(material_text, terms)
    if hits:
        return True, f"材料里出现目标词：{'、'.join(hits)}"
    if not (material_text or "").strip():
        return False, "材料里没有正文（只有链接），没有任何目标词可核对"
    return False, f"材料里没有出现任何一个目标词（{'、'.join(terms[:6])}…）"
