from solution import average


def test_mean():
    assert average([1, 2, 3, 4]) == 2.5


def test_single():
    assert average([5]) == 5.0
