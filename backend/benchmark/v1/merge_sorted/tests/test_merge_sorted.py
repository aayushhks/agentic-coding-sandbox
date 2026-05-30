from solution import merge


def test_basic():
    assert merge([1, 3, 5], [2, 4, 6]) == [1, 2, 3, 4, 5, 6]


def test_empty():
    assert merge([], [1, 2]) == [1, 2]


def test_duplicates():
    assert merge([1, 1], [1]) == [1, 1, 1]
