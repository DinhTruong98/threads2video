from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


def retry(
    fn: Callable[[], T],
    *,
    attempts: int,
    what: str,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    base_delay: float = 2.0,
) -> T:
    """Call fn up to `attempts` times with exponential backoff; re-raise the last error."""
    for i in range(1, attempts + 1):
        try:
            return fn()
        except retry_on as e:
            if i == attempts:
                raise
            delay = base_delay * 2 ** (i - 1)
            log.warning("%s failed (attempt %d/%d): %s; retrying in %.0fs", what, i, attempts, e, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")
