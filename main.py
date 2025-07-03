import json
import asyncio
import requests
import websockets
import threading
import os
from fastapi import FastAPI
import uvicorn
from datetime import datetime, timezone, timedelta

# ─── Configuration ─────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4")
USER_ID = os.getenv("USER_ID", "7683338204")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
THRESHOLD = int(100 * 1e6)  # 100 USDC
WINDOW_MINUTES = 5

# ─── Wallets to Monitor ────────────────────────────────────────────────
WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]

# ─── Globals ───────────────────────────────────────────────────────────
token_accounts = []
subs_usdc = {}            # subscription id → token account
balances = {}            # token account → last balance
logs_sub = {}            # wallet → log subscription id
watched_wallets = {}     # wallet → expiry datetime
seen_token_mints = set()
logs_ws = None

# ─── FastAPI App ───────────────────────────────────────────────────────
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok"}

# ─── Utilities ─────────────────────────────────────────────────────────
def timestamp():
    return datetime.now(timezone.utc).isoformat()

def notify(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": USER_ID, "text": msg, "parse_mode": "Markdown"}
        )
    except Exception as e:
        print(f"[Telegram Error] {e}")

# ─── Get USDC Token Accounts ───────────────────────────────────────────
async def get_usdc_token_accounts():
    for wallet in WALLETS:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}]
        }
        resp = requests.post(RPC_HTTP, json=payload).json()
        for acc in resp.get("result", {}).get("value", []):
            ata = acc.get("pubkey")
            if ata and ata not in token_accounts:
                token_accounts.append(ata)
                balances[ata] = None

# ─── Get Owner of Token Account ────────────────────────────────────────
async def get_owner_of_token_account(token_account):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [token_account, {"encoding": "jsonParsed"}]
    }
    resp = requests.post(RPC_HTTP, json=payload).json()
    return resp.get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {}).get("owner")

# ─── Cleanup Wallet B ──────────────────────────────────────────────────
async def cleanup_wallet(wallet):
    await asyncio.sleep(WINDOW_MINUTES * 60)
    sub_id = logs_sub.get(wallet)
    if sub_id and logs_ws and logs_ws.open:
        await logs_ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": sub_id,
            "method": "logsUnsubscribe",
            "params": [sub_id]
        }))
        logs_sub.pop(wallet, None)
    watched_wallets.pop(wallet, None)
    notify(f"🔕 Stopped watching `{wallet}` after {WINDOW_MINUTES} minutes.")

# ─── Subscribe Logs for Wallet ─────────────────────────────────────────
async def subscribe_logs(wallet):
    global logs_ws
    if not logs_ws or not logs_ws.open:
        logs_ws = await websockets.connect(RPC_WS, ping_interval=30)

    sub_id = len(logs_sub) + 100
    payload = {
        "jsonrpc": "2.0",
        "id": sub_id,
        "method": "logsSubscribe",
        "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}]
    }
    await logs_ws.send(json.dumps(payload))
    res = json.loads(await logs_ws.recv())
    logs_sub[wallet] = res.get("result")
    watched_wallets[wallet] = datetime.now(timezone.utc) + timedelta(minutes=WINDOW_MINUTES)
    asyncio.create_task(cleanup_wallet(wallet))
    notify(f"👀 Watching `{wallet}` for token buys for {WINDOW_MINUTES} min.")

# ─── Listen to USDC Token Account ──────────────────────────────────────
async def listen_usdc():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        for i, ata in enumerate(token_accounts):
            payload = {
                "jsonrpc": "2.0",
                "id": i + 1,
                "method": "accountSubscribe",
                "params": [ata, {"encoding": "jsonParsed", "commitment": "confirmed"}]
            }
            await ws.send(json.dumps(payload))
            res = json.loads(await ws.recv())
            subs_usdc[res.get("result")] = ata

        notify(f"🤖 Monitoring {len(token_accounts)} USDC accounts.")

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification":
                continue

            params = msg["params"]
            ata = subs_usdc.get(params.get("subscription"))
            info = params.get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {})
            amt = int(info.get("tokenAmount", {}).get("amount", 0))

            if balances[ata] is None:
                balances[ata] = amt
                continue

            diff = amt - balances[ata]
            balances[ata] = amt

            if diff < 0 and abs(diff) >= THRESHOLD:
                owner = await get_owner_of_token_account(info.get("destination") or info.get("to"))
                if owner and owner not in logs_sub:
                    await subscribe_logs(owner)
                notify(f"🚨 *{abs(diff)/1e6:.2f} USDC* sent from `{ata}`")

# ─── Inspect SPL Token Buys ────────────────────────────────────────────
async def inspect_transaction(sig):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}]
    }
    resp = requests.post(RPC_HTTP, json=payload).json()
    tx = resp.get("result", {})

    now = datetime.now(timezone.utc)
    for inner in tx.get("meta", {}).get("innerInstructions", []):
        for ix in inner.get("instructions", []):
            if ix.get("program") != "spl-token":
                continue
            parsed = ix.get("parsed", {})
            mint = parsed.get("info", {}).get("mint")
            dest = parsed.get("info", {}).get("to")
            if not mint or mint == USDC_MINT or mint in seen_token_mints:
                continue
            if dest in watched_wallets and watched_wallets[dest] >= now:
                seen_token_mints.add(mint)
                notify(f"🎯 *New Token Acquired:* [Copy Contract](https://solscan.io/token/{mint})")

# ─── Listen to Logs for SPL Token Buys ─────────────────────────────────
async def listen_logs():
    global logs_ws
    if not logs_ws or not logs_ws.open:
        logs_ws = await websockets.connect(RPC_WS, ping_interval=30)

    async for raw in logs_ws:
        msg = json.loads(raw)
        if msg.get("method") != "logsNotification":
            continue
        sig = msg.get("params", {}).get("result", {}).get("value", {}).get("signature")
        if sig:
            asyncio.create_task(inspect_transaction(sig))

# ─── Run Bot Forever ───────────────────────────────────────────────────
async def run_bot():
    await get_usdc_token_accounts()
    await asyncio.gather(listen_usdc(), listen_logs())

# ─── Start FastAPI Server ──────────────────────────────────────────────
def start_web():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    threading.Thread(target=start_web, daemon=True).start()
    asyncio.run(run_bot())
    
