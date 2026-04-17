"""Tests for :mod:`voxhub_core.memory_budget`."""

import numpy as np
import pytest

from voxhub_core import memory_budget
from voxhub_core.memory_budget import (
    MemoryBudget,
    MemoryBudgetError,
    MemoryWarning,
    check,
    estimate_array_bytes,
)


class TestEstimateArrayBytes:
    def test_uint16_volume(self):
        assert estimate_array_bytes((100, 100, 100), np.uint16) == 100 * 100 * 100 * 2

    def test_float32_accepts_dtype_string(self):
        assert estimate_array_bytes((10, 10, 10), 'float32') == 4000

    def test_zero_dim_axis_is_zero(self):
        assert estimate_array_bytes((0, 100), np.float64) == 0


class TestMemoryBudgetFactories:
    def test_warn_only_does_not_refuse(self):
        b = MemoryBudget.warn_only()
        assert b.refuse_when_low is False

    def test_disabled_threshold_above_realistic_volumes(self):
        b = MemoryBudget.disabled()
        # 1 EB volume should still not trip the warn threshold.
        assert b.warn_threshold_bytes > 10**18


class TestCheck:
    def test_under_threshold_no_warning(self, monkeypatch):
        # Force a generous available number so the low-memory path can't fire.
        monkeypatch.setattr(memory_budget, 'read_available_bytes', lambda: 16 * 1024**3)
        budget = MemoryBudget(
            warn_threshold_bytes=128 * 1024**2,
            refuse_when_low=False,
        )
        warnings = check(64 * 1024**2, budget=budget)
        assert warnings == []

    def test_large_volume_emits_warning(self, monkeypatch):
        monkeypatch.setattr(memory_budget, 'read_available_bytes', lambda: 16 * 1024**3)
        budget = MemoryBudget(
            warn_threshold_bytes=128 * 1024**2,
            refuse_when_low=False,
        )
        warnings = check(256 * 1024**2, budget=budget, context='store-x')
        assert len(warnings) == 1
        assert warnings[0].code == 'large_volume'
        assert 'store-x' in warnings[0].message
        assert warnings[0].volume_bytes == 256 * 1024**2

    def test_low_memory_warning_when_not_refusing(self, monkeypatch):
        monkeypatch.setattr(memory_budget, 'read_available_bytes', lambda: 100 * 1024**2)
        budget = MemoryBudget(
            warn_threshold_bytes=10 * 1024**2,
            refuse_when_low=False,
            safety_factor=2.0,
        )
        warnings = check(80 * 1024**2, budget=budget)
        codes = {w.code for w in warnings}
        assert 'low_memory_available' in codes
        # Plain warning, no exception
        low = next(w for w in warnings if w.code == 'low_memory_available')
        assert low.available_bytes == 100 * 1024**2
        assert low.threshold_bytes == 160 * 1024**2

    def test_refuse_when_low_raises(self, monkeypatch):
        monkeypatch.setattr(memory_budget, 'read_available_bytes', lambda: 50 * 1024**2)
        budget = MemoryBudget(
            warn_threshold_bytes=2**63 - 1,
            refuse_when_low=True,
            safety_factor=2.0,
        )
        with pytest.raises(MemoryBudgetError) as exc_info:
            check(80 * 1024**2, budget=budget, context='store-y')
        err = exc_info.value
        assert isinstance(err.warning, MemoryWarning)
        assert err.warning.code == 'low_memory_available'
        assert 'store-y' in str(err)

    def test_unknown_available_skips_low_memory_check(self, monkeypatch):
        # Simulate a non-Linux platform where MemAvailable is unreadable.
        monkeypatch.setattr(memory_budget, 'read_available_bytes', lambda: None)
        budget = MemoryBudget(
            warn_threshold_bytes=2**63 - 1,
            refuse_when_low=True,
            safety_factor=10.0,
        )
        # Even with refuse_when_low=True we don't raise: we have no signal.
        warnings = check(1024 * 1024**2, budget=budget)
        assert warnings == []


class TestReadAvailableBytes:
    def test_returns_int_or_none(self):
        # On the dev box this is Linux, so we get a real number.  In the
        # fallback path it's ``None``.  Both are valid.
        v = memory_budget.read_available_bytes()
        assert v is None or (isinstance(v, int) and v > 0)

    def test_handles_missing_meminfo(self, monkeypatch, tmp_path):
        monkeypatch.setattr(memory_budget, '_MEMINFO_PATH', tmp_path / 'absent')
        assert memory_budget.read_available_bytes() is None
