from __future__ import annotations

from benchmarks.run_phase5_live_fixture import _rss_checkpoints


class FakeStore:
    def __init__(self, ordinals: list[int]):
        self._items = [{"ordinal": ordinal} for ordinal in ordinals]

    def items(self, batch_id: str):
        return self._items


def test_rss_checkpoints_report_only_observed_target_ordinals():
    store = FakeStore([1, 10, 25])
    checkpoints = _rss_checkpoints(store, "batch", {"10": 51.2})
    assert checkpoints == {
        "10": 51.2,
        "25": None,
        "50": None,
        "100": None,
    }
