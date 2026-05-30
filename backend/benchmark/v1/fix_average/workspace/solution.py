def average(numbers):
    total = 0
    for n in numbers:
        total += n
    # bug: integer division truncates the result
    return total // len(numbers)
