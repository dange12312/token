import os
import asyncio
import logging
import json
import threading
from fastapi import FastAPI
import uvicorn
import websockets
from telegram import Bot
from telegram.error import TelegramError

# === CONFIG ===
MONITORED_WALLET = "7rtiKSUDLBm59b1SBmD9oajcP8xE64vAGSMbAN5CXy1q"
TELEGRAM_BOT_TOKEN = "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4"
TELEGRAM_USER_ID = 7683338204

SOLANA_RPC_WS = "wss://api.mainnet-beta.solana.com"

# === SETUP ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
bot = Bot(token=TELEGRAM_BOT_TOKEN)

@app.get("/")
async def root():
    return {"status": "ok"}

# === HELPER ===
async def send_telegram_alert(mint_address: str, amount: int, signature: str):
    try:
        message = (
            f"\u2728 *New SPL Token Mint Detected* \u2728\n"
            f"\n*Mint:* `{mint_address}`"
            f"\n*Amount:* {amount}"
            f"\n[View on Solscan](https://solscan.io/tx/{signature})"
        )
        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=message, parse_mode="Markdown")
        logger.info("Alert sent for %s", signature)
    except TelegramError as e:
        logger.error("Telegram error: %s", str(e))

# === MINT DETECTION ===
async def monitor_wallet():
    subscription_id = None
    while True:
        try:
            async with websockets.connect(SOLANA_RPC_WS) as ws:
                sub_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [MONITORED_WALLET]},
                        {"commitment": "confirmed"}
                    ]
                }
                await ws.send(json.dumps(sub_request))
                logger.info("Subscribed to logs for wallet: %s", MONITORED_WALLET)

                while True:
                    raw_msg = await ws.recv()
                    msg = json.loads(raw_msg)

                    if "result" in msg and "id" in msg:
                        subscription_id = msg["result"]
                        continue

                    if "method" not in msg or msg["method"] != "logsNotification":
                        continue

                    logs = msg.get("params", {}).get("result", {})
                    tx_signature = logs.get("signature")
                    await asyncio.sleep(0.5)  # Avoid rate limiting
                    await inspect_transaction(tx_signature)
        
        except Exception as e:
            logger.exception("Error in monitor_wallet: %s", str(e))
            await asyncio.sleep(5)

async def inspect_transaction(signature):
    try:
        url = "https://api.mainnet-beta.solana.com"
        headers = {"Content-Type": "application/json"}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}]
        }

        async with asyncio.Semaphore(5):  # Limit concurrent calls
            reader, writer = await asyncio.open_connection("api.mainnet-beta.solana.com", 443, ssl=True)
            writer.write((json.dumps(body) + "\n").encode())
            await writer.drain()
            response = await reader.read(65536)
            writer.close()
            await writer.wait_closed()

        resp_json = json.loads(response.decode())
        tx = resp_json.get("result", {})
        if not tx:
            return

        meta = tx.get("meta", {})
        inner_instructions = meta.get("innerInstructions", [])
        for inner in inner_instructions:
            for ix in inner.get("instructions", []):
                if ix.get("program") != "spl-token":
                    continue
                if ix.get("parsed", {}).get("type") != "mintTo":
                    continue
                info = ix.get("parsed", {}).get("info", {})
                if info.get("to") != MONITORED_WALLET:
                    continue

                mint = info.get("mint")
                amount = int(info.get("amount", 0))
                await send_telegram_alert(mint, amount, signature)
    except Exception as e:
        logger.exception("Error inspecting transaction %s: %s", signature, str(e))

# === RUN BOTH ===
def start_fastapi():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

def start_monitor():
    asyncio.run(monitor_wallet())

if __name__ == "__main__":
    threading.Thread(target=start_fastapi, daemon=True).start()
    start_monitor()
