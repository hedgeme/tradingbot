# /bot/app/telegram_listener.py
import os
import logging
from decimal import Decimal
from dotenv import load_dotenv

# telegram (python-telegram-bot v13 style)
from telegram.ext import Updater, CommandHandler
from telegram import ParseMode

# project imports
from app.wallet import WALLETS, get_native_balance_wei, get_erc20_balance_wei
from app.trade_executor import get_token_address
from app.price_feed import fetch_lp_quotes

# --- Env & logging ---
load_dotenv("/home/tecviva/.env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_listener")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# --- Formatting helpers ---

def fmt_amount(wei_or_smallest: int, decimals: int = 18) -> str:
    """
    Convert integer (wei / smallest units) to human string with fixed decimals.
    """
    if wei_or_smallest is None:
        wei_or_smallest = 0
    try:
        val = Decimal(int(wei_or_smallest)) / (Decimal(10) ** decimals)
    except Exception:
        val = Decimal(0)
    return f"{val:,.4f}"

# Which symbols to display (ORDER MATTERS) for each strategy wallet
PROFILE_SYMBOLS = {
    "tecbot_eth":  ["ONE", "1ETH"],
    "tecbot_usdc": ["ONE", "1USDC"],
    "tecbot_sdai": ["ONE", "1ETH", "TEC", "1USDC"],
    "tecbot_tec":  ["ONE", "TEC", "1sDAI"],
}

# Decimals for formatting human-readable amounts
TOKEN_DECIMALS = {
    "ONE":   18,
    "1ETH":  18,
    "1USDC": 6,
    "1sDAI": 18,
    "TEC":   18,
}

# Allow old/internal naming differences without breaking output
WALLET_KEY_FALLBACKS = {
    "tecbot_eth":  ["tecbot_eth", "eth", "ETH", "wallet_eth", "bot_eth"],
    "tecbot_usdc": ["tecbot_usdc", "usdc", "USDC", "wallet_usdc", "bot_usdc"],
    "tecbot_sdai": ["tecbot_sdai", "sdai", "SDAI", "1sDAI_wallet", "bot_sdai"],
    "tecbot_tec":  ["tecbot_tec", "tec", "TEC", "wallet_tec", "bot_tec"],
}

# Map visible symbols to resolver aliases in case get_token_address() uses different spellings
TOKEN_ALIASES = {
    "1ETH":  ["1ETH", "ETH1", "ETH", "hETH"],
    "1USDC": ["1USDC", "USDC1", "USDC"],
    "1sDAI": ["1sDAI", "sDAI", "SDAI", "1SDAI"],
    "TEC":   ["TEC", "Tec", "tec"],
}

# --- Commands ---

def ping(update, context):
    update.message.reply_text("pong ✅\nServer IP active")

def _resolve_wallet_address(profile_key: str):
    """
    Try several possible keys in app.wallet.WALLETS so we don't silently lose balances
    if naming changed.
    """
    for k in WALLET_KEY_FALLBACKS.get(profile_key, [profile_key]):
        addr = WALLETS.get(k)
        if addr:
            if k != profile_key:
                logger.warning("balances: using fallback wallet key '%s' for '%s'", k, profile_key)
            return addr
    logger.warning("balances: no wallet address found for '%s' (tried: %s)", profile_key, WALLET_KEY_FALLBACKS.get(profile_key))
    return None

def _resolve_token_address(symbol: str):
    """
    Try the symbol and known aliases with get_token_address().
    """
    # Native ONE has no token address
    if symbol == "ONE":
        return None

    # Try primary, then aliases
    tried = []
    for candidate in [symbol] + TOKEN_ALIASES.get(symbol, []):
        if candidate in tried:
            continue
        tried.append(candidate)
        try:
            addr = get_token_address(candidate)
        except Exception as e:
            logger.debug("balances: get_token_address('%s') error: %s", candidate, e)
            addr = None
        if addr:
            if candidate != symbol:
                logger.warning("balances: token '%s' resolved via alias '%s'", symbol, candidate)
            return addr

    logger.warning("balances: no token address for '%s' (tried: %s)", symbol, tried)
    return None

def balances(update, context):
    """
    Prints each strategy wallet with a fixed set of symbols, in a fixed order,
    and ALWAYS includes zeros so you can sanity-check funding at a glance.
    """
    lines = []

    for profile in ["tecbot_eth", "tecbot_usdc", "tecbot_sdai", "tecbot_tec"]:
        addr = _resolve_wallet_address(profile)
        symbols = PROFILE_SYMBOLS.get(profile, [])

        # Header (Markdown bold)
        lines.append(f"*{profile}*")

        # Build dict of symbol -> (amount_in_smallest_units, decimals)
        sym_amounts = {}

        # Native ONE
        if addr:
            try:
                one_wei = get_native_balance_wei(addr)
            except Exception as e:
                logger.warning("balances: ONE get_native_balance_wei failed for %s: %s", profile, e)
                one_wei = 0
        else:
            one_wei = 0
        sym_amounts["ONE"] = (one_wei, TOKEN_DECIMALS["ONE"])

        # ERC-20s as requested for the wallet
        for sym in symbols:
            if sym == "ONE":
                continue
            dec = TOKEN_DECIMALS.get(sym, 18)
            amt_smallest = 0
            token_addr = _resolve_token_address(sym)
            if addr and token_addr:
                try:
                    amt_smallest = get_erc20_balance_wei(token_addr, addr)
                except Exception as e:
                    logger.warning("balances: %s balance fetch failed for %s: %s", sym, profile, e)
            elif addr and not token_addr:
                # We'll log in the resolver already; keep zero
                pass
            sym_amounts[sym] = (amt_smallest, dec)

        # Render in exact order
        for sym in symbols:
            amt_smallest, dec = sym_amounts.get(sym, (0, TOKEN_DECIMALS.get(sym, 18)))
            lines.append(f"  {sym:>5} {fmt_amount(amt_smallest, decimals=dec)}")

        lines.append("")  # blank line between wallet sections

    text = "\n".join(lines).rstrip()
    update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

def prices(update, context):
    """
    Prints price/quote info from your existing LP + Coinbase fetcher.
    """
    try:
        quotes = fetch_lp_quotes()
    except Exception as e:
        logger.exception("prices: fetch_lp_quotes failed")
        update.message.reply_text(f"Prices ❌\n{e}")
        return

    lines = ["Prices ✅"]
    for token, val in quotes.items():
        if isinstance(val, dict):
            lines.append(f"{token:5s} | Harmony {val.get('harmony')} | CB {val.get('coinbase')}")
        else:
            lines.append(f"{token:5s} | {val}")
    update.message.reply_text("\n".join(lines))

# --- Main ---

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("ping", ping))
    dp.add_handler(CommandHandler("balances", balances))
    dp.add_handler(CommandHandler("prices", prices))

    updater.start_polling()
    logger.info("Telegram bot started")
    updater.idle()

if __name__ == "__main__":
    main()
