def is_valid_temperature_millideg_c(temperature_millideg_c: int) -> bool:
    return -100_000 <= temperature_millideg_c <= 150_000


def test_normal_positive_temperature_is_valid():
    assert is_valid_temperature_millideg_c(25_125)


def test_normal_negative_temperature_is_valid():
    assert is_valid_temperature_millideg_c(-5_250)


def test_boundary_temperatures_are_valid():
    assert is_valid_temperature_millideg_c(-100_000)
    assert is_valid_temperature_millideg_c(150_000)


def test_temperatures_outside_range_are_invalid():
    assert not is_valid_temperature_millideg_c(-100_001)
    assert not is_valid_temperature_millideg_c(150_001)
    assert not is_valid_temperature_millideg_c(999_999)