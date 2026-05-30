def topo_sort(graph):
    visited = set()
    order = []

    def visit(node):
        if node in visited:
            return
        visited.add(node)
        for nxt in graph.get(node, []):
            visit(nxt)
        order.append(node)

    for node in graph:
        visit(node)
    order.reverse()
    return order
