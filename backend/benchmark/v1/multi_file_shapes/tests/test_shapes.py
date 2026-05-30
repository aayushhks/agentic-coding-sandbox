import math

from shapes import Circle, Rectangle


def test_circle():
    assert math.isclose(Circle(2).area(), math.pi * 4)


def test_rectangle():
    assert Rectangle(3, 4).area() == 12
