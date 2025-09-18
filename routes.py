# /bot/app/routes.py
"""
Declarative routing matrix for all four bots.

Each route is a tuple of (tokens, fees):
- tokens: list of symbol strings in hop order, e.g. ["1USDC", "WONE", "1sDAI"]
- fees:   list of uint24 pool fees between tokens, e.g. [500, 500]
  (len(fees) == len(tokens) - 1)

These are the pools you already validated in your quote sanity:
Single-hop:
  1ETH -> WONE  (3000)
  1USDC -> 1sDAI (500)
  1USDC -> WONE  (500)
  TEC -> WONE    (10000)

Multi-hop:
  1ETH -> WONE (3000) -> 1USDC (500)
"""

from typing import List, Tuple, Dict

# ----- Route definitions (symbols + fees) -----

# Single-hop routes
ROUTE_ETH_WONE_3000: Tuple[List[str], List[int]] = (["1ETH", "WONE"], [3000])
ROUTE_USDC_SDAI_500: Tuple[List[str], List[int]] = (["1USDC", "1sDAI"], [500])
ROUTE_USDC_WONE_500: Tuple[List[str], List[int]] = (["1USDC", "WONE"], [500])
ROUTE_TEC_WONE_10000: Tuple[List[str], List[int]] = (["TEC", "WONE"], [10000])

# Multi-hop example
ROUTE_ETH_WONE_USDC_3000_500: Tuple[List[str], List[int]] = (["1ETH", "WONE", "1USDC"], [3000, 500])

# ----- Bot → routes mapping (what each strategy is allowed to use) -----

BOT_ROUTES: Dict[str, List[Tuple[List[str], List[int]]]] = {
    # Bot #1: tecbot_eth (core: 1ETH <-> WONE)
    "tecbot_eth": [
        ROUTE_ETH_WONE_3000,
        ROUTE_ETH_WONE_USDC_3000_500,  # multihop out to USDC (optional for quotes/health)
    ],
    # Bot #2: tecbot_usdc (USDC stable leg)
    "tecbot_usdc": [
        ROUTE_USDC_SDAI_500,
        ROUTE_USDC_WONE_500,
    ],
    # Bot #3: tecbot_sdai (sDAI leg; may interact with USDC)
    "tecbot_sdai": [
        ROUTE_USDC_SDAI_500,
        ROUTE_USDC_WONE_500,  # for routing visibility
    ],
    # Bot #4: tecbot_tec (TEC <-> WONE core)
    "tecbot_tec": [
        ROUTE_TEC_WONE_10000,
    ],
}

def all_sanity_routes() -> List[Tuple[List[str], List[int]]]:
    """
    Flatten & de-duplicate all routes we want to sanity check (quotes only).
    """
    seen = set()
    result: List[Tuple[List[str], List[int]]] = []
    for routes in BOT_ROUTES.values():
        for tokens, fees in routes:
            key = (tuple(tokens), tuple(fees))
            if key not in seen:
                seen.add(key)
                result.append((tokens, fees))
    return result

if __name__ == "__main__":
    # Quick visual check if you invoke this file directly.
    import json
    print(json.dumps({k: v for k, v in BOT_ROUTES.items()}, indent=2))
    print("\nSanity routes:")
    for tokens, fees in all_sanity_routes():
        print(f" - {tokens} | {fees}")

