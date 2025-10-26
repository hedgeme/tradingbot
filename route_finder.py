# app/route_finder.py
"""
Lightweight route discovery over config.POOLS_V3 with optional 'force_via' constraint.

We:
- Build a token graph from POOLS_V3 keys like '1USDC/WONE@500'
- Find direct + 2-hop paths
- Label hops with the *lowest* fee tier available
- Return paths like ['1USDC','WONE@500','1ETH@3000']

telegram_listener will:
- Turn 500 -> "0.05%" etc for human-readable display
- Sum total fee % for the path
- Attach impact/estOut/etc using quotes
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
    Graph of token -> [(neighbor_token, feeTier), ...]
    We treat pools as undirected for discovery (quotes will encode direction).
    """
    g: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for key in getattr(C, "POOLS_V3", {}).keys():
        try:
            a, b, fee = _parse_pool_key(key)
        except Exception:
            continue
        g[a].append((b, fee))
        g[b].append((a, fee))
    return g


def _label_with_lowest_fees(path_tokens: List[str], g: Dict[str, List[Tuple[str, int]]]) -> Optional[List[str]]:
    """
    ['1USDC','WONE','1ETH'] -> ['1USDC','WONE@500','1ETH@3000']
    We pick the minimum fee tier edge between each hop.
    """
    if len(path_tokens) < 2:
        return None
    out: List[str] = [path_tokens[0]]
    for u, v in zip(path_tokens[:-1], path_tokens[1:]):
        fees = [fee for (nbr, fee) in g.get(u, []) if nbr == v]
        if not fees:
            return None
        out.append(f"{v}@{min(fees)}")
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
    Return up to max_routes candidate paths.

    token_in='1USDC'
    token_out='1ETH'
    -> e.g. [
        ['1USDC','WONE@500','1ETH@3000'],
        ['1USDC','1sDAI@500','1ETH@3000'],
    ]

    If force_via='WONE', we ONLY keep paths where WONE is an intermediate node.
    """
    token_in = token_in.strip()
    token_out = token_out.strip()
    if token_in == token_out:
        return [[token_in]]

    g = _build_graph()
    if token_in not in g:
        return []

    res: List[List[str]] = []
    # BFS with memory of minimal depth visited
    q = deque([(token_in, [token_in])])
    visited_depth: Dict[str, int] = {token_in: 0}

    while q:
        cur, path = q.popleft()
        depth = len(path) - 1
        if depth > max_hops:
            continue

        if cur == token_out and 1 <= depth <= max_hops:
            labeled = _label_with_lowest_fees(path, g)
            if labeled:
                if force_via:
                    mids = set(path[1:-1])
                    if force_via not in mids:
                        # doesn't satisfy constraint
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

        for nxt, _fee in g.get(cur, []):
            if nxt in path:
                continue
            if visited_depth.get(nxt, 99) <= depth + 1:
                continue
            visited_depth[nxt] = depth + 1
            q.append((nxt, path + [nxt]))

    return res
