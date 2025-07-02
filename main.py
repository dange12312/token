import json
import asyncio
import requests
import websockets
import threading
import os
from fastapi import FastAPI
import uvicorn
from datetime import datetime, timezone, timedelta

# ─── Configuration & Globals ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4")
USER_ID = os.getenv("USER_ID", "7683338204")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
THRESHOLD = int(100 * 1e6)  # 100 USDC
WINDOW_MINUTES = 5          # alert window duration

# Wallets to monitor initial USDC outflows
WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]

# State
subs_usdc = {}                   # accountSubscribe id -> token account
balances = {}                    # token account -> last balance
token_accounts = []              # USDC token accounts
seen_token_mints = set()         # dedupe new token alerts
logs_ws = None                   # global logs WS connection
logs_sub = {}                    # wallet -> logs subscription ID
watched_wallets = {}             # wallet -> valid_until datetime

# ─── Utilities ────────────────────────────────────────────────────────────
def timestamp(): return datetime.now(timezone.utc).isoformat()

def notify(msg):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": USER_ID, "text": msg, "parse_mode": "Markdown"}
    )

# ─── FastAPI ─────────────────────────────────────────────────────────────
app = FastAPI()
@app.get("/")
async def root(): return {"status":"ok"}

# ─── Initialize USDC Token Accounts ───────────────────────────────────────
async def get_usdc_accounts():
    for w in WALLETS:
        body = {"jsonrpc":"2.0","id":1,
                "method":"getTokenAccountsByOwner",
                "params":[w,{"mint":USDC_MINT},{"encoding":"jsonParsed"}]}
        r = requests.post(RPC_HTTP, json=body).json()
        for acc in r.get("result",{}).get("value",[]):
            pk = acc.get("pubkey")
            if pk and pk not in token_accounts:
                token_accounts.append(pk)
                balances[pk] = None

# ─── Subscribe USDC Accounts ─────────────────────────────────────────────
async def subscribe_usdc(ws):
    for i, acc in enumerate(token_accounts, 1):
        req = {"jsonrpc":"2.0","id":i,
               "method":"accountSubscribe",
               "params":[acc,{"encoding":"jsonParsed","commitment":"confirmed"}]}
        await ws.send(json.dumps(req))
        res = json.loads(await ws.recv())
        subs_usdc[res.get("result")] = acc

# ─── Cleanup after window or funds return ─────────────────────────────────
async def cleanup_wallet(owner):
    await asyncio.sleep(WINDOW_MINUTES * 60)
    sub_id = logs_sub.get(owner)
    if sub_id and logs_ws and logs_ws.open:
        await logs_ws.send(json.dumps({
            "jsonrpc":"2.0","id":sub_id,
            "method":"logsUnsubscribe",
            "params":[sub_id]
        }))
        del logs_sub[owner]
    watched_wallets.pop(owner, None)
    notify(f"🔕 Stopped watching `{owner}` after window or funds returned.")

# ─── Listen for USDC Outflows & Manage watchlist ──────────────────────────
async def listen_usdc():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_usdc(ws)
        notify(f"🤖 Monitoring {len(token_accounts)} USDC accounts.")
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") != "accountNotification": continue
            p = msg["params"]; acc = subs_usdc.get(p.get("subscription"))
            d = p.get("result", {}).get("value", {}).get("data")
            if not isinstance(d, dict): continue
            info = d.get("parsed", {}).get("info", {})
            amt = int(info.get("tokenAmount", {}).get("amount", 0))
            if balances[acc] is None:
                balances[acc] = amt; continue
            diff = amt - balances[acc]; balances[acc] = amt
            # Outflow -> add B to watchlist
            if diff < 0 and abs(diff) >= THRESHOLD:
                usdc_out = abs(diff) / 1e6
                notify(f"🚨 *{usdc_out:.2f} USDC* sent from `{acc}`")
                to_acc = info.get("destination") or info.get("to")
                if to_acc:
                    owner = await get_owner_of_token_account(to_acc)
                    if owner and owner not in logs_sub:
                        # subscribe logs for B
                        await subscribe_logs_for(owner)
                        end_time = datetime.now(timezone.utc) + timedelta(minutes=WINDOW_MINUTES)
                        watched_wallets[owner] = end_time
                        asyncio.create_task(cleanup_wallet(owner))
                        notify(f"🔔 Watching `{owner}` for token buys (next {WINDOW_MINUTES} min)")
            # Inflow back to main -> stop watch
            elif diff > 0:
                to_owner = None
                # if main wallet's USDC increases, likely return from a watched B; clear all
                for b in list(watched_wallets):
                    # unsubscribe each
                    await cleanup_wallet(b)

# ─── RPC: get owner of token account ──────────────────────────────────────
async def get_owner_of_token_account(token_acc):
    body = {"jsonrpc":"2.0","id":1,
            "method":"getAccountInfo",
            "params":[token_acc,{"encoding":"jsonParsed"}]}
    r = requests.post(RPC_HTTP, json=body).json()
    return r.get("result", {}).get("value", {}).get("data", {}).get("parsed", {})
            .get("info", {}).get("owner")

# ─── Subscribe to logs for SPL buys ───────────────────────────────────────
async def subscribe_logs_for(wallet):
    global logs_ws
    if not logs_ws or not logs_ws.open:
        logs_ws = await websockets.connect(RPC_WS, ping_interval=30)
    sub_id = len(logs_sub) + 1
    req = {"jsonrpc":"2.0","id":sub_id,
           "method":"logsSubscribe",
           "params":[{"mentions":[wallet]},{"commitment":"confirmed"}]}
    await logs_ws.send(json.dumps(req))
    res = json.loads(await logs_ws.recv())
    logs_sub[wallet] = res.get("result")

# ─── Listen logs: new SPL token detection ─────────────────────────────────
async def listen_logs():
    global logs_ws
    logs_ws = await websockets.connect(RPC_WS, ping_interval=30)
    # subscribe initial wallets
    for w in WALLETS:
        await subscribe_logs_for(w)
    async for raw in logs_ws:
        msg = json.loads(raw)
        if msg.get("method") != "logsNotification": continue
        sig = msg.get("params", {}).get("result", {}).get("value", {}).get("signature")
        asyncio.create_task(inspect_purchase(sig))

# ─── Inspect purchase for valid watched wallets ───────────────────────────
async def inspect_purchase(sig):
    body = {"jsonrpc":"2.0","id":1,
            "method":"getTransaction",
            "params":[sig,{"encoding":"json","maxSupportedTransactionVersion":0}]}
    r = requests.post(RPC_HTTP, json=body).json(); tx = r.get("result", {})
    now = datetime.now(timezone.utc)
    for inner in tx.get("meta", {}).get("innerInstructions", []):
        for ix in inner.get("instructions", []):
            if ix.get("program") != "spl-token": continue
            parsed = ix.get("parsed", {}); typ = parsed.get("type")
            if typ not in ("transfer", "mintTo"): continue
            info = parsed.get("info", {}); mint = info.get("mint"); dest = info.get("to")
            # alert only if within window and not seen
            if (mint and mint != USDC_MINT and mint not in seen_token_mints
                and dest in watched_wallets and watched_wallets[dest] >= now):
                seen_token_mints.add(mint)
                link = f"https://solscan.io/token/{mint}"
                notify(f"✨ New token acquired! [`{mint}`]({link}) by `{dest}`")

# ─── Main Runner ─────────────────────────────────────────────────────────
async def run_forever():
    await get_usdc_accounts()
    await asyncio.gather(listen_usdc(), listen_logs())

# ─── Deploy ──────────────────────────────────────────────────────────────
def start_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT",8000)))

if __name__=="__main__":
    threading.Thread(target=start_fastapi, daemon=True).start()
    print(f"[{timestamp()}] Bot starting…")
    asyncio.run(run_forever())
            
