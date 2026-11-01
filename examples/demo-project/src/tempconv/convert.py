"""Celsius/Fahrenheit conversion."""

ABSOLUTE_ZERO_C = -273.15


def celsius_to_fahrenheit(value: float) -> float:
    if value < ABSOLUTE_ZERO_C:
        raise ValueError("temperature below absolute zero")
    return value * 9 / 5 + 32


def fahrenheit_to_celsius(value: float) -> float:
    celsius = (value - 32) * 5 / 9
    if celsius < ABSOLUTE_ZERO_C:
        raise ValueError("temperature below absolute zero")
    return celsius
