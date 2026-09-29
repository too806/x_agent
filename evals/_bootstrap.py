"""让 `python evals/xxx.py` 能 import 到 `app` 与 `evals`。

为什么需要它：直接执行 `python evals/run_benchmark.py` 时，`sys.path[0]` 是
`evals/` 而不是仓库根，于是 `import app.agent.schemas` 与 `import evals.model`
都会失败。两个 import 都需要仓库根在 path 上。

做法是在每个入口脚本的**最前面** import 本模块，由它把仓库根插到 `sys.path`。
不装包、不改 pyproject.toml（那超出本会话的文件边界），因此这是唯一可行的方式。

幂等：重复 import 不会重复插入。
"""

import sys
from pathlib import Path

# __file__ = <repo>/evals/_bootstrap.py → parents[1] = <repo>
REPO_ROOT = Path(__file__).resolve().parents[1]


def ensure_repo_root_on_path() -> Path:
    """把仓库根放到 sys.path 最前，并返回它。

    放最前而不是追加：万一环境里装了同名的 `evals` 或 `app` 包，
    我们要的是仓库里这一份——评估对象是**这个**仓库的 Agent。
    """
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def force_utf8_stdout() -> None:
    """把 stdout/stderr 切成 UTF-8。

    本仓库的报告全是中文，而 Windows 控制台的默认编码不保证是 UTF-8
    （cp437 下会直接 UnicodeEncodeError 崩掉，而不是显示乱码——更难查）。
    `errors="replace"` 是刻意的：宁可某个字变问号，也不要让整份评估报告
    因为一个字符而打不出来。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - 平台相关
                pass


ensure_repo_root_on_path()
