"""检索词能不能原样交给 X。

X 的 recent search 用自己的查询语法解析 `query`，而那条语法里 `/`、`:` 与
ASCII 括号都有特殊含义：`(` `)` 是分组运算符，`/` 与 `:` 构不成任何合法 token。
一个 URL 同时含 `/` 与 `:`，于是**拿 URL 当检索词必然 400**——真机 smoke 实测：

    GET .../search/recent?query=https%3A%2F%2Ft.co%2FRMhWA6lmX9
    400 {"message":"...no viable alternative at character '/' (at position 7)"}

而且它不是"少取几条材料"，是**整次运行到此为止**：异常从工具里抛出去，
Run 直接判失败。

## 为什么会走到"URL 当检索词"

真实检索回来的材料里有一部分正文**就是一条链接**（分享型帖子）。当这样一条
材料独自成一个候选选题时，标题自然就是那条链接；选定它之后，
`research_topic` 拿标题去检索——于是搜索一个 URL。

## 边界划在哪

把一个不能用的检索词**换掉**是语义决定（换成什么才对？），属于工具层的职责。
`app/services/x_api.py` 只管传输层的硬约束（例如 `end_time` 必须早于请求时刻
10 秒），那里没有"换成什么"的答案。所以这里只提供两件纯函数：

- `usable()` —— 这个字符串能不能原样发过去；
- `choose()` —— 在一串**有优先级的候选**里挑出第一个能用的，并说明挑的是哪个。

`choose()` 不会凭空造检索词。候选全不能用时，它退而把非法字符换成空格
（`MCP/Agent` → `MCP Agent` 是个合理的降级），并把这件事写进说明——
**"我们搜的不是你以为的那个词"必须能从 summary 里看出来**。
"""

from collections.abc import Sequence

#: 会让 X 判成非法查询的字符。`(` `)` 是查询语法里的分组运算符，
#: `/` 与 `:` 在查询里不构成任何合法 token（URL 一定同时带这两个）。
UNUSABLE_CHARS = ("/", ":", "(", ")")

#: 候选全部不能用时的兜底替换：非法字符 → 空格。
_REPLACEMENT = " "


def usable(text: str) -> bool:
    """这个字符串能不能原样作为 X 的检索词。

    空（或只有空白）也算不能用：空 query 同样会被 X 判成参数非法。
    """
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return False
    return not any(char in collapsed for char in UNUSABLE_CHARS)


def degrade(text: str) -> str:
    """把非法字符换成空格，并压掉重复空白。

    这是**最后手段**：只在所有候选都不能用时才走。它不生成新词，
    只把不能用的字符拿掉——`MCP/Agent` 变成 `MCP Agent` 是有意义的降级，
    而一条 URL 会变成一串无意义的碎片。所以调用方必须把"降级过"报出去。
    """
    for char in UNUSABLE_CHARS:
        text = text.replace(char, _REPLACEMENT)
    return " ".join(text.split())


def choose(options: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """按优先级挑出一个能交给 X 的检索词。

    `options` 是 `(说明, 候选文本)` 的序列，**顺序就是优先级**——与调用方原本
    的 if/elif 链一一对应，因此这个函数不改变任何一条既有的优先级规则。

    返回 `(检索词, 说明)`：

    - 找到能用的候选 → 用它，说明是那个候选自己的说明；
    - 候选都含非法字符 → 用**第一个非空候选**降级后的结果，说明里写明降级
      （不能让 summary 看起来像"按题目检索了"，实际搜的是别的东西）；
    - 候选全为空 → `("", 说明)`。调用方拿它去检索一定会失败，但那种情况
      （目标与题目都为空）本来就没有合理的兜底可言。
    """
    first_filled: tuple[str, str] | None = None
    for label, text in options:
        if not (text or "").strip():
            continue
        if first_filled is None:
            first_filled = (label, text)
        if usable(text):
            return " ".join(text.split()), label

    if first_filled is None:
        return "", "没有可用的检索词"

    label, text = first_filled
    return degrade(text), f"{label}（原文本含 X 不接受的字符，已替换为空格）"
