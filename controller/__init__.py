"""BlkMaC 顶层调度包：三组对照实验的主入口。

.. note::
   本包**故意不在顶层 import main_controller**。

   ``main_controller`` 既是普通模块又是 ``python -m`` 的入口。若在这里
   顶层导入它，执行 ``python -m controller.main_controller`` 时会：

     1. 先导入包 ``controller`` → 触发 ``import controller.main_controller``，
        模块被放进 ``sys.modules``；
     2. runpy 再把同一个文件**当作 ``__main__`` 重新执行一遍**。

   同一份代码被加载两次，``ExperimentRunner`` 等类会存在两个互不相等的副本，
   跨副本的 ``isinstance`` 判断会失败。Python 也会就此告警::

       RuntimeWarning: 'controller.main_controller' found in sys.modules after
       import of package 'controller', but prior to execution of ...

   因此这里改用 PEP 562 的模块级 ``__getattr__`` 做惰性导出：
   ``from controller import ExperimentRunner`` 依然可用，
   但只在真正取用时才导入，不会污染 ``python -m`` 的执行路径。
"""

from typing import TYPE_CHECKING

__all__ = [
    "GROUP_LABELS",
    "ExperimentRunner",
    "GroupResult",
    "Question",
    "QuestionRecord",
    "build_decomposition_plan",
    "build_knowledge_base",
    "export_results",
    "is_correct",
    "load_dataset",
    "plot_results",
]

if TYPE_CHECKING:  # 仅供类型检查器与 IDE 补全，运行时不执行
    from controller.main_controller import (
        GROUP_LABELS,
        ExperimentRunner,
        GroupResult,
        Question,
        QuestionRecord,
        build_decomposition_plan,
        build_knowledge_base,
        export_results,
        is_correct,
        load_dataset,
        plot_results,
    )


def __getattr__(name: str):
    """惰性转发到 main_controller，避免包导入期就加载入口模块。"""
    if name in __all__:
        from importlib import import_module

        return getattr(import_module("controller.main_controller"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
