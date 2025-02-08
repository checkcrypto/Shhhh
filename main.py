import os
import re
import random
import logging
import time
import signal
from io import BytesIO
import json
import aiohttp
import asyncio
import requests
# PostgreSQL সংক্রান্ত ইমপোর্ট এখন আর ব্যবহার করা হচ্ছে না
# import psycopg2 
# from psycopg2.pool import SimpleConnectionPool
import threading
import gc
from concurrent.futures import ThreadPoolExecutor
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode, InputMediaPhoto, InputFile
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, CallbackContext, MessageHandler, Filters
from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip44,
    Bip44Coins,
    Bip44Changes,
    Bip39WordsNum,
)
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables
load_dotenv()

TELEGRAM_BOT_TOKEN = "7822177522:AAHbW2s4VurYr_AISu8xqt7gztaXWmtjCLU"
ADMIN_ID = 6268276296

# Firebase configuration
FIREBASE_URL = "https://scarlett-9bc45-default-rtdb.firebaseio.com/"
API_KEY = "AIzaSyC31qn1YAJiPjAg7lVE1l2EwlRrNrcAzwg"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),  # Log file
        logging.StreamHandler(),        # Console output for Railway
    ],
)

# Thread pool for managing concurrent scans
scan_executor = ThreadPoolExecutor(max_workers=10)

# Assuming COOLDOWN_TIME is 5 seconds (can be adjusted)
COOLDOWN_TIME = 5

# Dictionary to store the last command time for each user
user_last_command_time = {}

# Since we are using Firebase, explicit database connection and table creation is not required.
def create_tables():
    # Firebase is NoSQL; no explicit table creation required.
    pass

# ------------------ Firebase Helper Functions ------------------ #
def firebase_set(path, data):
    """Set (or replace) data at the given Firebase path."""
    url = f"{FIREBASE_URL}{path}.json"
    response = requests.put(url, json=data)
    if response.status_code != 200:
        logging.error("Error setting data in Firebase: " + response.text)
    return response.json()

def firebase_update(path, data):
    """Update (patch) data at the given Firebase path."""
    url = f"{FIREBASE_URL}{path}.json"
    response = requests.patch(url, json=data)
    if response.status_code != 200:
        logging.error("Error updating data in Firebase: " + response.text)
    return response.json()

def firebase_get(path):
    """Retrieve data from the given Firebase path."""
    url = f"{FIREBASE_URL}{path}.json"
    response = requests.get(url)
    if response.status_code != 200:
        logging.error("Error getting data from Firebase: " + response.text)
        return None
    return response.json()

def firebase_delete(path):
    """Delete data at the given Firebase path."""
    url = f"{FIREBASE_URL}{path}.json"
    response = requests.delete(url)
    if response.status_code != 200:
        logging.error("Error deleting data from Firebase: " + response.text)
    return response.json()

# ------------------ Shutdown & Active Users ------------------ #
def shutdown_handler(signum, frame):
    save_active_users()
    logging.info("Bot is shutting down. Active users saved.")

signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

def save_active_users():
    with open("active_chat_ids.json", "w") as f:
        json.dump(list(active_chat_ids), f)
    logging.info("Active user list saved.")

def load_active_users():
    global active_chat_ids
    try:
        with open("active_chat_ids.json", "r") as f:
            active_chat_ids = set(json.load(f))
        logging.info(f"Loaded {len(active_chat_ids)} active users.")
    except FileNotFoundError:
        active_chat_ids = set()
        logging.info("No active users file found. Starting fresh.")

# Initialize logger
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Notification message
NOTIFICATION_MESSAGE = (
    "🔄 **Bot Update Notification** 🔄\n\n"
    "✨ The bot has been updated with new features and fixes!\n"
    "💡 Use /start to explore the latest updates and ensure you're ready to scan wallets.\n\n"
    "Thank you for using Wallet Scanner Bot! 🚀"
)

# Set to track active users (chat IDs) during this session
active_chat_ids = set()

# Function to track users
def track_user(update: Update, context: CallbackContext) -> None:
    """Track active user chat IDs."""
    chat_id = update.message.chat.id
    active_chat_ids.add(chat_id)
    logger.info(f"Tracking user: {chat_id}")

# Notify all users (synchronous version)
def notify_all_users(context: CallbackContext) -> None:
    """Broadcast the update notification to all active users."""
    app = context.bot  # Access the bot instance from context
    if not active_chat_ids:
        logger.info("No active users to notify.")
        return

    logger.info(f"Notifying {len(active_chat_ids)} active users about the update.")
    for chat_id in active_chat_ids:
        try:
            app.send_message(chat_id=chat_id, text=NOTIFICATION_MESSAGE)
            logger.info(f"Notified chat ID: {chat_id}")
        except Exception as e:
            logger.error(f"Failed to notify chat {chat_id}: {e}")

def clear_logs(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to clear the logs.")
        return

    log_file = "bot.log"

    try:
        with open(log_file, "w") as file:
            file.write("")
        update.message.reply_text("✅ All logs have been cleared.")
    except Exception as e:
        logging.error("Error clearing logs: %s", str(e))
        update.message.reply_text("❌ An error occurred while clearing the logs.")

# ------------------ Wallet & Blockchain Functions ------------------ #
def bip():
    return Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_12)

def bip44_wallet_from_seed(seed, coin_type):
    seed_bytes = Bip39SeedGenerator(seed).Generate()
    bip44_mst_ctx = Bip44.FromSeed(seed_bytes, coin_type)
    bip44_acc_ctx = (
        bip44_mst_ctx.Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(0)
    )
    address = bip44_acc_ctx.PublicKey().ToAddress()
    return address

def check_balance(address, blockchain='eth', retries=3):
    API_URLS = {
        'ETH': 'https://api.etherscan.io/api',
        'BNB': 'https://api.bscscan.com/api',
        'MATIC': 'https://polygon-mainnet.g.alchemy.com/v2/',
        'BTC': 'https://api.blockcypher.com/v1/btc/main/addrs',
        'SOL': 'https://solana-mainnet.g.alchemy.com/v2/',
        'TRX': 'https://api.trongrid.io/v1/accounts',
    }

    API_KEYS = {
        'ETH': ['FQP5IPEJ8AX6CPK36KA4SA83JM8Q8GE536', 'QJ1KK3WKKXPJY3YS1J7D92X28VHW3IZ3WS', 'XXCIS9AM5MTK3SYX6KUQJR78WS1RVV2JJ5', 'CBPTJ93NUMZWX9GZCDFTMGRUS9IC7EH3BQ', 'WXWU1HKNC5VTA3R2C2GSXSFA9X28G1I7M2', 'GURBM457ARBWUZB3S2H4GUJ1VJW81QYD4H', '6KGNW5GJGW75XBZAG4ZJ1MFTK485SCSGDX'],
        'BNB': ['65M94C8PQJ7D2XV2I1HRAGPAUBS4M6SEBM', 'WBRXW5TIW8695GJ9MYI4GMQ697E9IXTME9', 'T5TJ95BRV5C39EHGEGUE2C66CCWVT2AEWH', 'DR65PS97WNCUC8TNTVNBWM8II8KXSMYYNS'],
        'MATIC': ['zoMCKvF33iDsnOOypDHFM7Kz7DcXYGf6'],
        'BTC': ['caf89b72dce148db9ec9ab91b7752535'],
        'SOL': ['zoMCKvF33iDsnOOypDHFM7Kz7DcXYGf6'],
        'TRX': ['36fccbf8-4fb6-4359-9da1-9eb4731112dd', '9622305c-560a-4cbd-8f64-37b4cf17b24b', '938868d6-021f-4450-91a3-a2d282564e60', '59518681-695e-4a73-aacf-254bd39ebd84'],
    }

    blockchain = blockchain.upper()
    url = API_URLS.get(blockchain)
    api_keys = API_KEYS.get(blockchain)

    if not url or not api_keys:
        logging.error(f"Unsupported blockchain or missing API keys: {blockchain}")
        return 0

    for attempt in range(retries):
        for api_key_to_use in api_keys:
            try:
                logging.info(f"Checking balance for {blockchain} on attempt {attempt + 1} using API key: {api_key_to_use}")

                if blockchain == 'ETH':
                    full_url = f"{url}?module=account&action=balance&address={address}&tag=latest&apikey={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['result']) / 1e18
                    return balance

                elif blockchain == 'BNB':
                    full_url = f"{url}?module=account&action=balance&address={address}&tag=latest&apikey={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['result']) / 1e18
                    return balance

                elif blockchain == 'MATIC':
                    full_url = f"{url}{api_key_to_use}"
                    payload = {
                        "jsonrpc": "2.0",
                        "method": "eth_getBalance",
                        "params": [address, "latest"],
                        "id": 1
                    }
                    response = requests.post(full_url, json=payload, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['result'], 16) / 1e18
                    return balance

                elif blockchain == 'BTC':
                    full_url = f"{url}/{address}/balance?token={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = int(data['balance']) / 1e8
                    return balance

                elif blockchain == 'SOL':
                    full_url = url + api_key_to_use
                    payload = {
                        "jsonrpc": "2.0",
                        "method": "getBalance",
                        "params": [address],
                        "id": 1
                    }
                    response = requests.post(full_url, json=payload, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = data.get('result', {}).get('value', 0) / 1e9
                    return balance

                elif blockchain == 'TRX':
                    full_url = f"{url}/{address}?apikey={api_key_to_use}"
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    balance = data['data'][0]['balance'] / 1e6
                    return balance

                else:
                    logging.error(f"Unsupported blockchain: {blockchain}")
                    return 0

            except requests.exceptions.RequestException as e:
                logging.error(f"HTTP error for {blockchain} (address: {address}): {e}")
                time.sleep(1)
            except ValueError as e:
                logging.error(f"Error parsing response for {blockchain} (address: {address}): {e}")
                break

    logging.error(f"Failed to retrieve balance for {blockchain} (address: {address}) after {retries} attempts")
    return 0

def bip44_btc_seed_to_address(seed):
    seed_bytes = Bip39SeedGenerator(seed).Generate()
    bip44_mst_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.BITCOIN)
    bip44_acc_ctx = bip44_mst_ctx.Purpose().Coin().Account(0)
    bip44_chg_ctx = bip44_acc_ctx.Change(Bip44Changes.CHAIN_EXT)
    bip44_addr_ctx = bip44_chg_ctx.AddressIndex(0)
    btc_address = bip44_addr_ctx.PublicKey().ToAddress()
    return btc_address

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ------------------ Telegram Command Handlers ------------------ #
def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    username = update.message.chat.username or "Unknown"

    if user_id not in active_chat_ids:
        active_chat_ids.add(user_id)
        logging.info(f"User added to active_chat_ids: {user_id} (@{username})")

    current_time = time.time()
    last_command_time = user_last_command_time.get(user_id, 0)

    if current_time - last_command_time < COOLDOWN_TIME:
        remaining_time = int(COOLDOWN_TIME - (current_time - last_command_time))
        update.message.reply_text(
            f"⏳ Please wait **{remaining_time} seconds** before using this command again. Thank you for your patience! 🙏"
        )
        return

    user_last_command_time[user_id] = current_time

    # Retrieve user key from Firebase
    user_data = firebase_get(f"user_keys/{user_id}")
    if user_data:
        key = user_data.get("key")
        update.message.reply_text(
            f"🎉 **Welcome back, @{username}!** 🎉\n\n"
            f"🔑 **Key Redeemed:** `{key}`\n"
            "✨ You're all set to start scanning wallets! 🚀\n\n"
            "You can also use the Account Checker feature to process account files. Click the button below to access it! 😎"
        )
    else:
        update.message.reply_text(
            "🌟 **Welcome to Wallet Scanner Bot!** 🌟\n\n"
            "👋 Hi there! To begin, you’ll need to redeem a key.\n"
            "🔑 Use `/redeem <key>` to unlock the scanning features.\n\n"
            "Once redeemed, you'll gain access to the Account Checker and other features! 💰"
        )

    update.message.reply_photo(
        photo="https://i.ibb.co.com/FbjG1pwH/IMG-20250208-152642-799.jpg",
        caption="✨ **Welcome Aboard!** We’re thrilled to have you here. Let’s get started! 🚀"
    )

    keyboard = [
        [InlineKeyboardButton("💵 Key Prices", callback_data='keyprice')],
        [InlineKeyboardButton("ℹ️ About the Bot", callback_data='about')],
        [InlineKeyboardButton("🪙 Blockchain Options", callback_data='blockchain_options')],
        [InlineKeyboardButton("🚀 Start Scan (Booster Mode)", callback_data='start_scan_booster')],
        [InlineKeyboardButton("⛔ Stop Scan", callback_data='stop_scan')],
        [InlineKeyboardButton("🔑 Show Keys", callback_data='show_keys')],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "👇 **What would you like to do next?**\n\n"
        "Choose an option below to get started with Wallet Scanner Bot! 🔥",
        reply_markup=reply_markup
    )

def blockchain_options(update: Update, context: CallbackContext) -> None:
    current_time = time.time()
    user_id = None

    if update.message:
        user_id = update.message.chat.id
        last_command_time = user_last_command_time.get(user_id, 0)
        if current_time - last_command_time < COOLDOWN_TIME:
            remaining_time = int(COOLDOWN_TIME - (current_time - last_command_time))
            update.message.reply_text(f"⏳ Please wait {remaining_time} seconds before using this option again.")
            return
        user_last_command_time[user_id] = current_time

        blockchain_keyboard = [
            [InlineKeyboardButton("🪙 Ethereum (ETH)", callback_data='start_scan_eth')],
            [InlineKeyboardButton("🪙 Binance Smart Chain (BNB)", callback_data='start_scan_bnb')],
            [InlineKeyboardButton("🪙 Polygon (MATIC)", callback_data='start_scan_matic')],
            [InlineKeyboardButton("🪙 Solana (SOL)", callback_data='start_scan_sol')],
            [InlineKeyboardButton("🪙 Bitcoin (BTC)", callback_data='start_scan_btc')],
            [InlineKeyboardButton("🪙 Tron (TRX)", callback_data='start_scan_trx')],
            [InlineKeyboardButton("⬅️ Back", callback_data='back_to_main')],
        ]
        reply_markup = InlineKeyboardMarkup(blockchain_keyboard)

        update.message.reply_text(
            text="🌐 **Select a Blockchain** 🌐\n\nChoose a blockchain to start scanning:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif update.callback_query:
        query = update.callback_query
        user_id = query.message.chat.id
        last_command_time = user_last_command_time.get(user_id, 0)
        if current_time - last_command_time < COOLDOWN_TIME:
            remaining_time = int(COOLDOWN_TIME - (current_time - last_command_time))
            query.answer(
                f"⏳ Please wait {remaining_time} seconds before using this option again.",
                show_alert=True
            )
            return
        user_last_command_time[user_id] = current_time

        blockchain_keyboard = [
            [InlineKeyboardButton("🪙 Ethereum (ETH)", callback_data='start_scan_eth')],
            [InlineKeyboardButton("🪙 Binance Smart Chain (BNB)", callback_data='start_scan_bnb')],
            [InlineKeyboardButton("🪙 Polygon (MATIC)", callback_data='start_scan_matic')],
            [InlineKeyboardButton("🪙 Solana (SOL)", callback_data='start_scan_sol')],
            [InlineKeyboardButton("🪙 Bitcoin (BTC)", callback_data='start_scan_btc')],
            [InlineKeyboardButton("🪙 Tron (TRX)", callback_data='start_scan_trx')],
            [InlineKeyboardButton("⬅️ Back", callback_data='back_to_main')],
        ]
        reply_markup = InlineKeyboardMarkup(blockchain_keyboard)

        query.answer()
        query.edit_message_text(
            text="🌐 **Select a Blockchain** 🌐\n\nChoose a blockchain to start scanning:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

def back_to_main(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if query:
        try:
            query.answer()
        except Exception as e:
            logging.error(f"Error answering callback query: {e}")

    try:
        query.edit_message_text(
            text="👇 **What would you like to do next?**\n\n"
                 "Choose an option below to get started with Wallet Scanner Bot! 🔥",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💵 Key Prices", callback_data='keyprice')],
                [InlineKeyboardButton("ℹ️ About the Bot", callback_data='about')],
                [InlineKeyboardButton("🪙 Blockchain Options", callback_data='blockchain_options')],
                [InlineKeyboardButton("🚀 Start Scan (Booster Mode)", callback_data='start_scan_booster')],
                [InlineKeyboardButton("⛔ Stop Scan", callback_data='stop_scan')],
                [InlineKeyboardButton("🔑 Show Keys", callback_data='show_keys')],
            ])
        )
    except Exception as e:
        logging.error(f"Error editing callback query message: {e}")

def show_admin(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to view the admin list.")
        return

    admins = firebase_get("admins")
    if admins:
        admin_list = "\n".join([f"💻 @{admin['username']} [{admin['user_id']}]" for admin in admins.values()])
        update.message.reply_text(f"👥 **Admin list** 👥\n\n{admin_list}", parse_mode="Markdown")
    else:
        update.message.reply_text("❌ No admins found.")

def is_admin(user_id):
    if user_id == ADMIN_ID:
        return True
    admins = firebase_get("admins")
    if admins:
        return str(user_id) in admins or user_id in [admin.get("user_id") for admin in admins.values()]
    return False

def key_price_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    query.answer()
    message = (
        "💵 **Key Prices** 💵\n\n"
        "1 Day key : 15$ | 0.07 SOL\n"
        "🔧 Booster Mode: ❌ Disabled\n\n"
        "1 Week key : 70$ | 0.34 SOL\n"
        "🔧 Booster Mode: ❌ Disabled\n\n"
        "1 Month key : 300$ | 1.46 SOL\n"
        "🔧 Booster Mode: ✅ Enabled\n\n"
        "If you want to buy a key, just send a message to this bot: @CoinScannerBuyBot"
    )
    query.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def start_scan_by_id(user_id, blockchain, message, booster):
    message.reply_text(
        f"✨ Awesome! Starting a scan on {blockchain.upper()}... 🌍\n"
        f"🌱 Seed: .......\n🏦 Address: .......\n🔄 Scanned wallets: 0"
    )
    user_scan_status[user_id] = {'is_scanning': False}

    if booster and blockchain == 'all':
        blockchains = ['eth', 'bnb', 'matic', 'btc', 'sol', 'trx']
        for chain in blockchains:
            threading.Thread(target=scan_wallets, args=(user_id, chain, message, True)).start()
    else:
        threading.Thread(target=scan_wallets, args=(user_id, blockchain, message, False)).start()

    message.reply_text(
        f"🚀 Your {blockchain.upper()} scan has started! Sit tight while we search for treasure 🤑!"
    )

def stop_all_scans(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to stop all scans.")
        return
    
    for user in user_scan_status:
        user_scan_status[user]['is_scanning'] = False
    
    update.message.reply_text("🛑 All scans have been stopped by the admin. bot updated or fixed bug")

def stop_scan(update: Update, context: CallbackContext) -> None:
    user_id = update.callback_query.message.chat.id
    if user_id not in user_scan_status or not user_scan_status[user_id].get('is_scanning', False):
        update.callback_query.message.reply_text("⛔ No active scan to stop.")
        return

    user_scan_status[user_id]['is_scanning'] = False
    update.callback_query.message.reply_text("🛑 Scanning stopped.")

# ------------------ Scan Functions with Additional Checks ------------------ #
def scan_wallets(user_id, blockchain, message, booster=False):
    try:
        existing_log = firebase_get(f"scan_logs/{user_id}/{blockchain}")
        previous_scanned_count = existing_log.get("wallets_scanned", 0) if existing_log else 0

        user_scan_status[user_id] = {
            'is_scanning': True,
            'wallets_scanned': previous_scanned_count
        }

        # Ensure user key exists and contains the "key" field
        user_record = firebase_get(f"user_keys/{user_id}")
        if not user_record or "key" not in user_record:
            message.reply_text("❌ Your key data was not found. Please redeem your key again.")
            return

        booster_data = firebase_get(f"masterkeys/{user_record['key']}")
        booster_allowed = booster_data.get("can_use_booster") if booster_data else False

        if booster and not booster_allowed:
            booster = False
            message.reply_text("⚠️ You don't have permission to use booster mode. Continuing scan without booster.")

        blockchain_map = {
            'eth': Bip44Coins.ETHEREUM,
            'bnb': Bip44Coins.BINANCE_SMART_CHAIN,
            'matic': Bip44Coins.POLYGON,
            'btc': Bip44Coins.BITCOIN,
            'sol': Bip44Coins.SOLANA,
            'trx': Bip44Coins.TRON
        }
        coin_type = blockchain_map.get(blockchain)
        if not coin_type:
            message.reply_text("❌ Unsupported blockchain selected.")
            return

        watchdog_thread = threading.Thread(target=watchdog, args=(user_id, blockchain, message, booster))
        watchdog_thread.daemon = True
        watchdog_thread.start()

        while user_scan_status[user_id]['is_scanning']:
            seed = bip()
            if blockchain == 'btc':
                address = bip44_btc_seed_to_address(seed)
            else:
                address = bip44_wallet_from_seed(seed, coin_type)

            balance = check_balance(address, blockchain)
            user_scan_status[user_id]['wallets_scanned'] += 1

            firebase_set(f"scan_logs/{user_id}/{blockchain}", {"wallets_scanned": user_scan_status[user_id]['wallets_scanned']})

            if user_scan_status[user_id]['wallets_scanned'] % 50 == 0:
                try:
                    message.edit_text(
                        f"```\n"
                        f"✨ Scanning {blockchain.upper()}...\n"
                        f"🌱 Seed: {seed}\n"
                        f"🏦 Address: {address}\n"
                        f"🔄 Wallets scanned: {user_scan_status[user_id]['wallets_scanned']}\n"
                        f"⏳ Working hard to find balances! 🌟\n"
                        f"```",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logging.error(f"Error editing message: {e}")

            if balance > 0:
                message.reply_text(
                    f"🎉 Found a wallet with balance!\n"
                    f"🌱 Seed: {seed}\n"
                    f"🏦 Address: {address}\n"
                    f"💰 Balance: {balance} {blockchain.upper()}"
                )
                user_scan_status[user_id]['is_scanning'] = False
                break

            time.sleep(0.5 if booster else 0.9)

    except Exception as e:
        logging.error(f"Error in scan_wallets: {e}")
        message.reply_text("❌ An error occurred during the scan.")
    finally:
        user_scan_status[user_id]['is_scanning'] = False

def watchdog(user_id, blockchain, context, booster=False):
    while user_scan_status[user_id]['is_scanning']:
        prev_scanned = user_scan_status[user_id]['wallets_scanned']
        time.sleep(120)
        if user_scan_status[user_id]['wallets_scanned'] == prev_scanned:
            user_scan_status[user_id]['is_scanning'] = False
            context.bot.send_message(chat_id=user_id, text=f"⚠️ The scan on {blockchain.upper()} seems to have paused. Restarting now...")
            start_scan_by_id(user_id, blockchain, context.bot, booster)

# ------------------ Admin Related Functions ------------------ #
def add_admin(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to add admins.")
        return

    args = context.args
    if len(args) < 2:
        update.message.reply_text("❌ Usage: /add_admin <id> <username>")
        return

    new_admin_id = args[0]
    username = args[1]

    existing = firebase_get(f"admins/{new_admin_id}")
    if existing:
        update.message.reply_text(f"ℹ️ Admin [{new_admin_id}] already exists.")
    else:
        firebase_set(f"admins/{new_admin_id}", {"user_id": new_admin_id, "username": username})
        update.message.reply_text(f"✅ Admin added: {username} [{new_admin_id}]")

# --- IMPORTANT: Added remove_admin function ---
def remove_admin(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to remove admins.")
        return

    args = context.args
    if len(args) < 1:
        update.message.reply_text("❌ Usage: /remove_admin <id>")
        return

    admin_id = args[0]
    firebase_delete(f"admins/{admin_id}")
    update.message.reply_text(f"✅ Admin removed: [{admin_id}]")
# --- End of remove_admin function ---

def create_key(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    
    if not is_admin(user_id):
        update.message.reply_text("❌ You don't have permission to create keys.")
        return

    args = context.args
    if len(args) < 3:
        update.message.reply_text("❌ Usage: /create_key <key> <expiration (DD-MM-YYYY)> <booster (true/false)>")
        return

    key = args[0]
    expiration_str = args[1]
    booster = args[2].lower()

    try:
        expiration = datetime.strptime(expiration_str, "%d-%m-%Y")
    except ValueError:
        update.message.reply_text("❌ Invalid expiration date format. Please use DD-MM-YYYY.")
        return

    if booster not in ['true', 'false']:
        update.message.reply_text("❌ Booster must be either 'true' or 'false'.")
        return

    booster_mode = booster == 'true'

    firebase_set(f"masterkeys/{key}", {"key": key, "expiration": expiration_str, "can_use_booster": booster_mode})
    update.message.reply_text(f"✅ Key created: {key}\n📅 Expiration: {expiration_str}\n🚀 Booster mode: {booster_mode}")

def button_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    if query.data == 'about':
        about_callback(update, context)
    elif query.data == 'keyprice':  # Handle the new button
        key_price_callback(update, context)
    elif query.data in ['start_scan_eth', 'start_scan_bnb', 'start_scan_matic', 'start_scan_trx', 'start_scan_btc', 'start_scan_pol', 'start_scan_booster']:
        start_scan(update, context)
    elif query.data == 'stop_scan':
        stop_scan(update, context)
    elif query.data == 'show_keys':
        show_keys(update, context)

def about_callback(update: Update, context: CallbackContext) -> None:
    update.callback_query.message.reply_text(
        f"```\n"
        f"✨ Welcome to the Wallet Scanner Bot! ✨\n\n"
        f"🔍 This bot is your ultimate tool for finding wallets with balances across the following networks:\n"
        f"  - 🌐 Ethereum (ETH)\n"
        f"  - 🔶 Binance Smart Chain (BSC)\n"
        f"  - 🟣 Polygon (MATIC)\n"
        f"  - 🪙 Bitcoin (BTC)\n"
        f"  - 🌞 Solana (SOL)\n"
        f"  - 🚀 Tron (TRX)\n\n"
        f"💡 Features:\n"
        f"  - 🔑 Redeem keys to unlock powerful scanning capabilities.\n"
        f"  - 🚀 Use Booster Mode for faster, simultaneous scanning across all supported networks.\n\n"
        f"📖 How to Get Started:\n"
        f"  1️⃣ Use /redeem <key> to activate your scanning access.\n"
        f"  2️⃣ Select the blockchain network you want to scan.\n"
        f"  3️⃣ Sit back and let the bot do the work for you!\n\n"
        f"```"
        "💬 Need help or have questions? Send massage to @emran080 to learn more about the bot's features.\n\n"
        "Happy scanning! 🤑"
    )
    
def redeem(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id
    username = update.message.chat.username or "Unknown"
    args = context.args

    if len(args) < 1:
        update.message.reply_text("🔑 Please provide a key to redeem: /redeem <key>")
        return

    new_key = args[0]

    key_data = firebase_get(f"masterkeys/{new_key}")
    if not key_data:
        update.message.reply_text("❌ Invalid key. Please try again.")
        return

    # Check if the key is already redeemed by another user
    all_user_keys = firebase_get("user_keys")
    if all_user_keys:
        for uid, record in all_user_keys.items():
            if record.get("key") == new_key and int(uid) != user_id:
                update.message.reply_text("❌ This key is already redeemed by another user.")
                return

    firebase_set(f"user_keys/{user_id}", {"user_id": user_id, "key": new_key, "username": username})

    booster_enabled = key_data.get("can_use_booster", False)
    message_text = (
        f"✅ Key redeemed successfully!\n"
        f"🔑 Key: {new_key}\n"
        f"🚀 Booster mode: {'Enabled' if booster_enabled else 'Disabled'}\n"
        f"🎉 Welcome, @{username}!"
    )
    update.message.reply_text(message_text)

def optimize_memory():
    while True:
        gc.collect()
        time.sleep(600)

def remove_key(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    if not is_admin(user_id):
        update.message.reply_text("❌ You don't have permission to remove keys.")
        return

    args = context.args
    if len(args) < 1:
        update.message.reply_text("❌ Usage: /remove_key <key>")
        return

    key = args[0]

    firebase_delete(f"masterkeys/{key}")
    all_user_keys = firebase_get("user_keys")
    key_removed = False
    if all_user_keys:
        for uid, record in all_user_keys.items():
            if record.get("key") == key:
                firebase_delete(f"user_keys/{uid}")
                key_removed = True
    if key_removed:
        update.message.reply_text(f"✅ Key removed successfully: {key}")
    else:
        update.message.reply_text("❌ Key not found in either masterkeys or user_keys node.")

def show_keys(update: Update, context: CallbackContext) -> None:
    user_id = update.callback_query.message.chat.id

    if not is_admin(user_id):
        update.callback_query.message.reply_text("❌ You don't have permission to view the keys.")
        return

    user_keys_data = firebase_get("user_keys")
    if user_keys_data:
        keys_list = []
        for uid, record in user_keys_data.items():
            masterkey = firebase_get(f"masterkeys/{record.get('key')}")
            expiration = masterkey.get("expiration") if masterkey else "N/A"
            booster_mode = masterkey.get("can_use_booster") if masterkey else False
            keys_list.append(
                f"👤 User: @{record.get('username', 'Unknown')} ({uid})\n"
                f"🔑 Key: {record.get('key')}\n"
                f"📅 Expiration: {expiration}\n"
                f"🚀 Booster Mode: {'Enabled' if booster_mode else 'Disabled'}"
            )
        update.callback_query.message.reply_text(f"🗝️ Current Keys:\n\n" + "\n\n".join(keys_list))
    else:
        update.callback_query.message.reply_text("❌ No keys have been redeemed.")

def admin_panel(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id if update.message else update.callback_query.message.chat.id

    if user_id != ADMIN_ID:
        if update.message:
            update.message.reply_text("❌ You don't have permission to access the admin panel.")
        else:
            update.callback_query.answer("❌ You don't have permission.", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton("➕ Create Key", callback_data='admin_create_key')],
        [InlineKeyboardButton("➖ Remove Key", callback_data='admin_remove_key')],
        [InlineKeyboardButton("🔑 Show Keys", callback_data='admin_show_keys')],
        [InlineKeyboardButton("🛑 Stop All Scans", callback_data='admin_stop_all_scans')],
        [InlineKeyboardButton("🌱 Add Seed", callback_data='admin_add_seed')],
        [InlineKeyboardButton("📜 Show Seeds", callback_data='admin_show_seed')],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='back_to_main')]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = (
        "🔐 **Admin Panel** 🔐\n\n"
        "Welcome, Admin! Choose an action from the options below:\n\n"
        "🗂️ Manage keys and seeds efficiently.\n"
        "🚦 Control scanning operations.\n"
        "🔧 Customize app functionalities.\n\n"
        "💡 *Note*: Actions are for administrators only."
    )

    if update.message:
        update.message.reply_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    elif update.callback_query:
        update.callback_query.message.edit_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

# Dictionary to track ongoing checks per user
ongoing_checks = {}

def add_seed(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to add seeds.")
        return

    args = context.args
    if len(args) < 3:
        update.message.reply_text("❌ Usage: /add_seed <12_words> <balance> <chance rate (1-100%)>")
        return

    seed = args[0]
    balance = float(args[1])
    chance_rate = float(args[2])

    if not (1 <= chance_rate <= 100):
        update.message.reply_text("❌ Chance rate must be between 1% and 100%.")
        return

    data = {
        "seed": seed,
        "balance": balance,
        "chance_rate": chance_rate,
        "added_by": user_id,
        "created_at": datetime.now().isoformat()
    }
    url = f"{FIREBASE_URL}seeds.json"
    response = requests.post(url, json=data)
    if response.status_code == 200:
        update.message.reply_text(f"✅ Seed added successfully!\nSeed: {seed}\nBalance: {balance}\nChance Rate: {chance_rate}%")
    else:
        update.message.reply_text("❌ Failed to add seed.")
        logging.error(f"Error adding seed: {response.text}")

def show_seed(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to view seeds.")
        return

    seeds = firebase_get("seeds")
    if seeds:
        seed_list = []
        for seed_id, record in seeds.items():
            seed_list.append(
                f"📌 **ID**: {seed_id}\n"
                f"🌱 **Seed**: `{record.get('seed')}`\n"
                f"💰 **Balance**: {record.get('balance')}\n"
                f"⚡ **Chance Rate**: {record.get('chance_rate')}%\n"
            )
        seed_chunks = [ "\n".join(seed_list[i:i + 10]) for i in range(0, len(seed_list), 10)]
        for chunk in seed_chunks:
            update.message.reply_text(f"🔑 **Seeds List**:\n\n{chunk}", parse_mode=ParseMode.MARKDOWN)
    else:
        update.message.reply_text("❌ No seeds found in the database.")

def send_seed(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    if user_id != ADMIN_ID:
        update.message.reply_text("❌ You don't have permission to send seeds.")
        return

    args = context.args
    if len(args) < 5:
        update.message.reply_text("❌ Usage: /send_seed <seed_id> <user_id> <address> <balance> <blockchain>")
        return

    try:
        seed_id = args[0]
        target_user_id = args[1]
        address = args[2]
        balance = float(args[3])
        blockchain = args[4].lower()

        valid_blockchains = ['eth', 'bnb', 'matic', 'avax', 'btc', 'sol', 'pol']
        if blockchain not in valid_blockchains:
            update.message.reply_text(f"❌ Unsupported blockchain: {blockchain.upper()}. Supported: {', '.join(valid_blockchains).upper()}")
            return

        seed_record = firebase_get(f"seeds/{seed_id}")
        if not seed_record:
            update.message.reply_text("❌ Seed not found. Please check the seed ID.")
            return

        firebase_update(f"seeds/{seed_id}", {"address": address, "balance": balance, "blockchain": blockchain})

        message = (
            f"🎉 **Found a wallet with balance!**\n\n"
            f"🌱 **Seed:** `{seed_record.get('seed')}`\n"
            f"🏦 **Address:** `{address}`\n"
            f"💰 **Balance:** {balance} {blockchain.upper()}\n\n"
            f"🔗 *Use this wallet responsibly!*"
        )

        context.bot.send_message(target_user_id, message, parse_mode=ParseMode.MARKDOWN)
        update.message.reply_text(f"✅ Seed {seed_id} sent successfully to user {target_user_id}.")

    except ValueError as e:
        update.message.reply_text("❌ Invalid input. Please check the arguments and try again.")
        logging.error(f"Input validation error: {e}")
    except Exception as e:
        update.message.reply_text("❌ Failed to send the seed. Please check the logs for details.")
        logging.error(f"Error sending seed: {e}", exc_info=True)

def handle_admin_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user_id = query.message.chat.id

    if user_id != ADMIN_ID:
        query.answer("❌ Unauthorized action.", show_alert=True)
        return

    query.answer()

    if query.data == 'admin_create_key':
        query.edit_message_text("➕ Use /create_key <key> <expiration (DD-MM-YYYY)> <booster (true/false)> to create a key.")
    elif query.data == 'admin_remove_key':
        query.edit_message_text("➖ Use /remove_key <key> to remove a key.")
    elif query.data == 'admin_show_keys':
        show_keys(update, context)
    elif query.data == 'admin_stop_all_scans':
        stop_all_scans(update, context)
    elif query.data == 'admin_add_seed':
        query.edit_message_text("➕ Use /add_seed <12_words> <balance> <chance rate (1-100%)> to add a seed.")
    elif query.data == 'admin_show_seed':
        show_seed(update, context)

def pod_command(update: Update, context: CallbackContext) -> None:
    user_id = update.message.chat.id

    if not is_admin(user_id):
        update.message.reply_text("❌ You don't have permission to use this command.")
        return

    context.user_data.pop('waiting_for_broadcast', None)
    update.message.reply_text("📝 Please send the message or upload a photo with a caption for broadcasting.")
    context.user_data['waiting_for_broadcast'] = True

def handle_broadcast_input(update: Update, context: CallbackContext) -> None:
    if not context.user_data.get('waiting_for_broadcast', False):
        return

    if update.message.text:
        message = update.message.text
        send_broadcast(message=message, photo=None, context=context)
    elif update.message.photo:
        photo = update.message.photo[-1].file_id
        caption = update.message.caption or ""
        send_broadcast(message=caption, photo=photo, context=context)

    context.user_data['waiting_for_broadcast'] = False
    update.message.reply_text("✅ Broadcast sent successfully!")

def send_broadcast(message: str, photo: str, context: CallbackContext) -> None:
    bot = context.bot
    failed_count = 0

    for chat_id in active_chat_ids:
        try:
            if photo:
                bot.send_photo(chat_id=chat_id, photo=photo, caption=message)
            else:
                bot.send_message(chat_id=chat_id, text=message)
        except Exception as e:
            logging.error(f"Failed to send broadcast to {chat_id}: {e}")
            failed_count += 1

    logging.info(f"Broadcast complete. Failed to notify {failed_count} users.")

def start_scan(update: Update, context: CallbackContext) -> None:
    try:
        query = update.callback_query
        query.answer()
        user_id = query.message.chat.id

        user_data = firebase_get(f"user_keys/{user_id}")
        if not user_data:
            query.message.reply_text("❌ Oops! You need a valid key to start scanning. Please redeem one first!")
            return

        blockchain_map = {
            'start_scan_eth': 'eth',
            'start_scan_bnb': 'bnb',
            'start_scan_matic': 'matic',
            'start_scan_btc': 'btc',
            'start_scan_sol': 'sol',
            'start_scan_trx': 'trx',
            'start_scan_booster': 'all',
        }
        blockchain = blockchain_map.get(query.data)
        if not blockchain:
            logging.error(f"Invalid blockchain selection: {query.data}")
            query.message.reply_text("❌ Invalid blockchain selection. Please try again.")
            return

        if user_scan_status.get(user_id, {}).get('is_scanning', False):
            query.message.reply_text("🔍 A scan is already running. Please stop the current scan first.")
            return

        user_scan_status[user_id] = {'is_scanning': True}

        message = query.message.reply_text(
             f"✨ Awesome! Starting a scan on {blockchain.upper()}... 🌍\n"
            f"🌱 Seed: .......\n🏦 Address: .......\n🔄 Scanned wallets: 0"
        )

        if blockchain == 'all':  
            chains = ['eth', 'bnb', 'matic', 'btc', 'sol', 'trx']
            for chain in chains:
                try:
                    scan_executor.submit(scan_wallets, user_id, chain, message, True)
                except Exception as e:
                    logging.error(f"Failed to start scan for {chain}: {e}")
                    query.message.reply_text(f"❌ Failed to start scan for {chain.upper()}.")
        else:
            try:
                scan_executor.submit(scan_wallets, user_id, blockchain, message, False)
            except Exception as e:
                logging.error(f"Failed to start scan for {blockchain}: {e}")
                query.message.reply_text(f"❌ Failed to start scan for {blockchain.upper()}.")

        query.message.reply_text(f"🚀 Your {blockchain.upper()} scan has started! Sit tight while we search for treasure 🤑!")
    except Exception as e:
        logging.error(f"Error in start_scan: {e}")
        query.message.reply_text("❌ An error occurred while starting the scan. Please try again.")

def watchdog(user_id, blockchain, context, booster=False):
    while user_scan_status[user_id]['is_scanning']:
        prev_scanned = user_scan_status[user_id]['wallets_scanned']
        time.sleep(120)
        if user_scan_status[user_id]['wallets_scanned'] == prev_scanned:
            user_scan_status[user_id]['is_scanning'] = False
            context.bot.send_message(chat_id=user_id, text=f"⚠️ The scan on {blockchain.upper()} seems to have paused. Restarting now...")
            start_scan_by_id(user_id, blockchain, context.bot, booster)

# Global variable to track scan status for users
user_scan_status = {}

def main() -> None:
    memory_thread = threading.Thread(target=optimize_memory)
    memory_thread.daemon = True
    memory_thread.start()

    updater = Updater(TELEGRAM_BOT_TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("redeem", redeem))
    dispatcher.add_handler(CommandHandler("create_key", create_key))
    dispatcher.add_handler(CommandHandler("remove_key", remove_key))
    dispatcher.add_handler(CommandHandler("remove_admin", remove_admin))
    dispatcher.add_handler(CommandHandler("clear_logs", clear_logs))
    dispatcher.add_handler(CommandHandler("admin_panel", admin_panel))
    dispatcher.add_handler(CommandHandler("send_seed", send_seed))
    dispatcher.add_handler(CallbackQueryHandler(handle_admin_callback, pattern='admin_.*'))
    dispatcher.add_handler(CommandHandler("pod", pod_command))
    dispatcher.add_handler(MessageHandler(Filters.text | Filters.photo, handle_broadcast_input))
    dispatcher.add_handler(CallbackQueryHandler(back_to_main, pattern='back_to_main'))
    dispatcher.add_handler(CallbackQueryHandler(about_callback, pattern='about'))
    dispatcher.add_handler(CommandHandler("stop_allscans", stop_all_scans))
    dispatcher.add_handler(CommandHandler("add_admin", add_admin))
    dispatcher.add_handler(CommandHandler("remove_admin", remove_admin))
    dispatcher.add_handler(CommandHandler("show_admin", show_admin))
    dispatcher.add_handler(CallbackQueryHandler(blockchain_options, pattern="^blockchain_options$"))
    dispatcher.add_handler(CallbackQueryHandler(button_callback))

    updater.start_polling()
    updater.job_queue.run_once(notify_all_users, 0)

    logger = logging.getLogger(__name__)
    updater.idle()

if __name__ == '__main__':
    main()