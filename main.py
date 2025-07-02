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
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
THRESHOLD = int(100 * 1e6)  # 100 USDC

WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]

subs = {}
balances = {}
token_accounts = []

# ─── FastAPI Setup ────────────────────────────────────────────────────────
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

async def get_usdc_token_accounts():
    global token_accounts
    headers = {"Content-Type": "application/json"}
    for wallet in WALLETS:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                wallet,
                {"mint": USDC_MINT},
                {"encoding": "jsonParsed"}
            ]
        }
        try:
            resp = requests.post(RPC_HTTP, headers=headers, json=body, timeout=10)
            data = resp.json()
            for acc in data["result"]["value"]:
                pubkey = acc["pubkey"]
                token_accounts.append(pubkey)
                balances[pubkey] = None
                print(f"🔗 Tracked USDC Token Account: {pubkey}")
        except Exception as e:
            print(f"Failed to get token account for {wallet}: {e}")

# ─── WebSocket Subscriptions ─────────────────────────────────────────────
async def subscribe_token_accounts(ws):
    for i, acc in enumerate(token_accounts, 1):
        req = {
            "jsonrpc": "2.0",
            "id": i,
            "method": "accountSubscribe",
            "params": [acc, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
        sub_id = resp.get("result")
        subs[sub_id] = acc

# ─── Listener ────────────────────────────────────────────────────────────
async def listen_transactions():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_token_accounts(ws)
        print(f"[{timestamp()}] ✅ Subscribed to {len(token_accounts)} USDC token accounts.")

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification":
                continue

            params = msg["params"]
            sub_id = params.get("subscription")
            acc = subs.get(sub_id)
            data_field = params.get("result", {}).get("value", {}).get("data")

            if not isinstance(data_field, dict):
                continue

            parsed = data_field.get("parsed", {})
            info = parsed.get("info", {})
            token_amount = info.get("tokenAmount", {})
            amount = int(token_amount.get("amount", 0))

            if balances[acc] is None:
                balances[acc] = amount
                continue

            diff = amount - balances[acc]
            balances[acc] = amount

            if diff < 0 and abs(diff) >= THRESHOLD:
                usdc = abs(diff) / 1e6
                msg = (
                    f"🚨 {usdc:.2f} USDC sent from {acc}\n"
                    f"Time: {timestamp()}\n"
                    f"https://solscan.io/account/{acc}"
                )
                notify_telegram(msg)
                print(f"[{timestamp()}] ✉️ Alert: {acc} -{usdc:.2f} USDC")

# ─── Runner ──────────────────────────────────────────────────────────────
async def run_forever():
    await get_usdc_token_accounts()
    notify_telegram("🤖 USDC Monitor Bot has started and is now monitoring outflows.")
    delay = 1
    while True:
        try:
            await listen_transactions()
        except Exception as err:
            notify_telegram(f"⚠️ USDC Monitor Bot encountered an error and stopped: {err}")
            print(f"[{timestamp()}] 🔁 Error: {err} — retrying in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 1

# ─── Deployment ──────────────────────────────────────────────────────────
def start_fastapi():
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=start_fastapi, daemon=True).start()
    print(f"[{timestamp()}] 🔌 Starting USDC Outflow Monitor…")
    asyncio.run(run_forever())
    
