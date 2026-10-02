"""Learning Readiness 的**离线**基准（Phase 5C 工作单第二十六至二十九节）。

## 它测什么

这一层的产物全部是**确定性函数**（`app/learning/`），所以整份基准可以离线、
逐位复现地断言：同一批记录两次跑出**逐字节相同**的报告。工作单第二十七节
点名了九类数据，`fixtures.py` 一组一个，`selftest.py` 逐条核对它们的预期。

## 它真正守的是什么

不是"干净数据能通过"，而是**五类误读必须被拒绝**（工作单第二十八节）：

    n=1             → 不许产出建议 / 结论
    不同窗口        → 不许直接比较
    NULL            → 不许当成 0
    correlation     → 不许写成 causation
    小样本          → 不许 learning_ready

一个恒真的守卫比没有守卫更糟——它让人以为那条禁令被守住了。所以每条检查都
配一条**反证**：把触发条件改回去，结论必须跟着变。

## 它不碰什么

**不碰真实 X、不碰 LLM、不读数据库、不取挂钟时间。** 记录是手写的合成数据，
所以这一套在 CI 里跑不需要网络、不需要密钥、不需要数据库，也不会有
"今天红了明天绿了"的抖动。真机那半分在 `scripts/smoke_5c.py`。

## 单向依赖

与 `evals/` 其余部分一致：这里只 import `app/`，`app/` 从不 import 这里。
"""

from evals._bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

__all__ = ["fixtures", "selftest"]
