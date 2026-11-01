import pytest

from tempconv import celsius_to_fahrenheit, fahrenheit_to_celsius


def test_freezing_point():
    assert celsius_to_fahrenheit(0) == 32
    assert fahrenheit_to_celsius(32) == 0


def test_round_trip():
    assert fahrenheit_to_celsius(celsius_to_fahrenheit(37.5)) == pytest.approx(37.5)


def test_below_absolute_zero_is_rejected():
    with pytest.raises(ValueError):
        celsius_to_fahrenheit(-300)
