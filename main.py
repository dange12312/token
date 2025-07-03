import os
import json
import asyncio
import requests
import websockets
import threading
from datetime import datetime, timezone
from fastapi import FastAPI
import uvicorn

# Configuration
BOT_TOKEN = "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4"
USER_ID = "7683338204"
WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
THRESHOLD = int(100 * 1e6)
RPC_WS = "wss://api.mainnet-beta.solana.com/"

# Globals
subs = {}
balances = {}
seen_tokens = set()
watching_wallets = {}

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok"}

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def notify_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    res = requests.post(url, data={"chat_id": USER_ID, "text": message, "parse_mode": "Markdown"})
    print("Telegram response:", res.status_code, res.text)

async def subscribe_wallets(ws):
    for i, wallet in enumerate(WALLETS, 1):
        ata = await get_usdc_ata(wallet)
        req = {
            "jsonrpc": "2.0",
            "id": i,
            "method": "accountSubscribe",
            "params": [ata, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
        sub_id = resp.get("result")
        subs[sub_id] = ata
        balances[ata] = None

async def get_usdc_ata(wallet):
    from base58 import b58decode
    import hashlib
    seed = bytes(wallet, 'utf-8') + bytes(USDC_MINT, 'utf-8') + b"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    return hashlib.sha256(seed).hexdigest()[:32]  # Placeholder: Replace with actual PDA calculation

async def monitor_wallet_b(wallet):
    if wallet in watching_wallets:
        return
    watching_wallets[wallet] = True

    async def monitor():
        try:
            req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "accountSubscribe",
                "params": [wallet, {"encoding": "jsonParsed", "commitment": "confirmed"}]
            }
            async with websockets.connect(RPC_WS) as ws:
                await ws.send(json.dumps(req))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=300)
                    msg = json.loads(raw)
                    if msg.get("method") != "accountNotification":
                        continue
                    parsed = msg["params"]["result"]["value"]
                    tokens = parsed.get("data", {}).get("parsed", {}).get("info", {}).get("tokenAmount", {})
                    mint = parsed.get("data", {}).get("parsed", {}).get("info", {}).get("mint")
                    if mint and mint not in seen_tokens and mint != USDC_MINT:
                        seen_tokens.add(mint)
                        notify_telegram(f"🎯 *New Token Acquired:*
[Copy Contract](https://solscan.io/token/{mint})")
                        break
        except Exception as e:
            print(f"[{timestamp()}] Wallet B error: {e}")
        finally:
            watching_wallets.pop(wallet, None)

    asyncio.create_task(monitor())

async def listen_transactions():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_wallets(ws)
        notify_telegram("🤖 Bot started. Monitoring USDC outflows.")

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification":
                continue

            params = msg["params"]
            sub_id = params["subscription"]
            ata = subs.get(sub_id)
            info = params["result"]["value"]["data"]["parsed"]["info"]
            amount = int(info.get("tokenAmount", {}).get("amount", 0))

            if balances[ata] is None:
                balances[ata] = amount
                continue

            diff = amount - balances[ata]
            balances[ata] = amount

            if diff < 0 and abs(diff) >= THRESHOLD:
                from_wallet = ata
                outflow = abs(diff) / 1e6
                notify_telegram(f"🚨 *{outflow:.2f} USDC* sent from `{from_wallet}`")

                # Simulate receiver wallet B
                wallet_b = info.get("owner")
                if wallet_b:
                    await monitor_wallet_b(wallet_b)

async def run_forever():
    delay = 1
    while True:
        try:
            await listen_transactions()
        except Exception as err:
            notify_telegram(f"⚠️ Bot error: {err}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 1

def start_fastapi():
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=start_fastapi, daemon=True).start()
    print(f"[{timestamp()}] Starting monitor...")
    asyncio.run(run_forever())
    
