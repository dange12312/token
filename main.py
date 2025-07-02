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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4")
USER_ID = os.getenv("USER_ID", "7683338204")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
THRESHOLD = int(100 * 1e6)  # 100 USDC

# Wallets to monitor
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
logs_sub_ids = set()             # active logs subscription IDs

def timestamp(): return datetime.now(timezone.utc).isoformat()

def notify(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": USER_ID, "text": msg, "parse_mode": "Markdown"})

# ─── FastAPI ─────────────────────────────────────────────────────────────
app = FastAPI()
@app.get("/")
async def root(): return {"status":"ok"}

# ─── Initialize USDC Token Accounts ───────────────────────────────────────
async def get_usdc_accounts():
    global token_accounts
    for w in WALLETS:
        body = {"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner","params":[w,{"mint":USDC_MINT},{"encoding":"jsonParsed"}]}
        r = requests.post(RPC_HTTP, json=body).json()
        for acc in r.get("result",{}).get("value",[]):
            pk = acc.get("pubkey")
            if pk and pk not in token_accounts:
                token_accounts.append(pk);
                balances[pk]=None

# ─── Subscribe USDC Accounts ─────────────────────────────────────────────
async def subscribe_usdc(ws):
    for i,acc in enumerate(token_accounts,1):
        req={"jsonrpc":"2.0","id":i,"method":"accountSubscribe","params":[acc,{"encoding":"jsonParsed","commitment":"confirmed"}]}
        await ws.send(json.dumps(req)); r=json.loads(await ws.recv()); subs_usdc[r.get("result")]=acc

# ─── Listen for USDC Outflows & Detect New Wallet B ───────────────────────
async def listen_usdc():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        await subscribe_usdc(ws)
        notify(f"🤖 Bot started, monitoring {len(token_accounts)} USDC accounts.")
        async for msg_raw in ws:
            msg=json.loads(msg_raw)
            if msg.get("method")!="accountNotification": continue
            p=msg["params"]; acc=subs_usdc.get(p.get("subscription")); d=p["result"]["value"]["data"]
            if not isinstance(d,dict): continue
            info=d.get("parsed",{}).get("info",{}); amt=int(info.get("tokenAmount",{}).get("amount",0))
            if balances[acc] is None: balances[acc]=amt; continue
            diff=amt-balances[acc]; balances[acc]=amt
            if diff<0 and abs(diff)>=THRESHOLD:
                out=abs(diff)/1e6
                notify(f"🚨 *{out:.2f} USDC* sent from `{acc}`")
                # extract token-account B
                to_acc=info.get("destination") or info.get("to")
                if to_acc:
                    # subscribe logs for wallet B
                    owner = await get_owner_of_token_account(to_acc)
                    if owner and owner not in WALLETS:
                        WALLETS.append(owner)
                        await subscribe_logs_for(owner)
                        notify(f"🔔 Now watching new wallet `{owner}` for token buys.")

# ─── RPC call: get owner of token account ─────────────────────────────────
async def get_owner_of_token_account(token_acc):
    body={"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":[token_acc,{"encoding":"jsonParsed"}]}
    r=requests.post(RPC_HTTP,json=body).json()
    return r.get("result",{}).get("value",{}).get("data",{}).get("parsed",{}).get("info",{}).get("owner")

# ─── Subscribe to logs for SPL buys ───────────────────────────────────────
async def subscribe_logs_for(wallet):
    global logs_ws
    if logs_ws and logs_ws.open:
        req={"jsonrpc":"2.0","id":len(logs_sub_ids)+1,
             "method":"logsSubscribe","params":[{"mentions":[wallet]},{"commitment":"confirmed"}]}
        await logs_ws.send(json.dumps(req)); r=json.loads(await logs_ws.recv())
        logs_sub_ids.add(r.get("result"))

# ─── Listen logs: detect new token buys on any watched wallet ──────────────
async def listen_logs():
    global logs_ws
    logs_ws=await websockets.connect(RPC_WS,ping_interval=30)
    # subscribe main wallets first
    for w in WALLETS:
        await subscribe_logs_for(w)
    async for msg_raw in logs_ws:
        msg=json.loads(msg_raw)
        if msg.get("method")!="logsNotification": continue
        sig=msg["params"]["result"]["value"]["signature"]
        asyncio.create_task(inspect_purchase(sig))

# ─── Inspect purchase tx for new SPL token mints/transfers ───────────────
async def inspect_purchase(sig):
    body={"jsonrpc":"2.0","id":1,"method":"getTransaction","params":[sig,{"encoding":"json","maxSupportedTransactionVersion":0}]}
    r=requests.post(RPC_HTTP,json=body).json(); tx=r.get("result",{})
    for inner in tx.get("meta",{}).get("innerInstructions",[]):
        for ix in inner.get("instructions",[]):
            if ix.get("program")!="spl-token": continue
            parsed=ix.get("parsed",{}); typ=parsed.get("type")
            if typ not in("transfer","mintTo"): continue
            info=parsed.get("info",{}); mint=info.get("mint")
            if mint and mint!=USDC_MINT and mint not in seen_token_mints:
                seen_token_mints.add(mint)
                link=f"https://solscan.io/token/{mint}"
                notify(f"✨ New token acquired! [`{mint}`]({link})")

# ─── Main Runner ─────────────────────────────────────────────────────────
async def run_forever():
    await get_usdc_accounts()
    await asyncio.gather(listen_usdc(), listen_logs())

# ─── Deploy ──────────────────────────────────────────────────────────────
def start_fastapi():
    uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT",8000)))

if __name__=="__main__":
    threading.Thread(target=start_fastapi,daemon=True).start()
    print(f"[{timestamp()}] Bot starting…")
    asyncio.run(run_forever())
    
