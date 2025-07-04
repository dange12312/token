import os
import json
import asyncio
import requests
import websockets
import threading
from fastapi import FastAPI
import uvicorn
from datetime import datetime, timezone, timedelta

# ─── Configuration ─────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4")
CHAT_ID = os.getenv("CHAT_ID", "7683338204")
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MAIN_WALLETS = [
    "8pV7xKJfyudYM8bLWMAEgB4xiit5g3qh7uVihBFh233e",
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa"
]
OUTFLOW_THRESHOLD = 100 * 1e6  # in USDC base units
WATCH_WINDOW = 5  # in minutes

# ─── Globals ───────────────────────────────────────────────
usdc_atas = []
balances = {}
seen_tokens = set()
watching = {}

# ─── FastAPI ───────────────────────────────────────────────
app = FastAPI()
@app.get("/")
def root(): return {"status": "running"}

def notify(msg):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    )

# ─── Utilities ─────────────────────────────────────────────
def timestamp():
    return datetime.now(timezone.utc).isoformat()

async def get_token_accounts():
    for wallet in MAIN_WALLETS:
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}]
        }
        r = requests.post(RPC_HTTP, json=body).json()
        for acc in r.get("result", {}).get("value", []):
            pubkey = acc.get("pubkey")
            if pubkey and pubkey not in usdc_atas:
                usdc_atas.append(pubkey)
                balances[pubkey] = None

async def subscribe_atas(ws):
    for idx, ata in enumerate(usdc_atas, start=1):
        sub_msg = {
            "jsonrpc": "2.0", "id": idx,
            "method": "accountSubscribe",
            "params": [ata, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }
        await ws.send(json.dumps(sub_msg))

async def get_owner(account):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [account, {"encoding": "jsonParsed"}]
    }
    r = requests.post(RPC_HTTP, json=body).json()
    return (r.get("result") or {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {}).get("owner")

async def monitor_outflows():
    async with websockets.connect(RPC_WS) as ws:
        await subscribe_atas(ws)
        notify(f"🤖 Monitoring {len(usdc_atas)} USDC ATAs.")
        while True:
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification": continue
            acc = msg["params"]["result"]["context"]["slot"]
            val = msg["params"]["result"]["value"]
            ata = msg["params"]["subscription"]  # index based
            parsed = val.get("data", {}).get("parsed", {})
            info = parsed.get("info", {})
            amount = int(info.get("tokenAmount", {}).get("amount", 0))
            token_acc = msg["params"]["subscription"]
            if balances[token_acc] is None:
                balances[token_acc] = amount
                continue
            diff = amount - balances[token_acc]
            balances[token_acc] = amount

            # Only track significant outflows
            if diff < 0 and abs(diff) >= OUTFLOW_THRESHOLD:
                dest = info.get("destination") or info.get("to")
                main_owner = await get_owner(token_acc)
                outflow = abs(diff) / 1e6
                notify(f"🚨 *{outflow:.2f} USDC* outflow from `{main_owner}` to `{dest}`")

                if dest:
                    watching[dest] = datetime.now(timezone.utc) + timedelta(minutes=WATCH_WINDOW)

async def monitor_logs():
    async with websockets.connect(RPC_WS) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
            "params": [{"mentions": list(watching)}, {"commitment": "confirmed"}]
        }))
        while True:
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("method") != "logsNotification": continue
            sig = msg.get("params", {}).get("result", {}).get("value", {}).get("signature")
            asyncio.create_task(check_transaction(sig))

async def check_transaction(sig):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [sig, {"encoding": "json"}]
    }
    r = requests.post(RPC_HTTP, json=body).json()
    tx = r.get("result", {})
    post_tokens = tx.get("meta", {}).get("postTokenBalances", [])
    now = datetime.now(timezone.utc)
    for entry in post_tokens:
        owner = entry.get("owner")
        mint = entry.get("mint")
        if mint == USDC_MINT: continue
        if mint in seen_tokens: continue
        if owner in watching and watching[owner] >= now:
            seen_tokens.add(mint)
            link = f"https://solscan.io/token/{mint}"
            notify(f"✨ New token acquired: [`{mint}`]({link}) by `{owner}`")

async def run():
    await get_token_accounts()
    await asyncio.gather(monitor_outflows(), monitor_logs())

def start_server():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    asyncio.run(run())
    
