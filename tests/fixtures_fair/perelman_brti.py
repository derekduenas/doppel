"""Reference replica of the CME CF Bitcoin Real Time Index (BRTI).

Implements CME CF Real Time Indices Methodology Guide v17.0 (21 Sep 2026),
sections 4.1 (calculation), 5 (contingency rules) and 6 (parameters).
This is the correctness oracle for PERELMAN; the Rust hot path must match it.

Points the guide leaves open are marked AMBIGUITY and must be settled
empirically against published BRTI before the replica is trusted.
"""
from dataclasses import dataclass, field
from math import ceil, exp, floor, sqrt, pi
from statistics import median

DEVIATION_FROM_MID = 0.005          # D, BTC (Section 6.2)
POTENTIALLY_ERRONEOUS = 0.05        # 5% mid deviation from median (Section 6.2)
RETRIEVAL_LAG_THRESHOLD = 10.0      # seconds (Section 6.2)
TARGET_LEVELS = 100                 # tau (Section 6.1)
LAMBDA_FACTOR = 0.3                 # lambda = 1 / (0.3 * v_T) (Section 6.1)
CAP_SAMPLE_MIN = 50                 # Eq. 4a/4b
CAP_BAND = 0.05                     # 5% band around best price, Eq. 4a/4b


@dataclass
class Book:
    bids: list  # [(price, size)], any order
    asks: list
    retrieved_at: float  # seconds, our clock


class CalculationFailure(Exception):
    pass


def _clean(levels):
    """Section 5.2.2: drop entries with non-positive or non-numeric price/size."""
    out = []
    for p, s in levels:
        try:
            p, s = float(p), float(s)
        except (TypeError, ValueError):
            continue
        if p > 0 and s > 0 and p == p and s == s:
            out.append((p, s))
    return out


def _consolidate(books):
    bids, asks = {}, {}
    for b in books:
        for p, s in b.bids:
            bids[p] = bids.get(p, 0.0) + s
        for p, s in b.asks:
            asks[p] = asks.get(p, 0.0) + s
    return (sorted(bids.items(), key=lambda x: -x[0]), sorted(asks.items(), key=lambda x: x[0]))


def _sample_counts(bids, asks):
    """Eq. 4a/4b: levels within 5% of best, at least min(|side|, 50)."""
    ac = max(max(i + 1 for i, (p, _) in enumerate(asks) if p <= (1 + CAP_BAND) * asks[0][0]),
             min(len(asks), CAP_SAMPLE_MIN))
    bc = max(max(i + 1 for i, (p, _) in enumerate(bids) if p >= (1 - CAP_BAND) * bids[0][0]),
             min(len(bids), CAP_SAMPLE_MIN))
    return ac, bc


def _winsorize(sorted_sizes):
    n = len(sorted_sizes)
    k = floor(0.01 * n)
    w = [sorted_sizes[k] if i < k else sorted_sizes[n - k - 1] if i >= n - k else x
         for i, x in enumerate(sorted_sizes)]
    return k, w


def order_size_cap(bids, asks):
    """Section 4.1.3, computed from the UNCAPPED consolidated book."""
    ac, bc = _sample_counts(bids, asks)
    sizes = sorted([s for _, s in bids[:bc]] + [s for _, s in asks[:ac]])
    n = len(sizes)
    k, w = _winsorize(sizes)
    trimmed_mean = sum(sizes[k:n - k]) / (n - 2 * k)
    wmean = sum(w) / n
    sigma = sqrt(sum((x - wmean) ** 2 for x in w) / (n - 1)) if n > 1 else 0.0
    return trimmed_mean + 5 * sigma


def _uncross(bids, asks):
    """Section 4.1.1 step 2a."""
    bids, asks = [list(x) for x in bids], [list(x) for x in asks]
    while bids and asks and bids[0][0] >= asks[0][0]:
        q = min(bids[0][1], asks[0][1])
        bids[0][1] -= q
        asks[0][1] -= q
        if bids[0][1] <= 0:
            bids.pop(0)
        if asks[0][1] <= 0:
            asks.pop(0)
    return [tuple(x) for x in bids], [tuple(x) for x in asks]


def _kde_mode(sample):
    """Eq. 6g-6j: Gaussian KDE with Silverman bandwidth; argmax by grid search.
    AMBIGUITY: the guide gives no search method or grid; a 4001-point grid over
    the sample range is used here."""
    n = len(sample)
    if n < 4:
        return sample[0]
    _, w = _winsorize(sample)
    m = sum(w) / n
    sd = sqrt(sum((x - m) ** 2 for x in w) / (n - 1))
    if sd == 0:
        return w[0]
    h = 1.06 * sd * n ** (-0.2)
    lo, hi = w[0], w[-1]
    best, best_f = lo, -1.0
    for j in range(4001):
        x = lo + (hi - lo) * j / 4000
        f = sum(exp(-0.5 * ((x - xi) / h) ** 2) for xi in w) / (n * h * sqrt(2 * pi))
        if f > best_f:
            best, best_f = x, f
    return best


def spacing(bids, asks):
    """Section 4.1.4, Eq. 6a-7.
    AMBIGUITY: whether ac/bc and sizes here come from the capped, uncrossed book
    (used here) or the uncapped book."""
    s_lev = min(sum(s for _, s in asks), sum(s for _, s in bids)) / TARGET_LEVELS
    ac, bc = _sample_counts(bids, asks)
    bid_sum = sum(s for _, s in bids[:bc])
    ask_sum = sum(s for _, s in asks[:ac])
    side = bids[:bc] if bid_sum < ask_sum else asks[:ac]
    s_kde = _kde_mode(sorted(s for _, s in side))
    return min(s_lev, s_kde)


def _pv(levels, v):
    """Eq. 1a/1b: marginal price at cumulative volume v, or None beyond depth."""
    cum = 0.0
    for p, s in levels:
        cum += s
        if cum >= v - 1e-12:
            return p
    return None


def brti_from_books(books):
    if not books:
        raise CalculationFailure("no usable books")
    bids, asks = _consolidate(books)
    cap = order_size_cap(bids, asks)
    bids = [(p, min(s, cap)) for p, s in bids]
    asks = [(p, min(s, cap)) for p, s in asks]
    bids, asks = _uncross(bids, asks)
    if not bids or not asks:
        raise CalculationFailure("a side was consumed while uncrossing")
    s = spacing(bids, asks)
    depth = min(sum(q for _, q in bids), sum(q for _, q in asks))
    grid, mids = [], []
    v_t = s
    i = 1
    while i * s <= depth + 1e-12:
        v = i * s
        a, b = _pv(asks, s * ceil(v / s - 1e-12)), _pv(bids, s * ceil(v / s - 1e-12))
        if a is None or b is None:
            break
        mid = (a + b) / 2
        if a / mid - 1 > DEVIATION_FROM_MID:   # Eq. 2: stop at first breach
            break
        grid.append(v)
        mids.append(mid)
        v_t = v
        i += 1
    v_t = max(v_t, s)
    if not grid:  # utilized depth floored at spacing: index ~ consolidated mid
        a, b = _pv(asks, s), _pv(bids, s)
        grid, mids = [s], [(a + b) / 2]
    lam = 1 / (LAMBDA_FACTOR * v_t)
    weights = [lam * exp(-lam * v) for v in grid]
    nf = sum(weights)
    return sum(m * w for m, w in zip(mids, weights)) / nf, {
        "cap": cap, "spacing": s, "utilized_depth": v_t, "levels_used": len(grid)}


@dataclass
class Replica:
    """Stateful wrapper: Section 5 contingency rules incl. re-instatement hysteresis."""
    excluded: set = field(default_factory=set)

    def calculate(self, books_by_venue, calc_time):
        usable = {}
        for venue, book in books_by_venue.items():
            if calc_time - book.retrieved_at > RETRIEVAL_LAG_THRESHOLD:
                continue                                     # 5.1 delayed
            bids, asks = _clean(book.bids), _clean(book.asks)
            if not bids or not asks or max(p for p, _ in bids) >= min(p for p, _ in asks):
                continue                                     # 5.2.1 erroneous book
            usable[venue] = Book(bids, asks, book.retrieved_at)
        if not usable:
            raise CalculationFailure("all books delayed or erroneous")
        mids = {v: (max(p for p, _ in b.bids) + min(p for p, _ in b.asks)) / 2 for v, b in usable.items()}
        med = median(mids.values())
        for venue, mid in mids.items():                      # 5.3 with hysteresis
            dev = abs(mid / med - 1)
            if venue in self.excluded:
                if dev < 0.5 * POTENTIALLY_ERRONEOUS:
                    self.excluded.discard(venue)
            elif dev > POTENTIALLY_ERRONEOUS:
                self.excluded.add(venue)
        kept = [b for v, b in usable.items() if v not in self.excluded]
        if not kept:
            raise CalculationFailure("all books potentially erroneous")
        value, diag = brti_from_books(kept)
        return value, {**diag, "venues": sorted(v for v in usable if v not in self.excluded)}
