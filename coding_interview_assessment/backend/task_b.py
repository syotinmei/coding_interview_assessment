from __future__ import annotations

from collections import deque
from dataclasses import dataclass

INF = float("inf")


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Warehouse:
    id: str
    stock: int
    fixed_cost: int
    unit_cost: int


@dataclass(frozen=True)
class Plan:
    cost: int
    allocations: list[tuple[str, int]]  # sorted by warehouse_id

    @property
    def warehouse_count(self) -> int:
        return len(self.allocations)


def min_warehouses(stocks: list[int], quantity: int) -> int | None:
    """Smallest k whose k largest stocks sum to >= quantity, or None."""
    total = 0
    for k, s in enumerate(sorted(stocks, reverse=True), start=1):
        total += s
        if total >= quantity:
            return k
    return None


def optimise(warehouses: list[Warehouse], quantity: int) -> Plan | None:
    """Return the best plan, or None when the order cannot be fulfilled."""
    # Zero-stock warehouses can never be "used" (allocation must be > 0).
    pool = sorted((w for w in warehouses if w.stock > 0), key=lambda w: w.id)
    k = min_warehouses([w.stock for w in pool], quantity)
    if k is None:
        return None

    n = len(pool)
    # best[i][j] is a list over q (0..Q) of the min cost, INF when impossible.
    base = [INF] * (quantity + 1)
    base[0] = 0
    empty = [INF] * (quantity + 1)
    best: list[list[list[float]]] = [[empty] * (k + 1) for _ in range(n + 1)]
    best[n][0] = base

    for i in range(n - 1, -1, -1):
        w = pool[i]
        nxt = best[i + 1]
        cur: list[list[float]] = [nxt[0]] + [empty] * k  # j=0: use nothing
        for j in range(1, k + 1):
            skip = nxt[j]
            prev = nxt[j - 1]
            row = list(skip)  # option 1: skip warehouse i
            # Option 2: take x in [1, stock] from i. Substitute p = q - x so
            # cost = f + u*q + (prev[p] - u*p) with p in [q - stock, q - 1].
            window: deque[tuple[int, float]] = deque()  # (p, prev[p] - u*p), values increasing
            for q in range(1, quantity + 1):
                p = q - 1
                if prev[p] != INF:
                    val = prev[p] - w.unit_cost * p
                    while window and window[-1][1] >= val:
                        window.pop()
                    window.append((p, val))
                while window and window[0][0] < q - w.stock:
                    window.popleft()
                if window:
                    cand = w.fixed_cost + w.unit_cost * q + window[0][1]
                    if cand < row[q]:
                        row[q] = cand
            cur[j] = row
        best[i] = cur

    if best[0][k][quantity] == INF:  # pragma: no cover - guarded by min_warehouses
        return None

    # Reconstruction in warehouse_id order (see module docstring, step 3).
    allocations: list[tuple[str, int]] = []
    j, q = k, quantity
    for i, w in enumerate(pool):
        if j == 0:
            break
        target = best[i][j][q]
        chosen = 0
        for x in range(1, min(w.stock, q) + 1):  # smallest optimal x wins
            if w.fixed_cost + w.unit_cost * x + best[i + 1][j - 1][q - x] == target:
                chosen = x
                break
        if chosen:  # using i beats skipping i lexicographically
            allocations.append((w.id, chosen))
            j -= 1
            q -= chosen
        # else: optimal solution skips i; best[i+1][j][q] == target
    return Plan(int(best[0][k][quantity]), allocations)


def parse(text: str) -> tuple[list[Warehouse], int]:
    """Parse 'W Q' followed by W warehouse lines; raises InputError."""
    rows = [ln.split() for ln in text.strip().splitlines() if ln.strip()]
    try:
        w_count, quantity = int(rows[0][0]), int(rows[0][1])
        if len(rows[0]) != 2 or len(rows) - 1 != w_count:
            raise ValueError
        items = []
        for r in rows[1:]:
            if len(r) != 4:
                raise ValueError
            items.append(Warehouse(r[0], int(r[1]), int(r[2]), int(r[3])))
    except (IndexError, ValueError):
        raise InputError("expected 'W Q' then W lines of 'id stock fixed unit'") from None
    validate(items, quantity)
    return items, quantity


def validate(items: list[Warehouse], quantity: int) -> None:
    if not 1 <= len(items) <= 30:
        raise InputError("W must be in 1..30")
    if not 1 <= quantity <= 2000:
        raise InputError("Q must be in 1..2000")
    if len({w.id for w in items}) != len(items):
        raise InputError("warehouse_id values must be unique")
    for w in items:
        if not 0 <= w.stock <= 2000:
            raise InputError(f"{w.id}: stock must be in 0..2000")
        if not (0 <= w.fixed_cost <= 10**6 and 0 <= w.unit_cost <= 10**6):
            raise InputError(f"{w.id}: costs must be in 0..10^6")


def format_plan(plan: Plan | None) -> list[str]:
    if plan is None:
        return ["-1"]
    return [f"{plan.warehouse_count} {plan.cost}"] + [f"{i} {q}" for i, q in plan.allocations]


def run(text: str) -> list[str]:
    items, quantity = parse(text)
    return format_plan(optimise(items, quantity))


def main() -> None:
    import sys

    try:
        print("\n".join(run(sys.stdin.read())))
    except InputError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
