"""Objective-C 속성 탐색을 지원하는 super의 기본 타입 계약이다."""

from builtins import super as super
from collections.abc import Callable

def typedSelector[**P, R](signature: bytes) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """메서드의 파이썬 인자와 반환 타입을 유지하고 네이티브 호출 규약을 지정한다."""
    ...
