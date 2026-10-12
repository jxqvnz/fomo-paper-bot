# FOMO Paper Beast Bot

Paper copy trading bot pro FOMO (24h/7d/30d + VIP list).

## Nasazení na Render (zdarma, 24/7)

1. Nahraj tyto soubory na GitHub (nové veřejné repo)
2. Jdi na https://render.com → Sign up
3. New → Blueprint → připoj repo
4. Nebo New → Web Service a nastav:
   - Build: `pip install -r requirements.txt`
   - Start: `python fomo_paper_bot.py`
5. V Environment Variables přidej:
   - `TELEGRAM_BOT_TOKEN` = tvůj token z BotFather
   - `FOMO_API_KEY` = tvůj klíč z fomoapi.io
6. Deploy

Bot poběží sám. Notifikace chodí do Telegramu i když jsi offline.

## Lokální spuštění
```bash
pip install -r requirements.txt
python fomo_paper_bot.py
```
