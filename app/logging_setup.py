"""后端日志的**唯一**初始化位置。

为什么值得单独一个模块：2026-10-06 调查一条真实 X 帖子的来源时，能拿到的
证据只有数据库时间线——因为 `logging.basicConfig` 没挂 FileHandler，日志全进
后端 cmd 窗口的 stderr，而 `start.bat` 用 `cmd /k` 开窗、不重定向，**进程一退
（或被重启）滚动缓冲区就没了**。那次只能靠 `auto_review.acted` 反推"不是自动化
发的"，却回答不了"是谁发的"。

所以这里补上两件事：日志落盘 + 轮转。刻意只用标准库
（`logging.handlers.RotatingFileHandler`）：项目禁第三方依赖，日志不该是例外。

**`to_file` 默认 False**：只有真正的启动入口（`run.py`）才打开落盘。
`app.main` 是被 pytest 直接 import 的（见 `tests/support.py`），如果 import
就写文件，跑一次测试就会在仓库里建出 `logs/backend.log`，把生产日志和测试日志
混在一起——事后复盘时最不该发生的就是"分不清哪条是测试造的"。
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: 覆盖日志目录的环境变量。临时环境 / 测试用它指向别处。
LOG_DIR_ENV = "XAGENT_LOG_DIR"

LOG_FILE_NAME = "backend.log"

#: 5 MiB × 5 份 = 上限约 25 MiB。够覆盖一次事故复盘的全部上下文，
#: 又不会让 logs/ 无限长大。
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

#: 带时间戳。原来 `basicConfig` 的默认格式没有时间，事后只能靠文件 mtime 猜顺序。
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: 挂在 handler 上的标记，记住"这个 handler 是本模块加的、指向哪个文件"。
#: 幂等的依据——重复调用不能挂第二个 handler，否则每条日志会被写两遍。
_FILE_HANDLER_ATTR = "_xagent_log_file"


def default_log_dir() -> Path:
    """日志目录：默认是仓库根下的 `logs/`，可用 `XAGENT_LOG_DIR` 覆盖。"""
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "logs"


def configure_logging(
    *,
    log_dir: str | os.PathLike[str] | None = None,
    level: int = logging.INFO,
    to_file: bool = False,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
) -> Path | None:
    """配置 root logger：始终保留 console，`to_file=True` 时再落盘并轮转。

    返回实际使用的日志文件路径；未落盘时返回 `None`。

    幂等：同一条路径重复调用只会挂一个 handler。但**不同**目录会各挂一个——
    测试用临时目录时不能被当成"已经配过了"而跳过，否则文件永远建不出来。
    """
    root = logging.getLogger()
    if not root.handlers:
        # 只在 root 还没有 handler 时才 basicConfig：pytest 会先给 root 装捕获
        # handler，这里再加一个 console 只会让每条日志被打印两遍。
        logging.basicConfig(level=level, format=LOG_FORMAT)
    root.setLevel(level)

    if not to_file:
        return None

    target = Path(log_dir) if log_dir is not None else default_log_dir()
    # 目录不存在时自动创建。日志是"事后必须有"的东西，不能因为少一个目录
    # 就在启动时静默丢掉——那正是这次要修的问题。
    target.mkdir(parents=True, exist_ok=True)
    path = target / LOG_FILE_NAME
    marker = str(path.resolve())

    for handler in root.handlers:
        if getattr(handler, _FILE_HANDLER_ATTR, None) == marker:
            return path

    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        # 日志里有中文；不指定编码会走 Windows 默认的 cp936，中文可能直接
        # 抛 UnicodeEncodeError 把一次请求打成 500。
        encoding="utf-8",
        # 首次写入才真正打开文件：文件被另一个进程占着（比如前一任后端还没退干净）
        # 也不至于让新进程起不来。
        delay=True,
    )
    setattr(handler, _FILE_HANDLER_ATTR, marker)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    return path
