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
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
USER_ID = os.getenv("USER_ID", "YOUR_USER_ID_HERE")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
THRESHOLD = int(100 * 1e6)  # 100 USDC
WINDOW_MINUTES = 5

# === MAIN WALLETS ===
MAIN_WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]

# === STATE ===
token_accounts = []            # USDC ATAs
balances = {}                  # ATA -> last balance
subs_usdc = {}                 # sub_id -> ATA
watched_wallets = {}           # wallet -> expiry datetime
logs_sub = {}                  # wallet -> logs sub_id
seen_mints = set()             # SPL mints alerted
logs_ws = None

# === FASTAPI ===
app = FastAPI()
@app.get("/")
async def root():
    return {"status": "running"}

# === UTILITIES ===
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

# === INIT USDC ATAs ===
async def get_usdc_atas():
    for wallet in MAIN_WALLETS:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}]
        }
        resp = requests.post(RPC_HTTP, json=payload).json()
        for acc in resp.get("result", {}).get("value", []):
            ata = acc.get("pubkey")
            if ata and ata not in token_accounts:
                token_accounts.append(ata)
                balances[ata] = None
    # Debug/Startup alert
    notify(f"🤖 Bot started. Monitoring {len(token_accounts)} USDC ATAs.")
    print(f"[{timestamp()}] Found {len(token_accounts)} USDC ATAs: {token_accounts}")

# === GET OWNER ===
async def get_owner(token_account):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [token_account, {"encoding": "jsonParsed"}]
    }
    resp = requests.post(RPC_HTTP, json=payload).json()
    return resp.get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {}).get("owner")

# === CLEANUP ===
async def cleanup(wallet):
    await asyncio.sleep(WINDOW_MINUTES * 60)
    sub_id = logs_sub.pop(wallet, None)
    if sub_id and logs_ws and logs_ws.open:
        await logs_ws.send(json.dumps({
            "jsonrpc": "2.0", "id": sub_id,
            "method": "logsUnsubscribe", "params": [sub_id]
        }))
    watched_wallets.pop(wallet, None)
    notify(f"🔕 Stopped watching `{wallet}`.")
    print(f"[{timestamp()}] Stopped watching {wallet}")

# === SUBSCRIBE LOGS FOR WALLET B ===
async def subscribe_logs(wallet):
    global logs_ws
    if not logs_ws or not logs_ws.open:
        logs_ws = await websockets.connect(RPC_WS, ping_interval=30)
    sub_id = len(logs_sub) + 100
    payload = {
        "jsonrpc": "2.0", "id": sub_id,
        "method": "logsSubscribe",
        "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}]
    }
    await logs_ws.send(json.dumps(payload))
    res = json.loads(await logs_ws.recv())
    logs_sub[wallet] = res.get("result")
    watched_wallets[wallet] = datetime.now(timezone.utc) + timedelta(minutes=WINDOW_MINUTES)
    notify(f"👀 Watching `{wallet}` for token buys for {WINDOW_MINUTES} min")
    print(f"[{timestamp()}] Subscribed logs for {wallet}, sub_id {res.get('result')}")

# === INSPECT TRANSACTION ===
async def inspect_tx(signature):
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "json"}]
    }
    r = requests.post(RPC_HTTP, json=payload).json()
    tx = r.get("result", {})
    post_bal = tx.get("meta", {}).get("postTokenBalances", [])
    now = datetime.now(timezone.utc)
    for bal in post_bal:
        mint = bal.get("mint")
        owner = bal.get("owner")
        if mint and mint != USDC_MINT and mint not in seen_mints and owner in watched_wallets and watched_wallets[owner] >= now:
            seen_mints.add(mint)
            notify(f"🎯 *New Token Acquired:* [Copy Contract](https://solscan.io/token/{mint}) by `{owner}`")
            print(f"[{timestamp()}] New token {mint} acquired by {owner}")

# === LOG LISTENER ===
async def listen_logs():
    global logs_ws
    logs_ws = await websockets.connect(RPC_WS, ping_interval=30)
    async for raw in logs_ws:
        msg = json.loads(raw)
        if msg.get("method") != "logsNotification":
            continue
        sig = msg.get("params", {}).get("result", {}).get("value", {}).get("signature")
        print(f"[DEBUG] logsNotification sig: {sig}")
        if sig:
            asyncio.create_task(inspect_tx(sig))

# === USDC LISTENER ===
async def listen_usdc():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        # subscribe to each ATA
        for i, ata in enumerate(token_accounts, 1):
            payload = {
                "jsonrpc": "2.0", "id": i,
                "method": "accountSubscribe",
                "params": [ata, {"encoding": "jsonParsed", "commitment": "confirmed"}]
            }
            await ws.send(json.dumps(payload))
            res = json.loads(await ws.recv())
            sub_id = res.get("result")
            subs_usdc[sub_id] = ata
            notify(f"🔔 Subscribed to ATA `{ata}` (sub {sub_id})")
            print(f"[{timestamp()}] Subscribed to ATA {ata} with sub_id {sub_id}")

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification":
                continue
            print(f"[DEBUG] accountNotification: {raw}")
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
                usdc_amt = abs(diff) / 1e6
                to_acc = info.get("destination") or info.get("to")
                owner = await get_owner(to_acc)
                notify(f"🚨 *{usdc_amt:.2f} USDC* outflow from `{ata}` to `{owner or to_acc}`")
                print(f"[{timestamp()}] USDC outflow {usdc_amt} from {ata} to {owner or to_acc}")
                if owner and owner not in watched_wallets:
                    await subscribe_logs(owner)

# === MAIN ===
async def main():
    await get_usdc_atas()
    await asyncio.gather(listen_usdc(), listen_logs())

def start_server():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    print(f"[{timestamp()}] Bot starting...")
    asyncio.run(main())
    
