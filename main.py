import json
import asyncio
import requests
import websockets
import threading
import os
from fastapi import FastAPI
import uvicorn
from datetime import datetime, timezone

# ─── Configuration & Globals ─────────────────────────────────────────────
BOT_TOKEN = "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4"
USER_ID = "7683338204"

# Monitor USDC token balances in associated token accounts
WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]

RPC_WS = "wss://api.mainnet-beta.solana.com/"
# 100 USDC threshold (USDC has 6 decimals)
THRESHOLD = int(100 * 1e6)

subs = {}
balances = {}

# ─── FastAPI Setup (Open Port) ────────────────────────────────────────────
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok"}

# ─── Utility Functions ───────────────────────────────────────────────────
def timestamp():
    return datetime.now(timezone.utc).isoformat()

def notify_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    res = requests.post(url, data={"chat_id": USER_ID, "text": message})
    print("Telegram response:", res.status_code, res.text)
    if res.status_code != 200:
        print(f"[{timestamp()}] ⚠️ Telegram error:", res.text)

# ─── Wallet Subscription + Monitoring ────────────────────────────────────
async def subscribe_wallets(ws):
    for i, wallet in enumerate(WALLETS, 1):
        req = {
            "jsonrpc": "2.0",
            "id": i,
            "method": "accountSubscribe",
            "params": [wallet, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
        sub_id = resp.get("result")
        subs[sub_id] = wallet
        balances[wallet] = None

async def listen_transactions():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_wallets(ws)
        print(f"[{timestamp()}] ✅ Subscribed to {len(WALLETS)} USDC token accounts.")

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification":
                continue

            params = msg["params"]
            sub_id = params["subscription"]
            wallet = subs.get(sub_id)
            info = params["result"]["value"]["data"]["parsed"]["info"]
            amount = int(info.get("tokenAmount", {}).get("amount", 0))

            if balances[wallet] is None:
                balances[wallet] = amount
                continue

            diff = amount - balances[wallet]
            balances[wallet] = amount

            if diff < 0 and abs(diff) >= THRESHOLD:
                outflow = abs(diff) / 1e6
                message = (
                    f"🚨 {outflow:.2f} USDC sent from {wallet}\n"
                    f"Time: {timestamp()}\n"
                    f"https://solscan.io/account/{wallet}"
                )
                notify_telegram(message)
                print(f"[{timestamp()}] ✉️ Alert: {wallet} -{outflow:.2f} USDC")

# ─── Combined Run Forever Task with Alerts ────────────────────────────────
async def run_forever():
    # Notify bot start
    notify_telegram("🤖 USDC Monitor Bot has started and is now monitoring outflows.")
    delay = 1
    while True:
        try:
            await listen_transactions()
        except Exception as err:
            # Notify bot error/stop
            notify_telegram(f"⚠️ USDC Monitor Bot encountered an error and stopped: {err}")
            print(f"[{timestamp()}] 🔁 Error: {err} — retrying in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 1

# ─── Start FastAPI & Monitoring ──────────────────────────────────────────
def start_fastapi():
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    # Start web service for deployment
    threading.Thread(target=start_fastapi, daemon=True).start()
    # Start monitoring loop
    print(f"[{timestamp()}] 🔌 Starting USDC Outflow Monitor…")
    asyncio.run(run_forever())
    
