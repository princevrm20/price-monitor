from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from price_monitor.monitor import check_all
from price_monitor.storage import TrackedItem


def test_check_all_propagate_raises(tmp_path: Path) -> None:
    items_path = tmp_path / "items.json"
    items_path.write_text("[]", encoding="utf-8")
    item = TrackedItem(
        id="1",
        name="X",
        url="https://example.com",
        budget=1.0,
    )
    with patch("price_monitor.monitor.check_item", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="boom"):
            check_all([item], items_path=items_path, propagate=True)
