"""Stress selection must be explicit and cannot shrink the functional oracle."""
import pytest
from ground import durable_test_profile


def test_normal_and_stress_selection(monkeypatch):
    monkeypatch.delenv("ORBIT_TEST_DURABLE_COUNT", raising=False)
    assert durable_test_profile.sample_count() == 1000
    assert durable_test_profile.sender_timeout(1000) == 180
    monkeypatch.setenv("ORBIT_TEST_DURABLE_COUNT", "10000")
    assert durable_test_profile.sample_count() == 10000
    assert durable_test_profile.sender_timeout(10000) == 1200


@pytest.mark.parametrize("value", ["", "invalid", "999", "10001", "-1"])
def test_invalid_profile_fails_before_running_io(monkeypatch, value):
    monkeypatch.setenv("ORBIT_TEST_DURABLE_COUNT", value)
    with pytest.raises(ValueError, match="ORBIT_TEST_DURABLE_COUNT"):
        durable_test_profile.sample_count()
