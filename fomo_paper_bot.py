"""
FOMO Paper Beast — paper copy trading bot
- Reálná data z FOMO API přes /v2/users/{id}/swaps
- Žádná simulace, vše z reálného API
"""

import os
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field, asdict

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ==================== KONFIGURACE ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
FOMO_API_KEY = os.getenv("FOMO_API_KEY", "")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
FOMO_API_BASE = "https://api.fomoapi.io/v2"

# Zpomaleno na 5 minut, aby FOMO API nezahlcovalo 503
POLL_INTERVAL = 300
# Prodleva mezi requesty na jednotlivé tradery (aby se nestřílely naráz)
DELAY_BETWEEN_TRADERS = 2.0
TOP_N = 3
WINDOWS = ["7d", "30d"]
WATCHLIST = ["brezscales", "orangie", "tjr", "unipcs", "pointfarmcap"]
STARTING_BALANCE = 1000.0
TRADE_SIZE_PCT = 0.05
SWAPS_PER_TRADER = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


SUBSCRIBERS: Set[int] = set()

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
    swap_id: str = ""

@dataclass
class PaperPortfolio:
    balance: float = STARTING_BALANCE
    positions: List[PaperPosition] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)
    total_pnl: float = 0.0

    def equity(self):
        return self.balance + sum(p.amount_usd for p in self.positions)

# ==================== FOMO API ====================
class FomoData:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=20.0)
        self.last_seen_swaps: Set[str] = set()

    async def get_top(self, window: str) -> List[Trader]:
        if not FOMO_API_KEY:
            return []
        try:
            r = await self.client.get(
                f"{FOMO_API_BASE}/leaderboard/{window}",
                params={"limit": TOP_N},
                headers={
                    "Accept": "application/json",
                    "authorization": f"Bearer {FOMO_API_KEY}",
                },
            )
            r.raise_for_status()
            data = r.json()
            traders = []
            for i, t in enumerate(data.get("traders", [])[:TOP_N]):
                traders.append(Trader(
                    handle=t.get("handle", "?"),
                    user_id=t.get("userId") or "",
                    rank=t.get("rank", i + 1),
                    window=window,
                    pnl_usd=float(t.get("pnlUsd") or 0),
                ))
            return traders
        except Exception as e:
            logger.warning(f"Leaderboard {window}: {e}")
            return []

    async def get_swaps_raw(self, identifier: str) -> Optional[dict]:
        """
        Vrátí raw JSON z /users/{id}/swaps nebo None při chybě.
        Loguje status i tělo odpovědi.
        """
        if not FOMO_API_KEY:
            return None
        try:
            r = await self.client.get(
                f"{FOMO_API_BASE}/users/{identifier}/swaps",
                params={"limit": SWAPS_PER_TRADER},
                headers={
                    "Accept": "application/json",
                    "authorization": f"Bearer {FOMO_API_KEY}",
                },
            )
            logger.info(
                f"SWAPS @{identifier}: status={r.status_code} "
                f"body={r.text[:1500]}"
            )
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            logger.warning(f"Swaps @{identifier} exception: {e}")
            return None

    async def get_swaps(self, trader: Trader) -> List[dict]:
        """Načte swapy a převede na interní formát."""
        identifier = trader.user_id or trader.handle
        if not identifier:
            return []

        data = await self.get_swaps_raw(identifier)
        if data is None:
            return []

        # Různé možné tvary odpovědi
        if isinstance(data, list):
            swaps_list = data
        elif isinstance(data, dict):
            swaps_list = (
                data.get("swaps")
                or data.get("fills")
                or data.get("trades")
                or data.get("data")
                or data.get("items")
                or []
            )
        else:
            swaps_list = []

        if not isinstance(swaps_list, list):
            logger.warning(f"Swaps @{trader.handle}: neočekávaný typ {type(swaps_list)}")
            return []

        new_swaps = []
        for raw in swaps_list:
            if not isinstance(raw, dict):
                continue

            swap_id = (
                raw.get("swapId") or raw.get("id") or raw.get("fillId")
                or raw.get("txHash") or raw.get("signature")
                or f"{trader.handle}_{raw.get('ts') or raw.get('timestamp') or ''}"
            )
            if swap_id in self.last_seen_swaps:
                continue

            side_raw = (
                raw.get("side") or raw.get("direction")
                or raw.get("type") or raw.get("action") or ""
            )
            side = str(side_raw).lower()
            if side not in ("buy", "sell"):
                self.last_seen_swaps.add(swap_id)
                continue

            token_raw = (
                raw.get("token") or raw.get("tokenSymbol")
                or raw.get("symbol") or raw.get("mint") or "?"
            )
            token = str(token_raw)
            if not token.startswith("$"):
                token = f"${token}"

            price = (
                raw.get("priceUsd") or raw.get("price")
                or raw.get("executionPrice") or 0.0
            )
            amount = (
                raw.get("amountUsd") or raw.get("sizeUsd")
                or raw.get("valueUsd") or raw.get("value") or 0.0
            )

            new_swaps.append({
                "trader": trader.handle,
                "window": trader.window,
                "symbol": token,
                "side": side,
                "price": float(price or 0),
                "size_usd": float(amount or 0),
                "time": str(raw.get("ts") or raw.get("timestamp") or ""),
                "swap_id": swap_id,
            })
            self.last_seen_swaps.add(swap_id)

        return new_swaps


class GrokClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    async def chat(self, user_msg: str, context: str = "") -> str:
        if not XAI_API_KEY:
            return "Grok není připojený (chybí XAI_API_KEY)."
        try:
            r = await self.client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "grok-4",
                    "messages": [
                        {"role": "system", "content": "Jsi Grok, asistent pro FOMO paper bot. Česky, krátce. Kontext: " + context},
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

# ==================== PAPER TRADING ====================
class PaperTrader:
    def __init__(self):
        self.pf = PaperPortfolio()
        self.active = True

    def copy(self, swap: dict) -> Optional[str]:
        if not self.active:
            return None

        symbol = swap["symbol"]
        side = swap["side"]
        price = swap["price"]
        trader = swap["trader"]
        window = swap.get("window", "")

        if side == "buy":
            api_size = swap.get("size_usd", 0)
            size = api_size if api_size > 0 else max(5.0, self.pf.balance * TRADE_SIZE_PCT)
            size = min(size, self.pf.balance * 0.25)

            if size < 5.0 or self.pf.balance < size:
                return None

            self.pf.positions.append(PaperPosition(
                symbol=symbol,
                amount_usd=size,
                entry_price=price,
                entry_time=swap.get("time", ""),
                source_trader=trader,
                swap_id=swap.get("swap_id", ""),
            ))
            self.pf.balance -= size
            return (
                f"🟢 BUY {symbol} ${size:.1f}\n"
                f"Od @{trader} ({window})\n"
                f"Cena: ${price:.6f}\n"
                f"Zůstatek: ${self.pf.balance:.0f}"
            )

        elif side == "sell":
            for i, p in enumerate(self.pf.positions):
                if p.symbol == symbol:
                    self.pf.positions.pop(i)
                    if p.entry_price > 0 and price > 0:
                        pnl = p.amount_usd * ((price / p.entry_price) - 1)
                    else:
                        pnl = 0.0
                    self.pf.balance += p.amount_usd + pnl
                    self.pf.total_pnl += pnl
                    return (
                        f"🔴 SELL {symbol} PnL ${pnl:+.1f}\n"
                        f"Od @{trader}\n"
                        f"Zůstatek: ${self.pf.balance:.0f}"
                    )
            return None
        return None

# ==================== INSTANCE ====================
fomo = FomoData()
paper = PaperTrader()
grok = GrokClient()

# ==================== TELEGRAM HANDLERY ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SUBSCRIBERS.add(update.effective_chat.id)
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
        "Příkazy:\n"
        "`/test <handle>` – zobrazí RAW swaps JSON z API\n"
        "`/grok <dotaz>` – chat s Grok\n\n"
        "Vyber:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manuální test: /test <handle> — fetchne swaps a pošle RAW JSON do Telegramu.
    """
    if not context.args:
        await update.message.reply_text(
            "Použití: `/test brezscales`", parse_mode="Markdown"
        )
        return
    handle = context.args[0].lstrip("@")
    await update.message.reply_text(f"Testuji @{handle}…")
    data = await fomo.get_swaps_raw(handle)
    if data is None:
        await update.message.reply_text(
            f"❌ API nevrátilo 200 pro @{handle}. "
            f"Podívej se do Render logu na řádek `SWAPS @{handle}`."
        )
        return
    text = f"✅ @{handle} — RAW JSON:\n```\n{str(data)[:3500]}\n```"
    await update.message.reply_text(text, parse_mode="Markdown")

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
            if traders:
                for t in traders:
                    lines.append(f"  #{t.rank} @{t.handle} +${t.pnl_usd:,.0f}")
            else:
                lines.append("  (žádná data)")
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

async def grok_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Použití: `/grok <dotaz>`", parse_mode="Markdown")
        return
    q = " ".join(context.args)
    ctx = f"Balance ${paper.pf.balance:.0f}, positions {len(paper.pf.positions)}, PnL ${paper.pf.total_pnl:+.0f}"
    reply = await grok.chat(q, ctx)
    await update.message.reply_text(reply)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Použij /start nebo tlačítka v menu.")

# ==================== BACKGROUND JOBS ====================
async def poll_loop(context: ContextTypes.DEFAULT_TYPE):
    traders: List[Trader] = []
    for w in WINDOWS:
        traders.extend(await fomo.get_top(w))
    for h in WATCHLIST:
        traders.append(Trader(handle=h, window="vip"))

    seen = set()
    unique_traders = []
    for t in traders:
        key = t.user_id or t.handle
        if key not in seen:
            seen.add(key)
            unique_traders.append(t)

    all_new_swaps = []
    for t in unique_traders:
        swaps = await fomo.get_swaps(t)
        all_new_swaps.extend(swaps)
        # Prodleva mezi requesty, aby FOMO nestřílel 503
        await asyncio.sleep(DELAY_BETWEEN_TRADERS)

    if all_new_swaps:
        logger.info(f"Zpracováno {len(all_new_swaps)} nových swapů.")
    else:
        logger.info(f"Žádné nové swapy (kontrolováno {len(unique_traders)} traderů).")

    for sw in all_new_swaps:
        msg = paper.copy(sw)
        if msg:
            paper.pf.history.append({
                "msg": msg,
                "time": utcnow().isoformat(),
                "swap_id": sw.get("swap_id", ""),
            })
            for chat_id in list(SUBSCRIBERS):
                try:
                    await context.bot.send_message(chat_id, msg)
                except Exception as e:
                    logger.warning(f"Odeslání selhalo pro {chat_id}: {e}")

async def self_ping_job(context: ContextTypes.DEFAULT_TYPE):
    """Self-ping, aby se free Render neuspával."""
    url = os.getenv("RENDER_EXTERNAL_URL", "https://fomo-paper-bot.onrender.com")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(url)
            logger.info("Self-ping OK")
    except Exception as e:
        logger.debug(f"Self-ping chyba: {e}")

async def post_init(app: Application):
    app.job_queue.run_repeating(poll_loop, interval=POLL_INTERVAL, first=15)
    app.job_queue.run_repeating(self_ping_job, interval=300, first=20)

# ==================== HEALTH CHECK ====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, fmt, *args):
        return

def start_health_server():
    port = int(os.getenv("PORT", 10000))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
    except OSError as e:
        logger.error(f"Health server chyba: {e}")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health check server běží na portu {port}")

# ==================== MAIN ====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Chybí TELEGRAM_BOT_TOKEN")
        return
    if not FOMO_API_KEY:
        print("Chybí FOMO_API_KEY")

    start_health_server()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(CommandHandler("grok", grok_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("FOMO Paper Beast běží.")
    app.run_polling()

if __name__ == "__main__":
    main()
