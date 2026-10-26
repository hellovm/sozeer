"""pytest 共享环境：必须在任何测试模块 import apps.api.* 之前生效。

历史坑：test_v141 顶部 import apps.api.models（注册表）发生在收集期，
若彼时环境变量已被 test_v140 的硬赋值覆盖，config.DATA_DIR 就指向
v140 的临时目录，test_core::test_data_dir_isolated 必挂。conftest
由 pytest 最先加载，在这里一次性定死隔离目录，各测试文件一律
setdefault（不再各自硬赋值）。
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="nf_test_")
os.environ["NOVELFORGE_DATA"] = _TMP
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-for-pytest-only")

# ★★★ v1.0-E4.11：排除 test_import.py（它是"手工跑的冒烟脚本"，不是单测）
#
# 根因（实测踩坑）：该脚本模块级执行 `SRC = sys.argv[1] if len(sys.argv) > 1`
# —— pytest 收集期 sys.argv[1] 是 **pytest 自己的参数**（如 tests/test_import.py），
# 于是它把 **测试文件路径当小说目录导入**，产出 0 章的书 → chs[11] IndexError，
# 收集直接中断，拖垮全量。它本来就是 `python tests/test_import.py <小说目录>`
# 的手工工具，CI/全量一律 `--ignore`，但忘了带 --ignore 的调用会炸。
# 在 conftest 里声明 collect_ignore 是双保险（文件自身改动也不影响）。
collect_ignore_glob = ["test_import.py"]

# ★★★ 禁止在测试进程内启动后台队列 worker（v0.26.2）
#
# 为什么必须禁：
#   `app.on_event("startup")` 会调 `drama_queue.ensure_worker()`，而
#   `drama_queue._worker` 是**模块级全局** —— 一旦启动就跨测试存活，
#   并且它持有 `SessionLocal()` 的会话（每 POLL_INTERVAL 轮询一次）。
#
#   测试里为了隔离会把 `main/auth/db` 三处 `SessionLocal` 全部 monkeypatch
#   成指向临时库的 sessionmaker；此时**上一个测试遗留的 worker** 仍在用
#   旧 session 轮询 → 与新的 teardown 竞争 → 抛
#   `IllegalStateChangeError: close() can't be called here;
#    commit() is already in progress`
#   （症状：单跑该文件 77 passed，全量跑时随机 2 个用例失败 —— 典型的
#     跨测试污染，极难定位）。
#
#   本项目的后台 worker 属于**运行时基础设施**，不是被测对象：
#   批量生成端点在 worker 未启动时会返回明确错误提示，不影响契约测试。
#
# ★ 与 MEMORY §62「测试模块实例分裂」同源：全局单例 + 多测试 = 必须显式隔离
os.environ["NOVELFORGE_DISABLE_WORKERS"] = "1"

