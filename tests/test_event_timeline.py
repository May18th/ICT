"""事件时标的片区预警选择回归测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_event_timeline.py"
SPEC = importlib.util.spec_from_file_location("build_event_timeline", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
TIMELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TIMELINE)


def test_pick_alert_stays_in_event_district() -> None:
    alerts = [
        {
            "alert_id": "WA-20261029-01",
            "status": "active",
            "valid_from": "2026-10-29 08:00:00+08:00",
            "rainfall_mm_3h": "60",
            "district": "A-03",
        },
        {
            "alert_id": "WA-20261029-02",
            "status": "active",
            "valid_from": "2026-10-29 08:00:00+08:00",
            "rainfall_mm_3h": "70",
            "district": "B-01",
        },
    ]

    selected = TIMELINE.pick_alert(alerts, "2026-10-29", "A-03")

    assert selected["alert_id"] == "WA-20261029-01"


def test_pick_alert_rejects_missing_event_district() -> None:
    with pytest.raises(ValueError):
        TIMELINE.pick_alert(
            [
                {
                    "alert_id": "WA-20261029-01",
                    "status": "active",
                    "valid_from": "2026-10-29 08:00:00+08:00",
                    "rainfall_mm_3h": "60",
                    "district": "A-03",
                }
            ],
            "2026-10-29",
            "B-01",
        )
