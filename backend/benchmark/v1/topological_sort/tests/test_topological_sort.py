from solution import topo_sort


def test_valid_order():
    graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
    order = topo_sort(graph)
    assert set(order) == {"a", "b", "c", "d"}
    assert order.index("a") < order.index("b")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")
