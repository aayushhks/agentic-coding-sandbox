from solution import count_vowels


def test_basic():
    assert count_vowels("hello") == 2


def test_mixed_case():
    assert count_vowels("AEiou xyz") == 5


def test_none():
    assert count_vowels("xyz") == 0
