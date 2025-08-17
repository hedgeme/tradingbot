import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock

try:
    from dotenv import load_dotenv
except ImportError:
    raise SystemExit("Please `pip install python-dotenv` before running wallet.py")

# ---------- Load env ----------
ENV_PATH = "/home/tecviva/.env"
if not Path(ENV_PATH).exists():
    raise SystemExit(f".env not found at {ENV_PATH}")
load_dotenv(ENV_PATH)

HMY_NODE       = os.getenv("HMY_NODE", "https://api.s0.t.hmny.io")
HMY_CHAIN_ID   = os.getenv("HMY_CHAIN_ID", "1666600000")
FROM_SHARD     = os.getenv("HMY_FROM_SHARD", "0")
TO_SHARD       = os.getenv("HMY_TO_SHARD", "0")
SECRETS_DIR    = os.getenv("SECRETS_DIR", "/home/tecviva/.secrets")
BOT_DB_DIR     = os.getenv("BOT_DB_DIR", "/bot/db")
BOT_LOG_DIR    = os.getenv("BOT_LOG_DIR", "/bot/logs")

# Wallets (names + ONE addresses)
WALLETS = {
    "tecbot_eth": {
        "name": os.getenv("WALLET_ETH_NAME", "tecbot_eth"),
        "one":  os.getenv("WALLET_ETH_ADDR", ""),
        "secret": f"{SECRETS_DIR}/tecbot_eth.pass.gpg",
    },
    "tecbot_sdai": {
        "name": os.getenv("WALLET_SDAI_NAME", "tecbot_sdai"),
        "one":  os.getenv("WALLET_SDAI_ADDR", ""),
        "secret": f"{SECRETS_DIR}/tecbot_sdai.pass.gpg",
    },
    "tecbot_tec": {
        "name": os.getenv("WALLET_TEC_NAME", "tecbot_tec"),
        "one":  os.getenv("WALLET_TEC_ADDR", ""),
        "secret": f"{SECRETS_DIR}/tecbot_tec.pass.gpg",
    },
    "tecbot_usdc": {
        "name": os.getenv("WALLET_USDC_NAME", "tecbot_usdc"),
        "one":  os.getenv("WALLET_USDC_ADDR", ""),
        "secret": f"{SECRETS_DIR}/tecbot_usdc.pass.gpg",
    },
}

# Simple in-process locks to avoid nonce/CLI races per wallet
_WALLET_LOCKS = {k: Lock() for k in WALLETS.keys()}

# ---------- Helpers ----------
def _run(cmd:list[str], *, input_text:str|None=None, timeout:int=60) -> subprocess.CompletedProcess:
    """
    Run a command securely. Returns CompletedProcess. Raises on non-zero.
    """
    try:
        cp = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}")

    if cp.returncode != 0:
        # sanitize: never include secrets in errors
        stderr = (cp.stderr or "").strip()
        stdout = (cp.stdout or "").strip()
        raise RuntimeError(f"hmy error ({' '.join(cmd)}): {stderr or stdout}")
    return cp

def gpg_is_unlocked(test_secret_file: str) -> bool:
    """
    Returns True if we can decrypt without interactive prompt.
    """
    if not Path(test_secret_file).exists():
        return False
    cp = subprocess.run(
        ["gpg", "--quiet", "--batch", "--decrypt", test_secret_file],
        capture_output=True,
        text=True,
    )
    return cp.returncode == 0

def require_gpg_unlocked() -> None:
    """
    Abort early with a clear message if GPG is locked.
    """
    # Use eth secret file as the sentinel; if unlocked, others will be too.
    sentinel = WALLETS["tecbot_eth"]["secret"]
    if not gpg_is_unlocked(sentinel):
        msg = (
            "\n[ERROR] GPG key is locked. Please unlock before running the bot.\n"
            f"Run: gpg --decrypt {sentinel} > /dev/null\n"
        )
        raise SystemExit(msg)

def _read_passphrase(secret_path: str) -> str:
    """
    Decrypt a wallet passphrase from its .gpg file via gpg-agent.
    """
    if not Path(secret_path).exists():
        raise RuntimeError(f"Secret file not found: {secret_path}")
    cp = _run(["gpg", "--quiet", "--batch", "--decrypt", secret_path])
    return cp.stdout.strip()

def eth_to_one(eth_addr: str) -> str:
    """
    Convert 0x... ETH-style address to one... using hmy utility.
    Accepts already-one-formatted and returns as-is.
    """
    if eth_addr.startswith("one1"):
        return eth_addr
    # Basic sanity
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", eth_addr):
        raise ValueError(f"Invalid ETH address: {eth_addr}")
    cp = _run(["hmy", "utility", "bech32", eth_addr])
    return cp.stdout.strip()

def get_one_balance(one_addr: str) -> int:
    """
    Returns balance in attoONE (as int). You can format to ONE externally.
    """
    cp = _run(["hmy", "balances", one_addr, "--node", HMY_NODE])
    # hmy prints a small JSON-ish or table — support JSON if available
    out = cp.stdout.strip()
    # Try to parse JSON array if present
    try:
        data = json.loads(out)
        # harmony CLI may return [{'address': '...', 'amount': '123'}]
        if isinstance(data, list) and data and "amount" in data[0]:
            return int(data[0]["amount"])
    except json.JSONDecodeError:
        pass
    # Fallback: regex for a number in output (attoONE)
    m = re.search(r"(\d+)", out)
    if not m:
        raise RuntimeError(f"Unable to parse balance output: {out}")
    return int(m.group(1))

def send_one_native(wallet_key: str, to_address: str, amount_one: float) -> dict:
    """
    Send native ONE using the named wallet (tecbot_eth|tecbot_sdai|...).
    `to_address` may be one... or 0x... (we convert if needed).
    Returns parsed receipt dict (includes transaction-hash, status, etc.)
    """
    if wallet_key not in WALLETS:
        raise ValueError(f"Unknown wallet key: {wallet_key}")
    wallet = WALLETS[wallet_key]
    one_from = wallet["one"]
    one_to   = eth_to_one(to_address)

    secret = wallet["secret"]
    with _WALLET_LOCKS[wallet_key]:
        passphrase = _read_passphrase(secret)
        try:
            cmd = [
                "hmy", "transfer",
                "--from", one_from,
                "--to", one_to,
                "--amount", str(amount_one),
                "--from-shard", str(FROM_SHARD),
                "--to-shard", str(TO_SHARD),
                "--chain-id", str(HMY_CHAIN_ID),
                "--node", HMY_NODE,
                "--passphrase",
            ]
            cp = _run(cmd, input_text=passphrase + "\n")
        finally:
            # Minimal lifetime in memory
            passphrase = "****"

    # hmy returns JSON array with tx info
    try:
        data = json.loads(cp.stdout)
        return data[0] if isinstance(data, list) and data else data
    except json.JSONDecodeError:
        # Return raw for debugging if parsing fails
        return {"raw": cp.stdout}

# ---------- Preflight when executed standalone ----------
def preflight() -> None:
    # Confirm folders exist
    for p in (BOT_DB_DIR, BOT_LOG_DIR, SECRETS_DIR):
        if not Path(p).exists():
            raise SystemExit(f"Required path missing: {p}")
    # Confirm wallets & secrets exist
    for k, w in WALLETS.items():
        if not w["one"].startswith("one1"):
            raise SystemExit(f"{k} missing ONE address in .env")
        if not Path(w["secret"]).exists():
            raise SystemExit(f"{k} secret file missing: {w['secret']}")
    # GPG unlocked?
    require_gpg_unlocked()
    # Node reachable (simple header call)
    try:
        _run(["hmy", "--node", HMY_NODE, "blockchain", "latest-headers"], timeout=15)
    except Exception as e:
        raise SystemExit(f"Harmony RPC not reachable or slow: {e}")

if __name__ == "__main__":
    try:
        preflight()
        # Example: transfer 0.001 ONE from tecbot_eth to tecbot_sdai
        # (safe canned example; comment out in production)
        # receipt = send_one_native("tecbot_eth", WALLETS["tecbot_sdai"]["one"], 0.001)
        # print(json.dumps(receipt, indent=2))
        print("[wallet] Preflight OK. Ready.")
    except SystemExit as e:
        print(str(e))
        sys.exit(1)
    except Exception as e:
        print(f"[wallet] Fatal: {e}")
        sys.exit(1)
