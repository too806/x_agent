"""Style & Structure 的**离线**基准（Phase 5B.2 工作单第二十七节）。

## 它测什么

工作单第二十六、二十七节列了四件事，全部是"同一输入必须给同一输出"类的
确定性断言，因此可以完全离线、逐位复现：

1. `style_corpus_integrity` —— 语料本身是合成的、完整的、字段齐全的；
2. `sample_has_required_fields` —— 每条样本的必填字段都在，id 不重复；
3. `style_profile_deterministic` —— **同一份 corpus 必然得到同一个 Profile**（Smoke 5）；
4. `recipe_is_structured` —— recipe 是一组结构化约束，不是一段正文。

## 它不碰什么

**不碰真实 X，也不碰 LLM。** 语料是固定写死的（`corpus.py`），标注是写死的，
所以整份基准在 CI 里跑不需要网络、不需要密钥、不需要数据库，也不会有
"今天红了明天绿了"的抖动。

## 它额外守的一条

工作单第四十节禁止"大段原文进入 Writer prompt"。这条在生产链路里由
`app/style/recipe.py` 的设计保证（recipe 里没有样本字段），但设计保证
不等于没被改坏。所以这里把**渲染出来的 Writer prompt** 与语料逐 n-gram 比对：
只要有人把样本正文接进 recipe、或让 prompt 引用样本，最长公共片段就会超过阈值，
基准立刻红。这是把一条"禁止"变成机器守着的规则的唯一办法。

## 单向依赖

与 `evals/` 其余部分一致：这里只 import `app/`，`app/` 从不 import 这里。
"""

from evals._bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

__all__ = ["corpus"]
