import pytest

from dropshare_server.core.rate_limit import SlidingWindowLimiter


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_says_how_long_to_wait() -> None:
    clock = _Clock()
    limiter = SlidingWindowLimiter(3, 60, clock)

    assert [limiter.acquire("admin") for _ in range(3)] == [None, None, None]
    clock.now += 20
    assert limiter.acquire("admin") == 40  # the first hit leaves the window in 40 s


def test_the_window_slides() -> None:
    clock = _Clock()
    limiter = SlidingWindowLimiter(2, 60, clock)
    limiter.acquire("admin")
    clock.now += 30
    limiter.acquire("admin")

    clock.now += 30  # the first hit is now exactly 60 s old
    assert limiter.acquire("admin") is None
    assert limiter.acquire("admin") == 30


def test_a_refused_action_is_not_counted() -> None:
    clock = _Clock()
    limiter = SlidingWindowLimiter(1, 60, clock)
    limiter.acquire("admin")
    for _ in range(5):
        clock.now += 10
        limiter.acquire("admin")

    clock.now += 10  # 60 s after the only counted hit
    assert limiter.acquire("admin") is None


def test_each_key_has_its_own_limit() -> None:
    limiter = SlidingWindowLimiter(1, 60, _Clock())

    assert limiter.acquire("alice") is None
    assert limiter.acquire("bob") is None
    assert limiter.acquire("alice") is not None


def test_the_wait_is_at_least_a_second() -> None:
    clock = _Clock()
    limiter = SlidingWindowLimiter(1, 60, clock)
    limiter.acquire("admin")
    clock.now += 59.9

    assert limiter.acquire("admin") == 1


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (1, 0)])
def test_nonsense_limits_are_refused(limit: int, window: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        SlidingWindowLimiter(limit, window)
