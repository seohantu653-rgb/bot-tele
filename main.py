import asyncio
import aiohttp
import json
import time
from pathlib import Path
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from flask import Flask
from threading import Thread

# ---------------- CONFIG ----------------
DOMAINS_FILE = "domains.json"
LOG_FILE = "domains_log.json"
CHECK_INTERVAL = 20  # detik
LATENCY_THRESHOLD_MS = 2000
CONSECUTIVE_FAIL_TRIGGER = 3

# Ganti sesuai bot Anda
TELEGRAM_BOT_TOKEN = "7958579410:AAF6lt7kb6hh45t1nMgatBuMZnoVoQZiJas"
TELEGRAM_GROUP_ID = 4731460922
# ---------------- END CONFIG ----------------

# ---------- Flask web server untuk ping Replit ----------
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask).start()

# ---------- Load / Save domains & log ----------
def load_domains():
    if Path(DOMAINS_FILE).exists():
        return json.loads(Path(DOMAINS_FILE).read_text())["domains"]
    return []

def save_domains(domains):
    Path(DOMAINS_FILE).write_text(json.dumps({"domains": domains}, indent=2))

def load_log():
    if Path(LOG_FILE).exists():
        return json.loads(Path(LOG_FILE).read_text())
    return {}

def save_log(log_data):
    Path(LOG_FILE).write_text(json.dumps(log_data, indent=2))

domains = load_domains()
domain_states = {d: {"last_status": None, "last_latency": None, "fail_count": 0} for d in domains}
log_history = load_log()

# ---------- Telegram Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Premium Monitor Bot aktif. Gunakan /help untuk daftar perintah.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/list - daftar domain\n"
        "/add <domain> - tambah domain\n"
        "/delete <domain> - hapus domain\n"
        "/status - status terakhir domain\n"
        "/check - paksa pengecekan sekarang\n"
        "/history <domain> - lihat 10 terakhir status & latency\n"
        "/summary - ringkasan uptime & latency semua domain\n"
        "/reset <domain> - reset fail count & history domain"
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "Domains:\n" + "\n".join(domains) if domains else "Belum ada domain terdaftar."
    await update.message.reply_text(msg)

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan /add <domain>")
        return
    domain = context.args[0]
    if domain not in domains:
        domains.append(domain)
        save_domains(domains)
        domain_states[domain] = {"last_status": None, "last_latency": None, "fail_count": 0}
        log_history[domain] = log_history.get(domain, [])
        save_log(log_history)
        await update.message.reply_text(f"{domain} ditambahkan dan mulai dipantau.")
    else:
        await update.message.reply_text(f"{domain} sudah ada dalam daftar.")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan /delete <domain>")
        return
    domain = context.args[0]
    if domain in domains:
        domains.remove(domain)
        save_domains(domains)
        domain_states.pop(domain, None)
        log_history.pop(domain, None)
        save_log(log_history)
        await update.message.reply_text(f"{domain} dihapus dari pemantauan.")
    else:
        await update.message.reply_text(f"{domain} tidak ditemukan dalam daftar.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msgs = []
    for d in domains:
        s = domain_states[d]
        msgs.append(f"{d} -> status:{s['last_status']} latency:{s['last_latency']}ms fails:{s['fail_count']}")
    await update.message.reply_text("\n".join(msgs) if msgs else "Belum ada domain terdaftar.")

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Melakukan pengecekan sekarang...")
    await run_checks(send_alert=True)

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan /history <domain>")
        return
    domain = context.args[0]
    if domain in log_history:
        history = log_history[domain][-10:]
        msg = "\n".join([f"{h['time']} -> status:{h['status']} latency:{h['latency']}ms" for h in history])
        await update.message.reply_text(msg or "Belum ada history.")
    else:
        await update.message.reply_text(f"{domain} tidak ditemukan.")

async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msgs = []
    for d in domains:
        logs = log_history.get(d, [])
        if logs:
            total = len(logs)
            up = sum(1 for l in logs if 200 <= (l['status'] or 0) < 400)
            avg_latency = sum((l['latency'] or 0) for l in logs)/total
            max_latency = max((l['latency'] or 0) for l in logs)
            msgs.append(f"{d}: Uptime={up}/{total} ({up/total*100:.1f}%), Avg Latency={avg_latency:.0f}ms, Max Latency={max_latency}ms")
        else:
            msgs.append(f"{d}: Belum ada data")
    await update.message.reply_text("\n".join(msgs))

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan /reset <domain>")
        return
    domain = context.args[0]
    if domain in domain_states:
        domain_states[domain]["fail_count"] = 0
        log_history[domain] = []
        save_log(log_history)
        await update.message.reply_text(f"{domain} berhasil di-reset.")
    else:
        await update.message.reply_text(f"{domain} tidak ditemukan.")

# ---------- Monitoring Core ----------
async def check_domain(session, domain):
    t0 = time.time()
    try:
        async with session.get(domain, timeout=10) as resp:
            latency = int((time.time() - t0) * 1000)
            status = resp.status
            ok = 200 <= status < 400
    except Exception:
        latency = None
        status = None
        ok = False

    state = domain_states[domain]
    state["last_status"] = status
    state["last_latency"] = latency
    state["fail_count"] = state.get("fail_count", 0) + (0 if ok else 1)

    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": status, "latency": latency}
    log_history.setdefault(domain, []).append(entry)
    if len(log_history[domain]) > 100:
        log_history[domain] = log_history[domain][-100:]
    save_log(log_history)

    return domain, ok, status, latency

async def run_checks(send_alert=False):
    if not domains:
        return
    async with aiohttp.ClientSession() as session:
        tasks = [check_domain(session, d) for d in domains]
        results = await asyncio.gather(*tasks)
    for domain, ok, status, latency in results:
        state = domain_states[domain]
        alert_msg = None
        if not ok or (latency and latency > LATENCY_THRESHOLD_MS) or state["fail_count"] >= CONSECUTIVE_FAIL_TRIGGER:
            prefix = "⚠️ ALERT" if not ok or state["fail_count"] >= CONSECUTIVE_FAIL_TRIGGER else "⚠️ HIGH LATENCY"
            alert_msg = f"{prefix} {domain} -> status:{status} latency:{latency}ms fails:{state['fail_count']}"
        if send_alert and alert_msg:
            await send_telegram(alert_msg)
        if ok and state["fail_count"] >= CONSECUTIVE_FAIL_TRIGGER:
            state["fail_count"] = 0
            await send_telegram(f"✅ {domain} pulih kembali. status:{status} latency:{latency}ms")
    return results

async def send_telegram(msg):
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_GROUP_ID, "text": msg}
        try:
            await session.post(url, json=payload, timeout=10)
        except:
            pass

async def monitor_loop():
    while True:
        await run_checks(send_alert=True)
        await asyncio.sleep(CHECK_INTERVAL)

# ---------- Main ----------
async def main():
    app_bot = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("help", help_cmd))
    app_bot.add_handler(CommandHandler("list", list_cmd))
    app_bot.add_handler(CommandHandler("add", add_cmd))
    app_bot.add_handler(CommandHandler("delete", delete_cmd))
    app_bot.add_handler(CommandHandler("status", status_cmd))
    app_bot.add_handler(CommandHandler("check", check_cmd))
    app_bot.add_handler(CommandHandler("history", history_cmd))
    app_bot.add_handler(CommandHandler("summary", summary_cmd))
    app_bot.add_handler(CommandHandler("reset", reset_cmd))

    asyncio.create_task(monitor_loop())
    await app_bot.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
