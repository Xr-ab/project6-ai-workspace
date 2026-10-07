"""ExceptionGroup 摊平（app/ai/tools/external_tools.py:53）。

刻意不 import exceptiongroup：Python 3.10 没有内置 BaseExceptionGroup，
duck-type `.exceptions` 才让 SDK 换成普通异常时自动退化成单层文案而不是跟着坏。
"""
from app.ai.tools.external_tools import _error_text


class _FakeGroup(Exception):
    """只靠 .exceptions 属性被识别——这正是被 duck-type 的形状。"""

    def __init__(self, subs):
        self.exceptions = subs
        super().__init__("multi-error")


def _group(subs, name="ExceptionGroup"):
    """逐用例造一个以此为类名的 _FakeGroup 子类，让类名 per-instance 独立。
    （原写法 `self.__class__.__name__ = name` 写的是**共享类属性**：之后构造的每个实例
    都继承最后一次改名 → 用例互相依赖构造顺序，且 `name` 实参从没被任何用例传过。
    _error_text 取的是 type(exc).__name__（external_tools.py:62），故这里必须真造一个
    以此为名的类，`name` 才活着、空-exceptions 那条的 "ExceptionGroup: multi-error" 才不靠运气。）"""
    return type(name, (_FakeGroup,), {})(subs)


def test_leaf_shape_is_type_name_colon_message():
    assert _error_text(TypeError("boom")) == "TypeError: boom"


def test_one_level_group_joins_with_the_full_width_semicolon():
    text = _error_text(_group([ValueError("a"), KeyError("b")]))
    assert text == "ValueError: a；KeyError: 'b'"


def test_nested_group_flattens_recursively():
    inner = _group([RuntimeError("x"), RuntimeError("y")])
    outer = _group([inner, RuntimeError("z")])
    assert _error_text(outer) == "RuntimeError: x；RuntimeError: y；RuntimeError: z"


def test_empty_exceptions_attribute_degrades_to_the_plain_leaf():
    assert _error_text(_group([])) == "ExceptionGroup: multi-error"


def test_non_group_exception_without_the_attribute_is_untouched():
    assert not hasattr(Exception("e"), "exceptions")
    assert _error_text(Exception("e")) == "Exception: e"
