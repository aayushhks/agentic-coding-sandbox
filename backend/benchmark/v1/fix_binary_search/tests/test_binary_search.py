from solution import binary_search


def test_found():
    assert binary_search([1, 3, 5, 7, 9], 7) == 3


def test_first():
    assert binary_search([1, 3, 5, 7, 9], 1) == 0


def test_absent():
    assert binary_search([1, 3, 5, 7, 9], 4) == -1
