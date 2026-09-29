# 内容质量

行为评估回答"Agent 做得对不对"，内容质量回答"写出来的东西好不好"。

两者不能互相替代。一次行为完全合规的运行——步数在预算内、评审通过、
进了人工审核、没有自动发布——照样可以产出一篇套模板的空话。
约束住流程不等于约束住产出。

**本阶段只定义评估，不接入生产。** 没有哪条内容质量指标会阻塞发布，
也没有哪条会让 Run 失败。

---

## 一条贯穿全文的纪律

> 自动指标负责**筛**，人工标注是**真值**。两者并置，不互相替代。

`evals/content/ai_style.py` 里的每个指标都是**确定性**的：同样的正文进去，
同样的数字出来，不调用任何模型。所以它们**可复现、可回归测试、可在 CI 里跑**。

但也正因为确定性，它们只看得见**形式**：套话短语、句长分布、数字和专有名词的
密度。它们看不见"这个观点是不是站得住"。后者只有人能给。

系统里没有任何地方让 LLM 给自己的输出打质量分。那样做得到的数字既不可复现
也不权威，而且会系统性地偏高。

---

## 八个维度

| 维度 | 定义 | 自动可测的部分 | 只有人能给的 |
|---|---|---|---|
| 具体性 | 有没有具体的人、事、数字、时间 | 数字计数、具体实体计数与密度 | 这些具体项是否**切题** |
| 立场 | 有没有明确主张，而不是罗列 | （无） | 主张是否成立、是否有争议价值 |
| 证据绑定 | 主张能否追溯到证据 | `evidence_traceability`（RULE-006） | 引用是否**支撑**该主张 |
| 泛化语言 | 套话、模糊限定语的密度 | 套话短语命中、hedge 计数 | 命中是否真的算套话 |
| 模板相似度 | 与"统一模板"的相似程度 | 重复片段、重复开头、句长方差 | 整篇是否读起来像填空 |
| 钩子强度 | 开头是否真的抓人 | （无） | 全部 |
| 信息密度 | 每句话是否都在提供信息 | 句长、实体密度 | 信息是否**服务于判断** |
| 人工编辑距离 | 人工改了多少 | 归一化 Levenshtein（有改写稿时） | 改动是**改好**还是改坏 |

### 自动指标清单（`evals/content/ai_style.py`）

```python
AiStyleReport
  char_count, sentence_count
  avg_sentence_length, sentence_length_variance, sentence_length_stdev
  generic_phrase_count, generic_phrases[], hedge_count
  concrete_entity_count, concrete_entities[], number_count, concrete_entity_density
  question_count, exclamation_count
  repeated_pattern_count, repeated_patterns[], repeated_opener_count
  abstract_noun_density_approx        # ← 标记为近似
  human_ai_style_rating               # ← 永远是 unavailable
```

### 人工维度清单（`ContentQualityRecord`）

八个维度全部是 `float | None = None`：

```
specificity, stance, evidence_binding, generic_language,
template_likeness, hook_strength, information_density, human_edit_distance
```

`None` 表示**没人标注过**，与 `0.0`（标注为最差）是两件完全不同的事。
报告里前者显示 `unavailable`，并且列进 `unannotated`——
这样"这份报告还缺多少人工输入"是一眼可见的。

---

## 几个刻意的设计

### `human_ai_style_rating` 永远 unavailable

这个字段放在 `AiStyleReport` 上（而不是只放在标注记录里），但**本模块永不填它**。

放这里是为了让"这个位置该有一个人工评分"出现在**每一份**自动报告上，
而不是只出现在有人记得去填的地方。一个空位比一个不存在的空位更容易被发现。

### 近似指标被显式标记

`abstract_noun_density_approx` 是文件名里带 `_approx` 的唯一一个，并且登记在
`APPROXIMATE_METRICS` 里。中文的抽象名词识别没有可靠的确定性做法，这个指标用
后缀启发式近似——它**会错**。标记出来是为了让读者知道该给这个数字多少信任，
而不是让它混在其它确定性指标里显得同样可靠。

### 编辑距离用归一化的

`human_edit_distance` 是归一化的 Levenshtein 距离（0 = 一字未改），不是原始
编辑次数。原因是原始次数随文本长度增长：改 20 个字，在 100 字的稿子里是重写，
在 2000 字的稿子里是微调。不归一化的话，这个数字在跨文章比较时没有意义。

**越高越差**——它衡量的是人工介入的程度。但这个方向有个前提：内容质量的
自动化目标是"人工改动量下降**而**质量不下降"。只看这一个数字会被
"Agent 学会写让人不想改的废话"骗过去，所以它必须和上面八个维度一起看。

### 自动指标与人工维度不交叉填充

`ContentQualityReport` 里 `automatic` 与 `dimensions` 是两个独立的字典。
不存在"人工没标就用自动指标顶上"的逻辑。

理由和 `unavailable` 那条纪律同源：一旦允许顶替，`dimensions.specificity`
就变成了一个有时来自人、有时来自启发式的数，而**从值上完全看不出来**。
用它做跨运行比较时，比较的可能是两把不同的尺子。

### 没有 gate，没有综合分

`ContentQualityReport` 里没有任何门禁，也没有把八个维度合成一个分数的字段。

内容质量是给人看的**判断依据**，不是自动拦截的依据。把"模板相似度 > 0.6 就
拒绝发布"写进系统，会立刻诱导出"把模板相似度压到 0.6 以下"的优化方向——
而那个方向与"写得好"不是一回事。

---

## 怎么用

```python
from evals.content.quality import build_report

report = build_report(
    draft_text,
    annotation=record.content_quality,   # 可选：人工标注记录
    edited=human_edited_text,            # 可选：人工改写稿
)

report.automatic       # 确定性指标
report.dimensions      # 人工维度（未标注的是 unavailable）
report.unannotated     # 还没人标的维度
report.human_edit_distance
```

`evals/fixtures/accepted_candidate.json` 里带了一份编出来的标注，
用来演示"有人工真值时长什么样"。它的 `note` 里写明了
**这组数字是编的，不要当成模型质量的证据**。

---

## 现状

**所有 fixture 的人工维度都是空的。** 这是有意的：

- 自动指标可以先跑起来，它们不需要人；
- 人工维度需要有人真的去读、去标，这是一个必须由人发起的过程，
  不该被一次技术实现假装完成。

所以现在跑 `--dataset`，内容质量部分的输出应该如实显示
`dimensions` 全为 `unavailable`、`unannotated` 列出全部八项。
**看到这个结果是正确的，不是缺陷。**
