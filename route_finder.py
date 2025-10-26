# app/route_finder.py
"""
route_finder: route discovery for /trade wizard

Builds a token graph from config.POOLS_V3. Returns candidate multihop
paths up to 2 hops, with optional "force_via" (WONE or 1sDAI, etc.).

Each returned candidate path is token+fee annotated like:
["1USDC", "WONE@500", "1ETH@3000"]

We also give helpers to:
- pretty-print fee tiers as percents (500 -> 0.05%)
- summarize total pool fee % along the path
"""

from collections import defaultdict, deque
from typing import Dict, List, Tuple, Optional

try:
    import app.config as C
except Exception:
    import config as C  # type: ignore


def _parse_pool_key(k: str) -> Tuple[str, str, int]:
    # '1USDC/WONE@500' -> ('1USDC', 'WONE', 500)
    pair, fee = k.split("@")
    a, b = pair.split("/")
    return a.strip(), b.strip(), int(fee)


def _build_graph() -> Dict[str, List[Tuple[str, int]]]:
    """
    Returns adjacency:
    {
      "1USDC": [("WONE", 500), ("1sDAI",500)],
      "WONE":  [("1USDC",500), ("1ETH",3000)],
      ...
    }
    """
    g: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for k in getattr(C, "POOLS_V3", {}).keys():
        try:
            a, b, fee = _parse_pool_key(k)
        except Exception:
            continue
        g[a].append((b, fee))
        g[b].append((a, fee))
    return g


def _lowest_fee_for_edge(a: str, b: str, g) -> Optional[int]:
    fees = [fee for (nbr, fee) in g.get(a, []) if nbr == b]
    return min(fees) if fees else None


def _label_path_with_fees(tokens: List[str], g) -> Optional[List[str]]:
    """
    ['1USDC','WONE','1ETH'] ->
    ['1USDC','WONE@500','1ETH@3000'] where fee is lowest available
    between hops.
    """
    if len(tokens) < 2:
        return None
    out: List[str] = [tokens[0]]
    for u, v in zip(tokens[:-1], tokens[1:]):
        fee = _lowest_fee_for_edge(u, v, g)
        if fee is None:
            return None
        out.append(f"{v}@{fee}")
    return out


def candidates(
    token_in: str,
    token_out: str,
    *,
    force_via: Optional[str] = None,
    max_hops: int = 2,
    max_routes: int = 3,
) -> List[List[str]]:
    """
    Return up to max_routes candidate paths, each as
    ['1USDC','WONE@500','1ETH@3000'].

    force_via: require that token as an intermediate (not endpoints).
    """
    token_in = token_in.strip()
    token_out = token_out.strip()
    if token_in == token_out:
        return [[token_in]]

    g = _build_graph()
    if token_in not in g:
        return []

    res: List[List[str]] = []

    # BFS up to max_hops edges
    from collections import deque
    q = deque([(token_in, [token_in])])
    best_depth: Dict[str, int] = {token_in: 0}

    while q:
        cur, path = q.popleft()
        depth = len(path) - 1
        if depth > max_hops:
            continue

        # Found the target
        if cur == token_out and 1 <= depth <= max_hops:
            labeled = _label_path_with_fees(path, g)
            if labeled:
                if force_via:
                    mids = set(path[1:-1])  # skip endpoints
                    if force_via not in mids:
                        pass
                    else:
                        res.append(labeled)
                else:
                    res.append(labeled)
                if len(res) >= max_routes:
                    break
            continue

        if depth == max_hops:
            continue

        # explore neighbors
        for nxt, _fee in g.get(cur, []):
            if nxt in path:
                continue
            if best_depth.get(nxt, 99) <= depth + 1:
                continue
            best_depth[nxt] = depth + 1
            q.append((nxt, path + [nxt]))

    return res


def _fee_bps_to_pct(fee_bps: int) -> str:
    """
    500 -> '0.05%'
    3000 -> '0.30%'
    10000 -> '1.00%'
    """
    # fee_bps here is Uniswap v3 fee *in bps*? On Harmony you're calling pools "500" for 0.05%.
    # That's 500 = 0.05% not 5%. So formula:
    # fee_percent = fee_bps / 10000
    pct = (fee_bps / 10000.0) * 100.0  # actually (500 / 10000)=0.05 then *100? we want "0.05%".
    # safer: show with 2 decimals if >=1%, else 2-3 decimals:
    if pct >= 1:
        return f"{pct:.2f}%"
    else:
        return f"{pct:.2f}%".rstrip("0").rstrip(".")

def humanize_path(labeled_path: List[str]) -> Dict[str, str]:
    """
    Input: ['1USDC','WONE@500','1ETH@3000']
    Output:
      {
        "display": "1USDC → WONE@0.05% → 1ETH@0.30%",
        "fee_total_pct": "~0.35%"
      }
    fee_total_pct is simple sum of the hop fee percents.
    """
    hops = []
    total_pct_float = 0.0
    for i, hop in enumerate(labeled_path):
        if i == 0:
            hops.append(hop)  # first one is just token
            continue
        # hop like "WONE@500"
        if "@" in hop:
            sym, raw = hop.split("@", 1)
            try:
                bps = int(raw)
            except:
                bps = None
            if bps is not None:
                # convert bps (500) => 0.05%
                pct = (bps / 10000.0)  # 500 -> 0.05
                total_pct_float += pct
                hops.append(f"{sym}@{_fee_bps_to_pct(bps)}")
            else:
                hops.append(hop)
        else:
            hops.append(hop)

    total_pct_disp = f"~{total_pct_float:.2%}"  # 0.0035 -> "0.35%"

    return {
        "display": " → ".join(hops),
        "fee_total_pct": total_pct_disp,
    }
