# Vendored verbatim from PERELMAN fastbrti.py at commit 9b55eae3cb7a2030989d18086d7bdee20f51f7ad
# (github.com/derekduenas/perelman), sha256 902205f009250a0f76c44a206f91a48af7f8dc2e4c976a09e9e9bd67b80127ae.
# Do not edit: replica parity with PERELMAN depends on it.
"""Vectorized BRTI replica for live use. Must agree with brti.py (the reference).

The only deliberate difference: the KDE mode in the dynamic spacing (Eq. 6j) is
found by binning the sample onto the same 4001-point grid the reference uses and
smoothing with the Gaussian kernel, instead of an O(grid x n) direct sum.
"""
import numpy as np

D = 0.005
TAU = 100
LAMBDA_FACTOR = 0.3
GRID = 4001


def consolidate(books):
    """books: iterable of (bids, asks), each a dict price -> size or a list of pairs."""
    bid, ask = {}, {}
    for b, a in books:
        for p, s in (b.items() if isinstance(b, dict) else b):
            if p > 0 and s > 0:
                bid[p] = bid.get(p, 0.0) + s
        for p, s in (a.items() if isinstance(a, dict) else a):
            if p > 0 and s > 0:
                ask[p] = ask.get(p, 0.0) + s
    bp = np.fromiter(bid.keys(), float, len(bid))
    bs = np.fromiter(bid.values(), float, len(bid))
    ap = np.fromiter(ask.keys(), float, len(ask))
    as_ = np.fromiter(ask.values(), float, len(ask))
    bo, ao = np.argsort(-bp), np.argsort(ap)
    return bp[bo], bs[bo], ap[ao], as_[ao]


def _counts(bp, ap):
    ac = max(int(np.searchsorted(ap, 1.05 * ap[0], side="right")), min(len(ap), 50))
    bc = max(int(np.count_nonzero(bp >= 0.95 * bp[0])), min(len(bp), 50))
    return ac, bc


def _winsor(x):
    n = len(x)
    k = int(np.floor(0.01 * n))
    w = x.copy()
    if k:
        w[:k] = x[k]
        w[n - k:] = x[n - k - 1]
    return k, w


def size_cap(bp, bs, ap, as_):
    ac, bc = _counts(bp, ap)
    x = np.sort(np.concatenate([bs[:bc], as_[:ac]]))
    n = len(x)
    k, w = _winsor(x)
    trimmed = x[k:n - k].mean()
    sigma = w.std(ddof=1) if n > 1 else 0.0
    return trimmed + 5 * sigma


def uncross(bp, bs, ap, as_):
    bs, as_ = bs.copy(), as_.copy()
    i = j = 0
    while i < len(bp) and j < len(ap) and bp[i] >= ap[j]:
        q = min(bs[i], as_[j])
        bs[i] -= q
        as_[j] -= q
        if bs[i] <= 0:
            i += 1
        if as_[j] <= 0:
            j += 1
    return bp[i:], bs[i:], ap[j:], as_[j:]


def kde_mode(sample):
    n = len(sample)
    if n < 4:
        return float(sample[0])
    _, w = _winsor(sample)
    sd = w.std(ddof=1)
    if sd == 0:
        return float(w[0])
    h = 1.06 * sd * n ** -0.2
    lo, hi = w[0], w[-1]
    grid = np.linspace(lo, hi, GRID)
    step = (hi - lo) / (GRID - 1)
    counts = np.bincount(np.clip(np.rint((w - lo) / step).astype(int), 0, GRID - 1), minlength=GRID)
    half = int(np.ceil(6 * h / step))
    offsets = np.arange(-half, half + 1) * step
    dens = np.convolve(counts, np.exp(-0.5 * (offsets / h) ** 2), mode="same") if half < GRID else \
        np.array([np.sum(np.exp(-0.5 * ((g - w) / h) ** 2)) for g in grid])
    return float(grid[int(np.argmax(dens))])


def spacing(bp, bs, ap, as_):
    s_lev = min(as_.sum(), bs.sum()) / TAU
    ac, bc = _counts(bp, ap)
    side = bs[:bc] if bs[:bc].sum() < as_[:ac].sum() else as_[:ac]
    return min(s_lev, kde_mode(np.sort(side)))


def brti(books, d=None):
    """d: deviation-from-mid limit (CF section 6.2): 0.5% for BTC, 1% for ETH, SOL, XRP."""
    d = D if d is None else d
    bp, bs, ap, as_ = consolidate(books)
    cap = size_cap(bp, bs, ap, as_)
    bs, as_ = np.minimum(bs, cap), np.minimum(as_, cap)
    bp, bs, ap, as_ = uncross(bp, bs, ap, as_)
    if not len(bp) or not len(ap):
        raise ValueError("a side was consumed while uncrossing")
    s = spacing(bp, bs, ap, as_)
    cb, ca = np.cumsum(bs), np.cumsum(as_)
    depth = min(cb[-1], ca[-1])
    n = int(np.floor(depth / s + 1e-12))
    if n < 1:
        n = 1
    v = s * np.arange(1, n + 1)
    a = ap[np.minimum(np.searchsorted(ca, v - 1e-12), len(ap) - 1)]
    b = bp[np.minimum(np.searchsorted(cb, v - 1e-12), len(bp) - 1)]
    mid = (a + b) / 2
    breach = np.nonzero(a / mid - 1 > d)[0]
    used = int(breach[0]) if len(breach) else n
    used = max(used, 1)
    v_t = v[used - 1]
    lam = 1 / (LAMBDA_FACTOR * v_t)
    wts = lam * np.exp(-lam * v[:used])
    return float(np.dot(mid[:used], wts) / wts.sum()), {
        "cap": float(cap), "spacing": float(s), "utilized_depth": float(v_t), "levels_used": used}
