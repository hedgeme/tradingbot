#!/usr/bin/env python3
# app/alert.py — Telegram alerts (clean formatting, symbols + explorer link)

import os
import html
import requests
from web3 import Web3

# --- ENV ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Harmony explorer link
def explorer_tx_url(tx_hash: str) -> str:
    if not tx_hash:
        return ""
    h = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash
    return f"https://explorer.harmony.one/tx/{h}"

# Known token symbols for pretty printing (ETH-format, checksum)
TOKENS = {
    "WONE":  Web3.to_checksum_address("0xcF664087a5bB0237a0BAd6742852ec6c8d69A27a"),
    "1ETH":  Web3.to_checksum_address("0x4cC435d7b9557d54d6EF02d69Bbf72634905Bf11"),
    "1USDC": Web3.to_checksum_address("0xBC594CABd205bD993e7FfA6F3e9ceA75c1110da5"),
    "TEC":   Web3.to_checksum_address("0x0DEB9A1998aAE32dAAcF6de21161c3E942aCe074"),
    "1sDAI": Web3.to_checksum_address("0xeDEb95D51dBc4116039435379Bd58472A2c09b1f"),
}
ADDR_TO_SYMBOL = {addr: sym for sym, addr in TOKENS.items()}

def sym_for(addr_or_sym: str) -> str:
    """Return a friendly symbol for an input that may be an address or already a symbol."""
    if not addr_or_sym:
        return "?"
    if addr_or_sym.startswith("0x"):
        try:
            cs = Web3.to_checksum_address(addr_or_sym)
            return ADDR_TO_SYMBOL.get(cs, cs)
        except Exception:
            return addr_or_sym
    return addr_or_sym  # already a symbol/path like "1USDC->1sDAI"

# --- Telegram send helper ---
def _send(text_html: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception:
        pass

# --- Public alert functions (used by executor & preflight) ---
def send_alert(msg: str) -> None:
    _send(html.escape(msg))

def alert_preflight_ok() -> None:
    _send("✅ <b>Preflight OK</b> — wallets, GPG, and RPC look good.")

def alert_preflight_fail(reason: str) -> None:
    reason = html.escape(reason)
    _send(f"❌ <b>Preflight FAILED</b>\n<code>{reason}</code>")

def alert_trade_success(
    pair_or_in: str,
    action: str,
    amt_in: str,
    amt_out_min: str,
    tx_hash: str | None = None,
    filled_actual: str | None = None,
) -> None:
    """
    amt_in / amt_out_min / filled_actual are expected to be human-readable strings
    (e.g. '0.50 1USDC', '0.43 1sDAI').
    """
    # pair_or_in may be "0x...->0x..." or "1USDC->1sDAI"
    pretty = (
        "->".join(sym_for(p.strip()) for p in pair_or_in.split("->"))
        if "->" in pair_or_in
        else sym_for(pair_or_in)
    )
    link = explorer_tx_url(tx_hash or "")
    msg  = (
        f"✅ <b>Trade OK</b>\n"
        f"<b>Action:</b> {html.escape(action)}\n"
        f"<b>Pair:</b> {html.escape(pretty)}\n"
        f"<b>amountIn:</b> {html.escape(str(amt_in))}\n"
        f"<b>amountOutMin:</b> {html.escape(str(amt_out_min))}"
    )
    if filled_actual:
        msg += f"\n<b>filled:</b> {html.escape(str(filled_actual))}"
    if link:
        msg += f"\n<b>tx:</b> <a href='{link}'>{html.escape(tx_hash)}</a>"
    _send(msg)

def alert_trade_failure(pair_or_in: str, action: str, reason: str, tx_hash: str | None = None) -> None:
    pretty = (
        "->".join(sym_for(p.strip()) for p in pair_or_in.split("->"))
        if "->" in pair_or_in
        else sym_for(pair_or_in)
    )
    link = explorer_tx_url(tx_hash or "")
    msg  = (
        f"❌ <b>Trade FAILED</b>\n"
        f"<b>Action:</b> {html.escape(action)}\n"
        f"<b>Pair:</b> {html.escape(pretty)}\n"
        f"<b>Reason:</b> <code>{html.escape(reason)}</code>"
    )
    if link:
        msg += f"\n<b>tx:</b> <a href='{link}'>{html.escape(tx_hash)}</a>"
    _send(msg)

def alert_low_balance(wallet_name: str, symbol: str, balance_str: str, threshold_str: str) -> None:
    msg = (
        f"⚠️ <b>Low balance</b>\n"
        f"<b>Wallet:</b> {html.escape(wallet_name)}\n"
        f"<b>Asset:</b> {html.escape(symbol)}\n"
        f"<b>Balance:</b> {html.escape(balance_str)} (min {html.escape(threshold_str)})"
    )
    _send(msg)
