TECBot Ops Post-Mortem & Runbook (Scope: /start, /help, /ping, /balances, /prices, /slippage, /assets, /sanity, /version)

0) What went wrong (quick list)

Prices mixed up unit totals vs. per-unit (e.g., TEC showing price for 100 TEC).

ETH midpoint (“mid”) derived from the wrong side of the market (sell vs. buy) and from noisy signals, skewing slippage.

Native ONE vs WONE references drifted; ONE balances occasionally read as zero or duplicated.

Quoter path assembly had fee/pair/ordering pitfalls and occasional pool direction inversions.

Slippage table used unstable mids and estimated input sizes poorly → flat or nonsense slippage rows.

Telegram tables lost alignment when formats changed (or when numbers grew) and when not wrapped with <pre>.

All of these are now addressed, and the bot is stable.




1) Correct prices for assets
Problems

Per-unit confusion: Some assets (e.g., TEC) were priced using a quote for 100 units but displayed as if it were a single unit.

Route choice: TEC had two viable routes (TEC→WONE→USDC vs TEC→1sDAI→USDC); we weren’t consistently picking the best outcome.

ETH bias: Using only forward (sell) quotes sometimes understated price vs. real buy side.

Resolutions

Per-unit normalization: For assets with a larger quote basis, we divide by the basis before displaying:

Example: out_usdc = price_usd("TEC", basis=100) → per_unit = out_usdc / 100.

Best-of routes for TEC: For 1 TEC, we probe both:

TEC -> WONE (1%) -> 1USDC (0.3%)

TEC -> 1sDAI (1%) -> 1USDC (0.05%)
and pick the better USDC per TEC.

ETH display (LP price): We compute a reverse tiny-probe set (25/50/100/250 USDC → ETH) and take the most favorable implied price (smallest USDC/ETH) for display. This keeps /prices close to reality and Coinbase.

Sanity script (per-unit check):

# Quick check for TEC (per-unit vs total)
from decimal import Decimal
from app import prices as PR
tot = PR.price_usd("TEC", Decimal("100"))
print("Per 1 TEC:", (tot/Decimal(100)))




2) Correct midpoints for assets
Problems

ETH mid used a forward 1 ETH sale or slot0 math; both were unstable/inverted at times.

ONE mid depended on native ONE without bridging to WONE pricing.

Mid ≠ tradable “fair” price → slippage vs mid was meaningless.

Resolutions

ETH mid: Use reverse tiny probes via USDC→WONE→1ETH for small USDC sizes and take the best implied price.

ONE mid: Use WONE→1USDC (per 1 WONE) for the mid; treat ONE as WONE for pricing.

Other tokens mid: Use price_usd(token, 1) per-unit (post-fix).

Sanity script (ETH reverse curve):

# USDC -> ETH tiny reverse probes
sizes = [25, 50, 100, 250]
for s in sizes:
    # compute eth_out for USDC s and implied price s/eth_out
    ...



3) Correct references for wallets and contract addresses
Problems

Balances showed duplicate ONE (ONE(native)/ONE/WONE) or missing native ONE.

The display sometimes read the wrong key (ONE(native) vs ONE) and rounded dust to zero.

Resolutions

Tolerant ONE resolver: We search row keys in priority order:

"ONE(native)", "ONE (native)", "ONE_NATIVE", "NATIVE_ONE", "NATIVE", "ONE", "WONE"


First found is used for the ONE column.

Decimal display policy:

1ETH: keep 8 decimals.

Others: 2 decimals (clip).

(Optional extension the team agreed on: show 4 decimals only if value < 1 to surface dust; not enabled by default.)

No duplicates: We only display the agreed columns: ONE, 1USDC, 1ETH, TEC, 1sDAI.

Sanity script (native ONE only):

from decimal import Decimal
from web3 import Web3
from app.chain import get_ctx
import config as C

w = list(C.WALLETS.values())[0]
wei = get_ctx(C.HARMONY_RPC).w3.eth.get_balance(Web3.to_checksum_address(w))
print("ONE:", Decimal(wei)/Decimal(1e18))




4) Correct integration of the Quoter
Problems

Path construction (token ordering, fee bytes, and pair direction) caused inversion/head-tail mismatches.

Slot0 math used the wrong token orientation occasionally.

Resolutions

Explicit path encoding (V3):

bytes = tokenA (20) + fee (3) + tokenB (20) [+ fee (3) + tokenC (20) ...]

Fees in big-endian 3-byte.

Address checksumming: Always Web3.to_checksum_address.

Token order: Use real token order of the intended route; don’t “sort by address” when calling Quoter.

Guardrails: If both forward and reverse probes exist for an asset, prefer the one that matches observed L2 price (e.g., Coinbase for ETH) or use the more conservative side.

Sanity script (pool lookup):

# Verify factory -> pool matches config
# and that token0/token1 match expectations before slot0 math




5) Correct slippage calculation
Problems

Slippage rows were flat or nonsense because:

Mid was bad (see §2).

Estimated input amounts were based on that bad mid.

For ONE, we didn’t bridge via WONE for quotes.

Resolutions

Mid fix: See §2 (reverse tiny probes for ETH; WONE mid for ONE).

Estimate input per target USDC: est_in = target_usdc / mid (quantized to 1e-6) and then re-quote for the effective price:

eff = price_usd(token_in, est_in) / est_in

slip% = (eff - mid) / mid * 100

ONE quoting: When /slippage ONE, we transparently quote via WONE so results aren’t blank.

Sanity script (slippage preview):

# For token_in='1ETH' or 'ONE', compute mid and then targets [10,100,1000,10000]
# Double-check eff vs mid monotonicity.



6) Output tables in the correct format (Telegram-optimized)
Problems

Inconsistent widths, no <pre> wrapping, too many decimals → misalignment in Telegram.

Resolutions

Wrap every table with <pre> ... </pre> and use monospaced alignment.

Fixed widths per column for all tables (kept exactly as approved).

Decimal policy as in §3, and thousands separators for big dollar amounts in prices.

No layout changes without approval. Formats are now locked.



7) Current stable behaviors

All of these are working and aligned with the agreed formats:

/start — basic intro

/help — command list

/ping — simple health + version

/balances — Option A layout, ONE column shows native ONE via tolerant resolver; 2 decimals except 1ETH (8)

/prices — Option B layout, per-unit prices, best route for TEC, ETH LP vs Coinbase comparison

/slippage — Option B layout, stable mids (ETH reverse tiny-probe; ONE via WONE), slippage changes with size

/assets — tokens + wallets, clean monospace block

/sanity — quick config & module availability

/version — version and git short hash (if available)




8) Field diagnostics (copy-paste mini-kit)

A) Verify TEC per-unit price and route choice


from decimal import Decimal
from web3 import Web3
import config as C
from app.chain import get_ctx
from app import prices as PR

ABI=[{"inputs":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"uint256","name":"amountIn","type":"uint256"}],
"name":"quoteExactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]
def addr(s): return Web3.to_checksum_address(PR._addr(s))
def fee3(f): return int(f).to_bytes(3,'big')
q = get_ctx(C.HARMONY_RPC).w3.eth.contract(address=Web3.to_checksum_address(C.QUOTER_ADDR),abi=ABI)

amt=Decimal("1"); dec_t=PR._dec("TEC"); dec_u=PR._dec("1USDC"); wei=int(amt*(Decimal(10)**dec_t))
pathW=(Web3.to_bytes(hexstr=addr("TEC"))+fee3(10000)+Web3.to_bytes(hexstr=addr("WONE"))+fee3(3000)+Web3.to_bytes(hexstr=addr("1USDC")))
pathD=(Web3.to_bytes(hexstr=addr("TEC"))+fee3(10000)+Web3.to_bytes(hexstr=addr("1sDAI"))+fee3(500)+Web3.to_bytes(hexstr=addr("1USDC")))
pxW=q.functions.quoteExactInput(pathW,wei).call()[0]/10**dec_u
pxD=q.functions.quoteExactInput(pathD,wei).call()[0]/10**dec_u
print("TEC via WONE:", pxW, "via 1sDAI:", pxD, "best:", max(pxW, pxD))



B) Verify ETH reverse tiny-probe mid

from decimal import Decimal
from web3 import Web3
import config as C
from app.chain import get_ctx
from app import prices as PR

ABI=[{"inputs":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"uint256","name":"amountIn","type":"uint256"}],
"name":"quoteExactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}]
def addr(s): return Web3.to_checksum_address(PR._addr(s))
def fee3(f): return int(f).to_bytes(3,'big')
q=get_ctx(C.HARMONY_RPC).w3.eth.contract(address=Web3.to_checksum_address(C.QUOTER_ADDR),abi=ABI)
dec_e=PR._dec("1ETH"); dec_u=PR._dec("1USDC")
for s in [25,50,100,250]:
    wei=int(Decimal(s)*(Decimal(10)**dec_u))
    path=(Web3.to_bytes(hexstr=addr("1USDC"))+fee3(3000)+Web3.to_bytes(hexstr=addr("WONE"))+fee3(3000)+Web3.to_bytes(hexstr=addr("1ETH")))
    out=q.functions.quoteExactInput(path,wei).call()[0]
    eth=Decimal(out)/(Decimal(10)**dec_e)
    print(s, "USDC ->", eth, "ETH  implied:", s/eth, "USDC/ETH")



C) Verify native ONE balance

from decimal import Decimal
from web3 import Web3
from app.chain import get_ctx
import config as C

ctx=get_ctx(C.HARMONY_RPC)
for name, addr in C.WALLETS.items():
    wei = ctx.w3.eth.get_balance(Web3.to_checksum_address(addr))
    print(name, "ONE:", Decimal(wei)/Decimal(1e18))



9) Table format rules (Telegram)

Always wrap with <pre> ... </pre> to keep monospaced alignment.

Keep fixed column widths (locked).

Prices: $ with comma separators; 2 decimals ≥ $1000, else up to 5 decimals (trim trailing).

Balances: 1ETH shows 8 decimals; others show 2 decimals.

Don’t change layout without explicit approval.





10) Quick troubleshooting checklist

ETH looks off in /prices?

Run the reverse tiny-probe (8B). Check best implied vs Coinbase.

If pool liquidity is skewed, the forward side will diverge; we display the reverse tiny-probe.

TEC price looks off?

Run (8A) and confirm best-of route. Ensure fees: TEC→WONE 10000, WONE→USDC 3000; TEC→1sDAI 10000, 1sDAI→USDC 500.

/slippage flat lines?

Verify mid (8B for ETH, WONE mid for ONE).

Ensure targets = [10,100,1000,10000] recalc est_in each time.

ONE balance missing?

Run (8C). If non-zero on chain but shows 0.00, check rounding/display; verify tolerant key maps to a real value.

Alignment off in Telegram?

Confirm <pre> wrapping and column widths in the handler.

Don’t use Markdown tables; stick to monospaced text.




11) Key decisions to preserve

ETH mid = reverse tiny-probe min(USDC/ETH) over {25,50,100,250 USDC}.

ONE priced via WONE; treat native ONE as WONE for pricing/slippage.

TEC uses best-of (via WONE vs via 1sDAI).

Per-unit display for all assets; when quoting with basis (e.g., 100 TEC), divide by basis for presentation.

Telegram layout locked; no unapproved format changes.




12) Files touched (high-level)

app/prices.py

Normalizes per-unit display, best-of TEC routes, ETH reverse tiny-probe helper used for display and mid support where applicable.

app/balances.py

Stable native balance reading; no format logic here—just values.

telegram_listener.py

Keeps approved formats; fixes mid logic in /slippage; ensures WONE usage for ONE; Coinbase compare for ETH; <pre> wrapping everywhere.


Final note

The current versions follow the principles above. When adding new assets or pools:

Declare tokens/decimals/pools in config.

Verify pool direction (token0/token1) before using slot0 math.

If multiple viable routes exist, add a best-of probe unless you explicitly want a single canonical path.

Keep table formats untouched unless we agree to change them.



saved in repo and chat project manager. ops-runbook.md












































































































