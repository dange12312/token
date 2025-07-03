import os
import json
import asyncio
import requests
import websockets
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
import uvicorn
import threading

# ─── Configuration ──────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8015586375:AAE9RwP1Lzqqob0yJt5DxcidgAlW8LpsYp4")
USER_ID = os.getenv("USER_ID", "7683338204")
RPC_WS = "wss://api.mainnet-beta.solana.com/"
RPC_HTTP = "https://api.mainnet-beta.solana.com"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
USDC_THRESHOLD = 100 * 10**6  # 100 USDC
WINDOW_MINUTES = 5
MAIN_WALLETS = [
    "dUJNHh9Nm9rsn7ykTViG7N7BJuaoJJD9H635B8BVifa",
    "9B1fR2Z38ggjqmFuhYBEsa7fXaBR1dkC7BamixjmWZb4"
]

# ─── State ───────────────────────────────────────────────────────────────
watched_wallets = {}       # pubkey -> expiry datetime
seen_tokens = set()        # dedupe new token CAs

# ─── FastAPI ─────────────────────────────────────────────────────────────
app = FastAPI()
@app.get("/")
def root():
    return {"status": "ok"}

# ─── Helpers ─────────────────────────────────────────────────────────────
def timestamp():
    return datetime.now(timezone.utc).isoformat()

def notify(msg: str):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": USER_ID, "text": msg, "parse_mode": "Markdown"}
    )

def get_owner(ata: str) -> str:
    body = {"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":[ata,{"encoding":"jsonParsed"}]}
    try:
        resp = requests.post(RPC_HTTP, json=body, timeout=5).json()
        return resp.get("result", {}).get("value", {}).get("data", {}).get("parsed", {}).get("info", {}).get("owner")
    except:
        return None

# ─── Track Wallet B ──────────────────────────────────────────────────────
async def track_wallet_b(wallet: str):
    if wallet in watched_wallets:
        return
    watched_wallets[wallet] = datetime.now(timezone.utc) + timedelta(minutes=WINDOW_MINUTES)
    notify(f"🔔 Now watching `{wallet}` for token buys (next {WINDOW_MINUTES} min)")

# ─── Listen USDC Outflow via logsSubscribe ────────────────────────────────
async def listen_usdc_outflows():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        # subscribe logs for USDC transfer events mentioning each main wallet
        for wallet in MAIN_WALLETS:
            await ws.send(json.dumps({
                "jsonrpc":"2.0","id":1,
                "method":"logsSubscribe",
                "params":[{"mentions":[wallet]},{"commitment":"confirmed"}]
            }))
            await ws.recv()
        notify(f"🤖 Monitoring {len(MAIN_WALLETS)} main wallets for USDC outflows")
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("method") != "logsNotification":
                    continue
                sig = msg["params"]["result"]["value"]["signature"]
                resp = requests.post(RPC_HTTP, json={
                    "jsonrpc":"2.0","id":1,
                    "method":"getTransaction",
                    "params":[sig, {"encoding":"json","maxSupportedTransactionVersion":0}]
                }, timeout=5).json()
                tx = resp.get("result")
                if not tx:
                    continue
                for inner in (tx.get("meta") or {}).get("innerInstructions", []):
                    for ix in inner.get("instructions", []):
                        if ix.get("programId") != TOKEN_PROGRAM_ID:
                            continue
                        parsed = ix.get("parsed", {})
                        if parsed.get("type") != "transfer":
                            continue
                        info = parsed.get("info", {})
                        source = info.get("source")
                        dest = info.get("destination")
                        amt = int(info.get("amount", 0))
                        mint = info.get("mint")
                        if mint == USDC_MINT and amt >= USDC_THRESHOLD:
                            usdc_amt = amt / 10**6
                            notify(f"🚨 *{usdc_amt:.2f} USDC* outflow to `{dest}`")
                            owner = get_owner(dest) or dest
                            await track_wallet_b(owner)
            except Exception as e:
                print(f"[{timestamp()}] Error in outflows listener: {e}")

# ─── Listen SPL Buys for Wallet B ─────────────────────────────────────────
async def listen_wallet_b_buys():
    async with websockets.connect(RPC_WS, ping_interval=30) as ws:
        # initially subscribe logs for any future wallet B
        while True:
            await asyncio.sleep(1)
            now = datetime.now(timezone.utc)
            to_remove = []
            # subscribe logs and handle buys
            for wallet, expiry in watched_wallets.items():
                if now > expiry:
                    to_remove.append(wallet)
                    continue
                # check logs events
                await ws.send(json.dumps({
                    "jsonrpc":"2.0","id":1,
                    "method":"logsSubscribe",
                    "params":[{"mentions":[wallet]}, {"commitment":"confirmed"}]
                }))
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("method") != "logsNotification":
                    continue
                sig = msg["params"]["result"]["value"]["signature"]
                tx = requests.post(RPC_HTTP, json={
                    "jsonrpc":"2.0","id":1,
                    "method":"getTransaction",
                    "params":[sig, {"encoding":"json","maxSupportedTransactionVersion":0}]
                }).json().get("result", {})
                for inner in tx.get("meta", {}).get("innerInstructions", []):
                    for ix in inner.get("instructions", []):
                        if ix.get("programId") != TOKEN_PROGRAM_ID:
                            continue
                        parsed = ix.get("parsed", {})
                        if parsed.get("type") != "transfer":
                            continue
                        info = parsed.get("info", {})
                        mint = info.get("mint")
                        owner_dest = get_owner(info.get("destination")) or info.get("destination")
                        if (mint and mint != USDC_MINT and mint not in seen_tokens and owner_dest == wallet):
                            seen_tokens.add(mint)
                            notify(f"✨ New token acquired! [`{mint}`](https://solscan.io/token/{mint}) by `{wallet}`")
            for w in to_remove:
                watched_wallets.pop(w, None)
                notify(f"🔕 Stopped watching `{w}` after window expired.")

# ─── Main Runner ─────────────────────────────────────────────────────────
async def main():
    await asyncio.gather(listen_usdc_outflows(), listen_wallet_b_buys())

# ─── Deploy ──────────────────────────────────────────────────────────────
def start_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

if __name__ == "__main__":
    threading.Thread(target=start_fastapi, daemon=True).start()
    print(f"[{timestamp()}] Bot starting…")
    asyncio.run(main())
    
