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
BOT_TOKEN = os.getenv("BOT_TOKEN", "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4")
USER_ID = os.getenv("USER_ID", "7683338204")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
RPC_WS = "wss://api.mainnet-beta.solana.com/"
THRESHOLD = int(100 * 1e6)  # 100 USDC
WINDOW_MINUTES = 5

# === MONITORED MAIN WALLETS ===
MAIN_WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "6KK9rw7aU7HaLiHPiXHStcpSnSXuH5w7oh8Aa5ecT6Ck"
]

# === STATE ===
token_accounts = []          # USDC ATAs
balances = {}                # ATA -> last balance
subs_usdc = {}               # sub_id -> ATA
watched_atas = {}            # ATA -> expiry datetime
logs_sub = {}                # ATA -> logs sub_id
seen_mints = set()           # dedupe mints
logs_ws = None
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# === FASTAPI ===
app = FastAPI()
@app.get("/")
async def root(): return {"status":"running"}

# === UTILITIES ===
def timestamp(): return datetime.now(timezone.utc).isoformat()

def notify(msg):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": USER_ID, "text": msg, "parse_mode": "Markdown"}
    )

# === INIT USDC ATAs ===
async def get_usdc_atas():
    for wallet in MAIN_WALLETS:
        payload = {"jsonrpc":"2.0","id":1,"method":"getTokenAccountsByOwner",
                   "params":[wallet,{"mint":USDC_MINT},{"encoding":"jsonParsed"}]}
        r = requests.post(RPC_HTTP, json=payload).json()
        for acc in r.get("result",{}).get("value",[]):
            ata = acc.get("pubkey")
            if ata and ata not in token_accounts:
                token_accounts.append(ata)
                balances[ata] = None
    notify(f"🤖 Monitoring {len(token_accounts)} USDC ATAs.")
    print(f"[{timestamp()}] ATAs: {token_accounts}")

# === CLEANUP ATA WATCH ===
async def cleanup_ata(ata):
    await asyncio.sleep(WINDOW_MINUTES*60)
    sub_id = logs_sub.pop(ata, None)
    if sub_id and logs_ws and logs_ws.open:
        await logs_ws.send(json.dumps({
            "jsonrpc":"2.0","id":sub_id,
            "method":"logsUnsubscribe","params":[sub_id]
        }))
    watched_atas.pop(ata, None)
    notify(f"🔕 Stopped watching ATA `{ata}`.")
    print(f"[{timestamp()}] Cleaned up ATA {ata}")

# === INSPECT TX for new mints ===
async def inspect_tx(sig):
    body={"jsonrpc":"2.0","id":1,"method":"getTransaction",
          "params":[sig,{"encoding":"json","maxSupportedTransactionVersion":0}]}
    resp = requests.post(RPC_HTTP, json=body).json()
    tx = resp.get("result")
    if not tx: return
    keys = tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
    post_bal = tx.get("meta",{}).get("postTokenBalances",[])
    now = datetime.now(timezone.utc)
    for bal in post_bal:
        ata = keys[bal.get("accountIndex")]
        mint = bal.get("mint")
        if ata in watched_atas and watched_atas[ata]>=now and mint!=USDC_MINT and mint not in seen_mints:
            seen_mints.add(mint)
            notify(f"🎯 *New Token Acquired:* [Click to Copy](https://solscan.io/token/{mint})")
            print(f"[{timestamp()}] ATA {ata} acquired {mint}")

# === SUBSCRIBE LOGS for ANY ATA ===
async def subscribe_logs_for_ata(ata):
    global logs_ws
    if not logs_ws or not logs_ws.open:
        logs_ws = await websockets.connect(RPC_WS,ping_interval=30)
    sub_id = len(logs_sub)+100
    payload={"jsonrpc":"2.0","id":sub_id,
             "method":"logsSubscribe",
             "params":[{"mentions":[ata]},{"commitment":"confirmed"}]}
    await logs_ws.send(json.dumps(payload))
    res=json.loads(await logs_ws.recv())
    logs_sub[ata]=res.get("result")
    watched_atas[ata]=datetime.now(timezone.utc)+timedelta(minutes=WINDOW_MINUTES)
    asyncio.create_task(cleanup_ata(ata))
    notify(f"🔔 Watching ATA `{ata}` for buys for {WINDOW_MINUTES} min.")
    print(f"[{timestamp()}] Subscribed logs for ATA {ata}, sub_id {logs_sub[ata]}")

# === LISTEN USDC OUTFLOWS ===
async def listen_usdc():
    async with websockets.connect(RPC_WS,ping_interval=30) as ws:
        # subscribe to each ATA
        for i,ata in enumerate(token_accounts,1):
            req={"jsonrpc":"2.0","id":i,"method":"accountSubscribe",
                 "params":[ata,{"encoding":"jsonParsed","commitment":"confirmed"}]}
            await ws.send(json.dumps(req))
            res=json.loads(await ws.recv())
            subs_usdc[res.get("result")] = ata
            print(f"[DEBUG] Subscribed ATA {ata} sub {res.get('result')}")
        async for raw in ws:
            msg=json.loads(raw)
            if msg.get("method")!="accountNotification": continue
            print(f"[DEBUG] accountNotification: {raw}")
            subid=msg["params"]["subscription"]
            ata=subs_usdc.get(subid)
            info=msg["params"]["result"]["value"]["data"]["parsed"]["info"]
            amt=int(info.get("tokenAmount",{}).get("amount",0))
            if balances[ata] is None:
                balances[ata]=amt; continue
            diff=amt-balances[ata]; balances[ata]=amt
            if diff<0 and abs(diff)>=THRESHOLD:
                out=abs(diff)/1e6
                dest=info.get("destination") or info.get("to")
                notify(f"🚨 *{out:.2f} USDC* outflow from `{ata}` to `{dest}`")
                print(f"[{timestamp()}] Outflow {out} from {ata} to {dest}")
                await subscribe_logs_for_ata(dest)

# === LISTEN LOGS ===
async def listen_logs():
    global logs_ws
    logs_ws=await websockets.connect(RPC_WS,ping_interval=30)
    async for raw in logs_ws:
        msg=json.loads(raw)
        if msg.get("method")!="logsNotification": continue
        sig=msg.get("params",{}).get("result",{}).get("value",{}).get("signature")
        print(f"[DEBUG] logsNotification sig: {sig}")
        if sig: asyncio.create_task(inspect_tx(sig))

# === MAIN RUNNER ===
async def main():
    await get_usdc_atas()
    await asyncio.gather(listen_usdc(), listen_logs())

def start_web():
    uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT",8000)))

if __name__=="__main__":
    threading.Thread(target=start_web,daemon=True).start()
    print(f"[{timestamp()}] Bot starting...")
    asyncio.run(main())
    
