from solution import reverse_string


def test_reverse():
    assert reverse_string("hello") == "olleh"


def test_empty():
    assert reverse_string("") == ""
