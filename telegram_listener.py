import os
import logging
from telegram.ext import Updater, CommandHandler
from telegram import ParseMode
from dotenv import load_dotenv

from app.wallet import WALLETS, get_native_balance_wei, get_erc20_balance_wei
from app.trade_executor import get_token_address
from app.price_feed import fetch_lp_quotes

load_dotenv("/home/tecviva/.env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_listener")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

def fmt_amount(val, decimals=18):
    return f"{val / (10**decimals):,.4f}"

# --- Commands ---

def ping(update, context):
    update.message.reply_text(f"pong ✅\nServer IP active")

def balances(update, context):
    lines = []
    for wname, addr in WALLETS.items():
        lines.append(f"*{wname}*")
        one_bal = get_native_balance_wei(addr)
        lines.append(f"  ONE {fmt_amount(one_bal)}")
    update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

def prices(update, context):
    quotes = fetch_lp_quotes()
    lines = ["Prices ✅"]
    for token, val in quotes.items():
        if isinstance(val, dict):
            lines.append(f"{token:5s} | Harmony {val['harmony']} | CB {val['coinbase']}")
        else:
            lines.append(f"{token:5s} | {val}")
    update.message.reply_text("\n".join(lines))

# --- Main ---

def main():
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