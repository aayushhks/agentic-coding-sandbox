from solution import fib


def test_small():
    assert fib(0) == 0
    assert fib(1) == 1
    assert fib(10) == 55


def test_large():
    assert fib(40) == 102334155
