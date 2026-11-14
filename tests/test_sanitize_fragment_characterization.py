"""Characterization test for the existing diagnostic fragment sanitizer."""

from tabularium.execution import _DIAGNOSTIC_MAX_CHARS, _sanitize_fragment


def test_sanitize_fragment_matches_pre_move_output():
    assert _DIAGNOSTIC_MAX_CHARS == 160
    cases = [
        ("empty", "", None, ""),
        ("short text", "Keep this diagnostic", None,
         "Keep this diagnostic"),
        ("long default", "x" * 200, None, "x" * 157 + "..."),
        ("newline and tab", "alpha\nbeta\tgamma", None,
         r"alpha\nbeta\tgamma"),
        ("escaped controls", "a\x00b\x1bc\x7fd\u2028e", None,
         r"a\x00b\x1bc\x7fd\u2028e"),
        ("non-string", 17, None, ""),
        ("exact default boundary", "b" * 160, None, "b" * 160),
        ("over default boundary", "c" * 161, None,
         "c" * 157 + "..."),
        ("limit 300", "d" * 500, 300, "d" * 297 + "..."),
        ("collapse before truncate", "m" * 155 + " \n\t " + "n" * 20,
         None, "m" * 155 + " \\" + "..."),
    ]
    for name, value, limit, expected in cases:
        kwargs = {} if limit is None else {"limit": limit}
        actual = _sanitize_fragment(value, **kwargs)
        assert actual == expected, f"{name}: {actual!r}"
