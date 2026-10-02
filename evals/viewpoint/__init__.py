"""Viewpoint Intelligence 的**离线**基准（Phase 5B.3 工作单第三十二节）。

## 它测什么

工作单第十一、十二节把"观点是怎么被评出来的"钉死在**可复算**上：

> 不要让 LLM 单独决定最终 confidence。
> 不要：LLM：这个观点 9.4 分 但无法复算。

于是这一层的对象全部是**确定性函数**（`app/agent/viewpoint.py`），可以完全
离线、逐位复现地断言。工作单第三十二节点名了七类数据：

    strong_evidence / weak_evidence / conflicting_evidence / single_source
    / counterpoint_present / overclaim / prediction

每类都带**明确的预期通过 / 预期失败**（写进 `cases.py` 的 `expect`），
不用"看起来还行"当 CI gate。

## 它不碰什么

**不碰真实 X，也不碰 LLM。** 证据包与观点草稿都是手写的合成数据，所以整份
基准在 CI 里跑不需要网络、不需要密钥、不需要数据库，也不会有"今天红了明天
绿了"的抖动。真机那半分在 `scripts/smoke_5b3.py`。

## 它额外守的两条

1. **每条检查都带一条反证**。一个恒真的守卫比没有守卫更糟——它让人以为
   那条禁令被守住了。所以每个检查都同时验证"改了那一项输入之后结论会变"，
   照 `evals/style/selftest.py::check_the_shared_span_detector_actually_fires`。
2. **`confidence` 里没有 novelty**。工作单第十五节是硬要求，这里直接断言
   权重表本身（而不只是"某条数据算出来没受影响"）。

## 单向依赖

与 `evals/` 其余部分一致：这里只 import `app/`，`app/` 从不 import 这里。
`scope.py` 里的检测器同时被 `scripts/smoke_5b3.py` 复用——真机与离线基准
查的必须是同一件事，否则"离线全绿、真机有问题"会变成一句空话。
"""

from evals._bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

__all__ = ["cases", "scope"]
