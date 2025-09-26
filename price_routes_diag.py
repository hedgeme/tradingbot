# /bot/app/price_routes_diag.py
# -*- coding: utf-8 -*-
"""
All-in-one diagnostic for pricing & routes on Harmony using VERIFIED_INFO.md + V3 pool math.

Changes vs previous:
- Uses Uniswap V3 pool ABI (slot0) instead of V2 getReserves().
- ONE is treated as WONE inside pools; output labeled as ONE.
- Resolves token symbols even if config.TOKENS and verified file differ (warns, but proceeds).
- Computes USD prices and prints per-leg V3 details.

Run:
  source ~/tecbot-venv/bin/activate
  python /bot/app/price_routes_diag.py

Optional RPC override:
  export HMY_RPC=https://api.s0.t.hmny.io
"""

from __future__ import annotations
import os, re, json
from typing import Dict, Tuple, List, Optional
from decimal import Decimal, getcontext

getcontext().prec = 60  # plenty for V3 price math

def _imp(m):
    try:
        return __import__(m, fromlist=['*'])
    except Exception:
        return __import__(f"app.{m}", fromlist=['*'])

config = _imp("config")

from web3 import Web3, HTTPProvider
from urllib.request import urlopen, Request

APP_DIR   = "/bot/app"
VERIFIED  = os.path.join(APP_DIR, "verified_info.md")
HTTP_TIMEOUT = 8

# Minimal Uniswap V3 pool ABI
V3_POOL_ABI = [
    {"inputs":[],"name":"slot0","outputs":[
        {"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},
        {"internalType":"int24","name":"tick","type":"int24"},
        {"internalType":"uint16","name":"observationIndex","type":"uint16"},
        {"internalType":"uint16","name":"observationCardinality","type":"uint16"},
        {"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},
        {"internalType":"uint8","name":"feeProtocol","type":"uint8"},
        {"internalType":"bool","name":"unlocked","type":"bool"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"liquidity","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"fee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],
     "stateMutability":"view","type":"function"},
]

def w3()->Web3:
    rpc = os.environ.get("HMY_RPC") or getattr(config,"RPC_URL","https://api.harmony.one")
    return Web3(HTTPProvider(rpc, request_kwargs={"timeout":HTTP_TIMEOUT}))

TOKENS_CFG: Dict[str,str]   = {k: Web3.to_checksum_address(v) for k,v in getattr(config,"TOKENS",{}).items()}
DECIMALS: Dict[str,int]     = getattr(config,"DECIMALS",{})
def dec(sym:str)->int:
    if sym == "ONE":  # ONE aliases WONE in pools
        return int(DECIMALS.get("WONE", 18))
    return int(DECIMALS.get(sym, 6 if sym=="1USDC" else 18))

# ---------- Parse VERIFIED_INFO.md ----------
def parse_verified():
    if not os.path.exists(VERIFIED):
        raise FileNotFoundError(f"{VERIFIED} not found")
    text = open(VERIFIED,"r",encoding="utf-8").read()
    tokens_file: Dict[str,str] = {}
    edges: Dict[Tuple[str,str], Tuple[str,str]] = {}  # (A,B) -> (pool_addr, fee_str)

    # token table
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and "Contract Address" not in s and "Asset" not in s and "Pools" not in s:
            m_addr = re.search(r"(0x[a-fA-F0-9]{40})", s)
            m_syms = re.findall(r"\|\s*([A-Za-z0-9]+)\s*\|", s)
            if m_addr and m_syms:
                sym=None
                for cand in reversed(m_syms):
                    if re.fullmatch(r"[A-Za-z0-9]+", cand):
                        sym=cand; break
                if sym:
                    tokens_file[sym] = Web3.to_checksum_address(m_addr.group(1))

    # pools table
    pool_section = False
    for line in text.splitlines():
        s=line.strip()
        if s.startswith("| Pool") and "Fee" in s:
            pool_section=True; continue
        if pool_section:
            if not s.startswith("|") or s.startswith("| ---"):
                continue
            # | 1ETH / WONE | `0x...` | 0.3% |
            m_addr = re.search(r"(0x[a-fA-F0-9]{40})", s)
            m_pair = re.search(r"\|\s*([A-Za-z0-9]+)\s*/\s*([A-Za-z0-9]+)\s*\|", s)
            m_fee  = re.search(r"\|\s*([0-9.]+)%\s*\|", s)
            if m_addr and m_pair and m_fee:
                a, b = m_pair.group(1), m_pair.group(2)
                addr = Web3.to_checksum_address(m_addr.group(1))
                fee  = m_fee.group(1) + "%"
                edges[(a,b)] = (addr, fee)
                edges[(b,a)] = (addr, fee)
    return tokens_file, edges

# ---------- Address ↔ symbol resolution helpers ----------
def all_symbol_to_addr() -> Dict[str,str]:
    out = dict(TOKENS_CFG)
    try:
        tokens_file, _ = parse_verified()
        for k,v in tokens_file.items():
            out.setdefault(k, v)
    except Exception:
        pass
    # normalize ONE -> WONE for pools
    if "ONE" in out and "WONE" in out:
        out["ONE"] = out["WONE"]
    return {k: Web3.to_checksum_address(v) for k,v in out.items()}

def addr_to_symbol_map() -> Dict[str,str]:
    rev = {}
    for sym, addr in all_symbol_to_addr().items():
        # prefer 1USDC over USDC aliasing etc; last write wins is fine since we only use known symbols
        rev[addr.lower()] = sym
    return rev

# ---------- V3 price math ----------
Q96 = Decimal(2) ** 96
def price_token1_per_token0_from_sqrtP(sqrtPriceX96: int) -> Decimal:
    # (sqrtP / 2^96)^2
    sp = Decimal(sqrtPriceX96) / Q96
    return sp * sp

def read_v3_pool_info(pool_addr: str) -> Dict[str,object]:
    c = w3().eth.contract(address=Web3.to_checksum_address(pool_addr), abi=V3_POOL_ABI)
    t0 = c.functions.token0().call()
    t1 = c.functions.token1().call()
    fee = c.functions.fee().call()
    liq = c.functions.liquidity().call()
    s0 = c.functions.slot0().call()
    sqrtP = int(s0[0])
    tick  = int(s0[1])
    return {
        "token0": Web3.to_checksum_address(t0),
        "token1": Web3.to_checksum_address(t1),
        "fee": int(fee),
        "liquidity": int(liq),
        "sqrtPriceX96": sqrtP,
        "tick": tick,
    }

def v3_mid_price(pool_addr: str, want_base_sym: str, want_quote_sym: str) -> Tuple[Decimal, Dict[str,object]]:
    """
    Return mid price of 1 want_base_sym in want_quote_sym using V3 slot0.
    Handles ONE->WONE aliasing automatically.
    """
    # Resolve symbols to pool addresses
    sym2addr = all_symbol_to_addr()
    base_sym = "WONE" if want_base_sym == "ONE" else want_base_sym
    quote_sym = "WONE" if want_quote_sym == "ONE" else want_quote_sym
    if base_sym not in sym2addr or quote_sym not in sym2addr:
        raise KeyError(f"Unknown symbols {want_base_sym}/{want_quote_sym}")
    base_addr  = Web3.to_checksum_address(sym2addr[base_sym])
    quote_addr = Web3.to_checksum_address(sym2addr[quote_sym])

    info = read_v3_pool_info(pool_addr)
    t0, t1 = info["token0"], info["token1"]
    sqrtP  = info["sqrtPriceX96"]

    # raw price = token1 per token0 (before decimals)
    p1_per_0 = price_token1_per_token0_from_sqrtP(sqrtP)

    # adjust for decimals so that units are in actual tokens
    # price(1 token0 in token1) * 10^(dec1 - dec0)
    # If we need price(1 base in quote), check mapping of (base, quote) to (token0, token1)
    dec0 = 18  # defaults; we will patch via symbol mapping
    dec1 = 18

    # map pool token addresses to symbols to get decimals precisely
    addr2sym = addr_to_symbol_map()
    sym0 = addr2sym.get(t0.lower())
    sym1 = addr2sym.get(t1.lower())
    if sym0 and sym1:
        dec0 = dec(sym0)
        dec1 = dec(sym1)

    # price token0->token1 with decimals:
    p0_to_1 = p1_per_0 * (Decimal(10) ** (dec1 - dec0))
    # and inverse
    p1_to_0 = (Decimal(1) / p0_to_1) if p0_to_1 != 0 else Decimal("NaN")

    # Now choose orientation for (base, quote):
    if t0.lower() == base_addr.lower() and t1.lower() == quote_addr.lower():
        px = p0_to_1
    elif t1.lower() == base_addr.lower() and t0.lower() == quote_addr.lower():
        px = p1_to_0
    else:
        raise RuntimeError(f"Pool tokens do not match requested pair {want_base_sym}/{want_quote_sym}")

    # enrich info for debugging
    detail = {
        **info,
        "want_base": want_base_sym,
        "want_quote": want_quote_sym,
        "token0_sym": sym0 or t0,
        "token1_sym": sym1 or t1,
        "dec_token0": dec0,
        "dec_token1": dec1,
        "mid": str(px),
    }
    return px, detail

# ---------- Routing & composed prices ----------
def parse_pools_from_verified() -> Dict[Tuple[str,str], Tuple[str,str]]:
    _, edges = parse_verified()
    return edges  # (A,B) -> (pool_addr, fee_str)

def composed_v3_price(legs: List[Tuple[str,str,str]]) -> Tuple[Decimal, List[Dict[str,object]]]:
    prod = Decimal("1")
    dets=[]
    for a,b,pool in legs:
        px, d = v3_mid_price(pool, a, b)
        dets.append(d)
        prod *= px
    return prod, dets

def choose_best_route(options: List[List[Tuple[str,str,str]]]):
    best_idx = -1
    best_px  = Decimal("-1")
    best_det: List[Dict[str,object]] = []
    for i, legs in enumerate(options):
        try:
            px, det = composed_v3_price(legs)
        except Exception:
            continue
        if px > best_px:
            best_idx, best_px, best_det = i, px, det
    return best_idx, best_px, best_det

def cb_eth_usd() -> Optional[Decimal]:
    try:
        req = Request("https://api.coinbase.com/v2/prices/ETH-USD/spot", headers={"User-Agent":"tec/diag"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        amt = data.get("data", {}).get("amount")
        return Decimal(amt) if amt else None
    except Exception:
        return None

# ---------- Main diagnostic ----------
def main():
    w = w3()
    print("RPC:", w.provider.endpoint_uri)
    print("Chain:", w.eth.chain_id, "Block:", w.eth.block_number)

    # Warn about mismatches but proceed
    try:
        tokens_file, edges = parse_verified()
    except Exception as e:
        print("ERROR parsing VERIFIED_INFO.md:", e)
        return

    for sym, cfg_addr in TOKENS_CFG.items():
        v_addr = tokens_file.get(sym)
        if v_addr and v_addr.lower()!=cfg_addr.lower():
            print(f"WARNING: {sym} differs: config={cfg_addr} verified={v_addr}")

    def pool(a,b)->Optional[str]:
        return edges.get((a,b),(None,None))[0]

    # Needed pools (symbol space uses WONE where appropriate)
    usdc_wone = pool("1USDC","WONE")
    eth_wone  = pool("1ETH","WONE")
    tec_wone  = pool("TEC","WONE")
    sdai_usdc = pool("1sDAI","1USDC")
    tec_sdai  = pool("TEC","1sDAI")

    missing=[]
    if not usdc_wone: missing.append("WONE/1USDC")
    if not eth_wone:  missing.append("1ETH/WONE")
    if not sdai_usdc: missing.append("1sDAI/1USDC")
    if not tec_wone and not tec_sdai: missing.append("TEC bridge (TEC/WONE or TEC/1sDAI)")
    if missing:
        print("\nNOTE: Missing verified pools:", ", ".join(missing))

    # ONE/USD (really WONE/USDC)
    one_usd = Decimal("NaN"); legs_one=[]
    if usdc_wone:
        one_usd, legs_one = composed_v3_price([("ONE","1USDC", usdc_wone)])  # ONE aliases WONE

    # 1ETH/USD via 1ETH/WONE * ONE/USD
    eth_usd = Decimal("NaN"); legs_eth=[]
    if eth_wone and usdc_wone and one_usd==one_usd:
        eth_one, legs_eth = composed_v3_price([("1ETH","ONE", eth_wone)])
        eth_usd = eth_one * one_usd

    # 1sDAI/USD via 1sDAI/1USDC
    sdai_usd = Decimal("NaN"); legs_sdai=[]
    if sdai_usdc:
        sdai_usd, legs_sdai = composed_v3_price([("1sDAI","1USDC", sdai_usdc)])

    # TEC/USD best of two bridges
    tec_usd = Decimal("NaN"); legs_tec=[]; route_lbl=""
    options=[]
    if tec_wone and usdc_wone:
        options.append([("TEC","ONE", tec_wone), ("ONE","1USDC", usdc_wone)])
    if tec_sdai and sdai_usdc:
        options.append([("TEC","1sDAI", tec_sdai), ("1sDAI","1USDC", sdai_usdc)])
    if options:
        idx, px, det = choose_best_route(options)
        tec_usd, legs_tec = px, det
        route_lbl = "via ONE" if idx==0 else "via 1sDAI" if idx==1 else "(picked)"

    # -------- Report ----------
    print("\n================ PRICE REPORT (V3 slot0; ONE=WONE) ================")
    print(f"ONE/USD : {('%.6f' % one_usd) if one_usd==one_usd else '—'}  (from WONE/1USDC pool)")
    print(f"1ETH/USD: {('%.2f'  % eth_usd) if eth_usd==eth_usd else '—'}  (via 1ETH/WONE)")
    print(f"1sDAI/USD: {('%.4f' % sdai_usd) if sdai_usd==sdai_usd else '—'}  (via 1sDAI/1USDC)")
    print(f"TEC/USD : {('%.6f' % tec_usd) if tec_usd==tec_usd else '—'}  {('['+route_lbl+']') if route_lbl else ''}")

    cb = cb_eth_usd()
    if cb and eth_usd==eth_usd:
        diff = abs((eth_usd - cb)/cb) * 100
        print(f"\nCoinbase ETH/USD: {cb:.2f}  | diff={diff:.2f}%")

    # Details
    def dump(title: str, dets: List[Dict[str,object]]):
        if not dets: return
        print(f"\n-- {title} --")
        for d in dets:
            print(f"pool {d['token0_sym']}/{d['token1_sym']} @ {d['fee']}  addr={d.get('pool','?')}")
            print(f"  token0={d['token0']}  token1={d['token1']}")
            print(f"  sqrtPriceX96={d['sqrtPriceX96']}  tick={d['tick']}  liquidity={d['liquidity']}")
            print(f"  leg: {d['want_base']} -> {d['want_quote']}  mid={d['mid']}")

    dump("ONE/USD leg (WONE/1USDC)", legs_one)
    dump("1ETH->ONE leg (1ETH/WONE)", legs_eth)
    dump("1sDAI->1USDC leg", legs_sdai)
    dump("TEC route legs (chosen)", legs_tec)

if __name__ == "__main__":
    main()
