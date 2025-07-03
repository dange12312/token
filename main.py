import json
import asyncio
import requests
import websockets
import threading
import os
from fastapi import FastAPI
import uvicorn
from datetime import datetime, timezone, timedelta

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
USER_ID = os.getenv("USER_ID", "YOUR_USER_ID")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
THRESHOLD = int(100 * 1e6)  # 100 USDC
WINDOW_MINUTES = 5

# === MONITORED MAIN WALLETS ===
MAIN_WALLETS = [
    "8pV7xKJfyudYM8bLWMAEgB4xiit5g3qh7uVihBFh233e",
    # Add more if needed
]

# === STATE ===
token_accounts = []
balances = {}
subs_usdc = {}  # sub ID -> token account
watched_wallets = {}  # wallet -> valid until
logs_sub = {}  # wallet -> sub ID
seen_mints = set()
logs_ws = None

# === FASTAPI ===
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "running"}

# === TELEGRAM ALERT ===
def notify(msg):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": USER_ID, "text": msg, "parse_mode": "Markdown"}
    )

# === HELPER ===
def timestamp():
    return datetime.now(timezone.utc).isoformat()

# === INIT USDC ACCOUNTS ===
async def get_usdc_atas():
    for wallet in MAIN_WALLETS:
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}]
        }
        r = requests.post(RPC_HTTP, json=body).json()
        for acc in r.get("result", {}).get("value", []):
            ata = acc.get("pubkey")
            if ata and ata not in token_accounts:
                token_accounts.append(ata)
                balances[ata] = None

# === GET OWNER OF TOKEN ACCOUNT ===
async def get_owner(token_account):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [token_account, {"encoding": "jsonParsed"}]
    }
    r = requests.post(RPC_HTTP, json=body).json()
    return r.get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {}).get("owner")

# === USDC ACCOUNT SUBSCRIBE ===
async def subscribe_usdc(ws):
    for i, acc in enumerate(token_accounts, 1):
        req = {
            "jsonrpc": "2.0", "id": i, "method": "accountSubscribe",
            "params": [acc, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        }
        await ws.send(json.dumps(req))
        res = json.loads(await ws.recv())
        subs_usdc[res.get("result")] = acc

# === CLEANUP ===
async def cleanup(wallet):
    await asyncio.sleep(WINDOW_MINUTES * 60)
    sub_id = logs_sub.get(wallet)
    if sub_id and logs_ws and logs_ws.open:
        await logs_ws.send(json.dumps({
            "jsonrpc": "2.0", "id": sub_id,
            "method": "logsUnsubscribe", "params": [sub_id]
        }))
        logs_sub.pop(wallet, None)
    watched_wallets.pop(wallet, None)
    notify(f"🔕 Stopped watching `{wallet}`.")

# === INSPECT TRANSACTION ===
async def inspect_tx(signature):
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {"encoding": "json"}]
    }
    r = requests.post(RPC_HTTP, json=body).json()
    tx = r.get("result", {})
    post_balances = tx.get("meta", {}).get("postTokenBalances", [])
    now = datetime.now(timezone.utc)
    for bal in post_balances:
        mint = bal.get("mint")
        owner = bal.get("owner")
        if mint != USDC_MINT and mint not in seen_mints and owner in watched_wallets:
            if watched_wallets[owner] >= now:
                seen_mints.add(mint)
                notify(
                    f"🎯 *New Token Acquired:* [Copy Contract](https://solscan.io/token/{mint}) by `{owner}`"
                )

# === LOGS SUBSCRIBE ===
async def subscribe_logs_for(wallet):
    global logs_ws
    if not logs_ws or not logs_ws.open:
        logs_ws = await websockets.connect(RPC_WS, ping_interval=30)
    sub_id = len(logs_sub) + 100
    req = {
        "jsonrpc": "2.0", "id": sub_id, "method": "logsSubscribe",
        "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}]
    }
    await logs_ws.send(json.dumps(req))
    res = json.loads(await logs_ws.recv())
    logs_sub[wallet] = res.get("result")

# === LOG LISTENER ===
async def listen_logs():
    global logs_ws
    logs_ws = await websockets.connect(RPC_WS, ping_interval=30)
    async for raw in logs_ws:
        msg = json.loads(raw)
        if msg.get("method") != "logsNotification":
            continue
        sig = msg.get("params", {}).get("result", {}).get("value", {}).get("signature")
        if sig:
            asyncio.create_task(inspect_tx(sig))

# === USDC MONITOR ===
async def listen_usdc():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_usdc(ws)
        notify(f"🤖 Monitoring {len(token_accounts)} USDC accounts.")
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification":
                continue
            sub_id = msg["params"]["subscription"]
            acc = subs_usdc.get(sub_id)
            info = msg["params"]["result"]["value"]["data"]["parsed"]["info"]
            amt = int(info.get("tokenAmount", {}).get("amount", 0))
            if balances[acc] is None:
                balances[acc] = amt
                continue
            diff = amt - balances[acc]
            balances[acc] = amt
            if diff < 0 and abs(diff) >= THRESHOLD:
                usdc_out = abs(diff) / 1e6
                to_acc = info.get("destination") or info.get("to")
                owner = await get_owner(to_acc)
                notify(f"🚨 {usdc_out:.2f} USDC sent from `{acc}`")
                if owner and owner not in watched_wallets:
                    await subscribe_logs_for(owner)
                    watched_wallets[owner] = datetime.now(timezone.utc) + timedelta(minutes=WINDOW_MINUTES)
                    asyncio.create_task(cleanup(owner))
                    notify(f"🔔 Watching `{owner}` for SPL token buys")

# === MAIN RUNNER ===
async def main():
    await get_usdc_atas()
    await asyncio.gather(listen_usdc(), listen_logs())

def start_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    threading.Thread(target=start_fastapi, daemon=True).start()
    print(f"[{timestamp()}] Bot starting...")
    asyncio.run(main())
    
