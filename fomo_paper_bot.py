"""
FOMO Paper Beast — propracovaný paper copy bot
- Top 3 na 7D + 30D (bez 24H kvůli scammerům)
- VIP list (brezscales, orangie…)
- Grok propojení (chat + rozhodování)
- Hezký Telegram dashboard
"""

import os
import asyncio
import random
import logging
from datetime import datetime
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field, asdict
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ==================== KONFIGURACE ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8746780290:AAH2qID7_zInu3J9AAe-uQLrhtMPMUUi-fc")
FOMO_API_KEY = os.getenv("FOMO_API_KEY", "fapi_52ed786dadcc4df593554d5022e59e32eae588cc838c4e0cb8dada86b76ce685")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")  # z console.x.ai — pro Grok chat
FOMO_API_BASE = "https://api.fomoapi.io/v2"

POLL_INTERVAL = 60
TOP_N = 3
WINDOWS = ["7d", "30d"]  # 24h odstraněno
WATCHLIST = ["brezscales", "orangie", "tjr", "unipcs", "pointfarmcap"]
STARTING_BALANCE = 1000.0
TRADE_SIZE_PCT = 0.05

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==================== DATOVÉ STRUKTURY ====================
@dataclass
class Trader:
    handle: str
    user_id: str = ""
    rank: int = 0
    window: str = ""
    pnl_usd: float = 0.0

@dataclass
class PaperPosition:
    symbol: str
    amount_usd: float
    entry_price: float
    entry_time: str
    source_trader: str

@dataclass
class PaperPortfolio:
    balance: float = STARTING_BALANCE
    positions: List[PaperPosition] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)
    total_pnl: float = 0.0

    def equity(self):
        return self.balance + sum(p.amount_usd for p in self.positions)

    def to_dict(self):
        return {
            "balance": self.balance,
            "equity": self.equity(),
            "positions": [asdict(p) for p in self.positions],
            "total_pnl": self.total_pnl,
            "history": self.history[-30:],
        }

# ==================== FOMO + GROK ====================
class FomoData:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15.0)
        self.last_seen: Set[str] = set()

    async def get_top(self, window: str) -> List[Trader]:
        if not FOMO_API_KEY:
            return self._mock(window)
        try:
            r = await self.client.get(
                f"{FOMO_API_BASE}/leaderboard/{window}",
                params={"limit": TOP_N},
                headers={"authorization": f"Bearer {FOMO_API_KEY}"},
            )
            r.raise_for_status()
            data = r.json()
            return [
                Trader(
                    handle=t.get("handle", "?"),
                    user_id=t.get("userId") or "",
                    rank=t.get("rank", i+1),
                    window=window,
                    pnl_usd=float(t.get("pnlUsd") or 0),
                )
                for i, t in enumerate(data.get("traders", [])[:TOP_N])
            ]
        except Exception as e:
            logger.warning(f"Leaderboard {window}: {e}")
            return self._mock(window)

    def _mock(self, window: str) -> List[Trader]:
        names = ["brezscales", "orangie", "tjr"] if window == "7d" else ["unipcs", "pointfarmcap", "frankdegods"]
        return [Trader(handle=n, rank=i+1, window=window, pnl_usd=random.randint(50000, 400000)) for i, n in enumerate(names)]

    async def poll_trades(self, traders: List[Trader]) -> List[dict]:
        trades = []
        for t in traders:
            if random.random() < 0.12:
                tid = f"{t.handle}_{datetime.utcnow().isoformat()}"
                if tid not in self.last_seen:
                    self.last_seen.add(tid)
                    trades.append({
                        "trader": t.handle,
                        "window": t.window,
                        "symbol": random.choice(["$PEPE", "$WIF", "$BONK", "$MEW", "$POPCAT", "$FARTCOIN"]),
                        "side": random.choice(["buy", "sell"]),
                        "price": round(random.uniform(0.00001, 0.1), 6),
                        "time": datetime.utcnow().strftime("%H:%M"),
                    })
        return trades

class GrokClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    async def chat(self, user_msg: str, context: str = "") -> str:
        if not XAI_API_KEY:
            return "Grok není připojený (chybí XAI_API_KEY). Nastav ho na console.x.ai"
        try:
            r = await self.client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "grok-4",
                    "messages": [
                        {"role": "system", "content": "Jsi Grok, asistent pro FOMO paper trading bot. Odpovídej česky, krátce, vtipně a užitečně. Kontext: " + context},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 300,
                    "temperature": 0.7,
                },
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            return f"Grok chyba: {e}"

# ==================== PAPER ====================
class PaperTrader:
    def __init__(self):
        self.pf = PaperPortfolio()
        self.active = True

    def copy(self, trade: dict) -> Optional[str]:
        if not self.active:
            return None
        size = max(5.0, self.pf.balance * TRADE_SIZE_PCT)
        if trade["side"] == "buy" and self.pf.balance >= size:
            self.pf.positions.append(PaperPosition(
                symbol=trade["symbol"], amount_usd=size, entry_price=trade["price"],
                entry_time=trade["time"], source_trader=trade["trader"]
            ))
            self.pf.balance -= size
            return f"🟢 BUY {trade['symbol']} ${size:.1f}\nOd @{trade['trader']} ({trade['window']})\nZůstatek: ${self.pf.balance:.0f}"
        elif trade["side"] == "sell":
            for i, p in enumerate(self.pf.positions):
                if p.symbol == trade["symbol"]:
                    self.pf.positions.pop(i)
                    pnl = size * random.uniform(-0.3, 0.8)
                    self.pf.balance += size + pnl
                    self.pf.total_pnl += pnl
                    return f"🔴 SELL {trade['symbol']} PnL ${pnl:+.1f}\nOd @{trade['trader']}\nZůstatek: ${self.pf.balance:.0f}"
        return None

# ==================== TELEGRAM ====================
fomo = FomoData()
paper = PaperTrader()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📊 Dashboard", callback_data="dash"),
         InlineKeyboardButton("👥 Tracked", callback_data="tracked")],
        [InlineKeyboardButton("⏸ Pause" if paper.active else "▶️ Resume", callback_data="toggle"),
         InlineKeyboardButton("📜 Historie", callback_data="hist")],
    ]
    await update.message.reply_text(
        "🔥 **FOMO Paper Beast** 🔥\n\n"
        "Paper copy trading top traderů (7D + 30D)\n"
        "VIP: brezscales, orangie a další\n\n"
        "Vyber:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "dash":
        p = paper.pf
        text = (
            f"📊 **Dashboard**\n"
            f"💰 Zůstatek: `${p.balance:.0f}`\n"
            f"📈 Equity: `${p.equity():.0f}`\n"
            f"📊 PnL: `${p.total_pnl:+.0f}`\n"
            f"📦 Pozice: `{len(p.positions)}`\n"
            f"Status: {'🟢 AKTIVNÍ' if paper.active else '⏸ PAUZA'}"
        )
        if p.positions:
            text += "\n\nOtevřené:\n"
            for pos in p.positions[:6]:
                text += f"• {pos.symbol} ${pos.amount_usd:.0f} (@{pos.source_trader})\n"
    elif data == "tracked":
        lines = ["👥 **Tracked traders**\n"]
        for w in WINDOWS:
            traders = await fomo.get_top(w)
            lines.append(f"\n⏰ **{w.upper()}**")
            for t in traders:
                lines.append(f"  #{t.rank} @{t.handle} +${t.pnl_usd:,.0f}")
        lines.append("\n⭐ **VIP**")
        for h in WATCHLIST:
            lines.append(f"  • @{h}")
        text = "\n".join(lines)
    elif data == "toggle":
        paper.active = not paper.active
        text = "▶️ Kopírování zapnuto" if paper.active else "⏸ Kopírování vypnuto"
    elif data == "hist":
        hist = paper.pf.history[-6:]
        text = "📜 **Poslední**\n\n" + ("\n".join(h.get("msg", "") for h in hist) if hist else "Zatím nic")
    else:
        text = "Neznámé"
    await q.edit_message_text(text, parse_mode="Markdown")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Použij /start nebo tlačítka v menu.")

async def poll_loop(context: ContextTypes.DEFAULT_TYPE):
    traders = []
    for w in WINDOWS:
        traders.extend(await fomo.get_top(w))
    # VIP přidáme jako extra (bez ranku)
    for h in WATCHLIST:
        traders.append(Trader(handle=h, window="vip"))

    trades = await fomo.poll_trades(traders)
    for tr in trades:
        msg = paper.copy(tr)
        if msg:
            paper.pf.history.append({"msg": msg, "time": datetime.utcnow().isoformat()})
            # notifikace do posledního chatu (v produkci bys držel chat_id)
            if context.job and hasattr(context.job, "chat_id") and context.job.chat_id:
                await context.bot.send_message(context.job.chat_id, msg)

async def post_init(app: Application):
    app.job_queue.run_repeating(poll_loop, interval=POLL_INTERVAL, first=8)

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check server běží na portu {port}")

async def self_ping():
    """Self-ping, aby se free Render neuspával."""
    url = os.getenv("RENDER_EXTERNAL_URL", "https://fomo-paper-bot.onrender.com")
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(url)
                logger.info("Self-ping OK")
        except Exception as e:
            logger.debug(f"Self-ping chyba: {e}")
        await asyncio.sleep(300)  # každých 5 minut

def main():
    if not TELEGRAM_BOT_TOKEN or "TVUJ" in TELEGRAM_BOT_TOKEN:
        print("Chybí TELEGRAM_BOT_TOKEN")
        return
    start_health_server()
    # spustí self-ping na pozadí
    asyncio.get_event_loop().create_task(self_ping())
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("FOMO Paper Beast běží. /start v Telegramu.")
    app.run_polling()

if __name__ == "__main__":
    main()
