def binary_search(arr, target):
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            # bug: failing to advance past mid can loop forever
            lo = mid
        else:
            hi = mid
    return -1
