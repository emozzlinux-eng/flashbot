"""Constant-product (Uniswap-V2 style) maths for a two-pool arbitrage.

Pure integer maths so it matches what the router does on-chain. No web3 needed.
"""
from math import isqrt

FEE_DENOM = 10_000  # fees are expressed in basis points, e.g. 30 = 0.30%


def get_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int) -> int:
    """Same formula as UniswapV2Library.getAmountOut (with a configurable fee)."""
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    with_fee = amount_in * (FEE_DENOM - fee_bps)
    return (with_fee * reserve_out) // (reserve_in * FEE_DENOM + with_fee)


def round_trip(x: int, a1: int, b1: int, f1: int, a2: int, b2: int, f2: int) -> int:
    """Borrow x of token A.

    Pool 1 holds (a1 of A, b1 of B): swap A -> B.
    Pool 2 holds (b2 of B, a2 of A): swap B -> A.
    Returns how much A comes back.
    """
    y = get_amount_out(x, a1, b1, f1)
    return get_amount_out(y, b2, a2, f2)


def optimal_amount(a1: int, b1: int, f1: int, a2: int, b2: int, f2: int, max_in: int):
    """Best amount of A to borrow, and the profit in A. Returns (0, 0) if there is no profit.

    The round trip is z = K*x / (M + N*x), so profit z - x is concave and the best x
    has a closed form: x* = (sqrt(K*M) - M) / N. We compute it with integers, then check
    a few neighbours with the exact integer swap maths to absorb rounding.
    """
    if min(a1, b1, a2, b2) <= 0 or max_in <= 0:
        return 0, 0
    d = FEE_DENOM
    g1, g2 = d - f1, d - f2
    s = isqrt(g1 * g2 * a1 * a2 * b1 * b2)
    num = d * (s - a1 * b2 * d)
    if num <= 0:  # pools are in line: no arbitrage
        return 0, 0
    x0 = num // (g1 * (b2 * d + g2 * b1))
    x0 = min(max(x0, 1), max_in)
    best_x, best_p = 0, 0
    for dx in range(-3, 4):
        x = x0 + dx
        if x < 1 or x > max_in:
            continue
        p = round_trip(x, a1, b1, f1, a2, b2, f2) - x
        if p > best_p:
            best_x, best_p = x, p
    return best_x, best_p
