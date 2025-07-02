import os
import asyncio
import logging
import json
import threading
from fastapi import FastAPI
import uvicorn
import websockets
import httpx
from telegram import Bot
from telegram.error import TelegramError

# === CONFIG ===
MONITORED_WALLET = os.environ.get("MONITORED_WALLET", "7rtiKSUDLBm59b1SBmD9oajcP8xE64vAGSMbAN5CXy1q")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4")
TELEGRAM_USER_ID = int(os.environ.get("TELEGRAM_USER_ID", "7683338204"))

SOLANA_RPC_WS = "wss://api.mainnet-beta.solana.com"
RPC_HTTP_URL = "https://api.mainnet-beta.solana.com"

# === SETUP ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
bot = Bot(token=TELEGRAM_BOT_TOKEN)
semaphore = asyncio.Semaphore(5)

@app.get("/")
async def root():
    return {"status": "ok"}

# === HELPER ===
async def send_telegram_alert(mint_address: str, amount: int, signature: str):
    try:
        message = (
            f"✨ *New SPL Token Mint Detected* ✨\n"
            f"\n*Mint:* `{mint_address}`"
            f"\n*Amount:* {amount}"
            f"\n[View on Solscan](https://solscan.io/tx/{signature})"
        )
        await bot.send_message(chat_id=TELEGRAM_USER_ID, text=message, parse_mode="Markdown")
        logger.info("Alert sent for tx %s: mint %s amount %s", signature, mint_address, amount)
    except TelegramError as e:
        logger.error("Telegram error: %s", str(e))

# === MINT DETECTION ===
async def monitor_wallet():
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

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON message: %s", raw_msg)
                        continue

                    if msg.get("method") != "logsNotification":
                        continue

                    result = msg.get("params", {}).get("result", {})
                    value = result.get("value", {})
                    tx_signature = value.get("signature")
                    if not tx_signature:
                        logger.warning("Log without signature: %s", value)
                        continue

                    # Avoid spamming
                    await asyncio.sleep(0.5)
                    asyncio.create_task(inspect_transaction(tx_signature))
        except Exception as e:
            logger.exception("Error in monitor_wallet: %s", str(e))
            await asyncio.sleep(5)

async def inspect_transaction(signature: str):
    if not signature:
        logger.warning("inspect_transaction called with empty signature")
        return

    async with semaphore:
        try:
            headers = {"Content-Type": "application/json"}
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}]
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(RPC_HTTP_URL, headers=headers, json=body, timeout=10.0)
            if resp.status_code != 200:
                logger.warning("Solana RPC returned %s for tx %s", resp.status_code, signature)
                return

            try:
                resp_json = resp.json()
            except json.JSONDecodeError as e:
                logger.error("Invalid JSON from Solana RPC for tx %s: %s", signature, e)
                return

            tx = resp_json.get("result")
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
            
