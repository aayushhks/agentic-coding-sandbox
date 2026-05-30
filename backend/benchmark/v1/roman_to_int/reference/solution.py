def roman_to_int(s):
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    prev = 0
    for ch in reversed(s):
        value = values[ch]
        if value < prev:
            total -= value
        else:
            total += value
            prev = value
    return total
