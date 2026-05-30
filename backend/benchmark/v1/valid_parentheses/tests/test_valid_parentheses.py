from solution import is_valid


def test_valid():
    assert is_valid("()[]{}") is True


def test_nested():
    assert is_valid("([{}])") is True


def test_mismatched():
    assert is_valid("(]") is False


def test_unbalanced():
    assert is_valid("(((") is False
