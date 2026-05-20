"""
Crypto OSINT Bot v6
- Без CSV файлов
- Баланс кошелька рядом с адресом
- Система ключей без лимита
"""

import logging, os, io, asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from searcher import extract_username, run_search, reverse_lookup, is_eth_address, get_variants
from access  import (generate_key, activate_key, check_access,
                     use_search, get_user_stats, list_keys,
                     revoke_key, block_user, unblock_user)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("7673817846:AAEispuEvUQ7yt4pPYZCLkcM2xCbpWj7zWU", "7673817846:AAEispuEvUQ7yt4pPYZCLkcM2xCbpWj7zWU")
ADMIN_ID  = int(os.getenv("6406599387", "6406599387"))


def esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ─── Форматирование ───────────────────────────────────────────────────────────

async def fmt_search(data: dict) -> str:
    u = data["username"]
    found = [r for r in data["results"] if r.get("found")]

    if not found:
        return (
            f"<b>@{esc(u)}</b>\n"
            f'<a href="https://x.com/{esc(u)}">Twitter</a>'
            f' · <a href="https://app.ethos.network/profile/x/{esc(u)}">Ethos</a>\n\n'
            f"ничего не найдено"
        )

    lines = []
    lines.append(f"<b>@{esc(u)}</b>")
    lines.append(
        f'<a href="https://x.com/{esc(u)}">Twitter</a>'
        f' · <a href="https://app.ethos.network/profile/x/{esc(u)}">Ethos</a>'
    )

    shown_wallets = set()
    for r in found:
        for w in (r.get("wallets") or [])[:3]:
            if w in shown_wallets:
                continue
            shown_wallets.add(w)
            lines.append("")
            lines.append(f"<code>{esc(w)}</code>")
            if w.startswith("0x"):
                lines.append(
                    f'<a href="https://platform.arkhamintelligence.com/explorer/address/{w}">Arkham</a>'
                    f' · <a href="https://zapper.xyz/account/{w}">Zapper</a>'
                    f' · <a href="https://debank.com/profile/{w}">DeBank</a>'
                )
            elif len(w) > 30:
                lines.append(f'<a href="https://solscan.io/account/{w}">Solscan</a>')

    if not shown_wallets:
        lines.append("\nкошельки не найдены")

    return "\n".join(lines)


async def fmt_reverse(data: dict) -> str:
    addr = data["address"]
    lines = [f"<code>{esc(addr)}</code>"]

    lines.append(
        f'<a href="https://platform.arkhamintelligence.com/explorer/address/{addr}">Arkham</a>'
        f' · <a href="https://zapper.xyz/account/{addr}">Zapper</a>'
        f' · <a href="https://debank.com/profile/{addr}">DeBank</a>'
    )

    if not data["results"]:
        lines.append("\nники не найдены")
        return "\n".join(lines)

    lines.append("")
    for r in data["results"]:
        handles = r.get("handles") or []
        for h in handles[:2]:
            clean_h = h.replace("lens/@", "")
            lines.append(f"<b>@{esc(clean_h)}</b>")
            lines.append(
                f'<a href="https://x.com/{esc(clean_h)}">Twitter</a>'
                f' · <a href="https://www.ethos.network/profile/{esc(clean_h)}">Ethos</a>'
                f' · <a href="{esc(r.get("url",""))}">профиль</a>'
            )

    return "\n".join(lines)


def wallet_buttons(wallets):
    if not wallets:
        return None
    btns = []
    for w in wallets[:2]:
        short = f"{w[:6]}...{w[-4:]}"
        btns.append([InlineKeyboardButton(
            f"⚡ Zapper {short}", url=f"https://zapper.xyz/account/{w}")])
        if w.startswith("0x"):
            btns.append([
                InlineKeyboardButton("Etherscan", url=f"https://etherscan.io/address/{w}"),
                InlineKeyboardButton("DeBank",    url=f"https://debank.com/profile/{w}"),
            ])
    return InlineKeyboardMarkup(btns)


# ─── КОМАНДЫ ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.username or str(uid)

    if ctx.args:
        key = ctx.args[0]
        ok, msg = activate_key(uid, uname, key)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {esc(msg)}", parse_mode=ParseMode.HTML)
        if ok and uid != ADMIN_ID:
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"🔑 Новый пользователь:\n@{esc(uname)} (ID: <code>{uid}</code>)\nКлюч: <code>{esc(key)}</code>",
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    if uid == ADMIN_ID:
        await update.message.reply_text(
            "<b>👑 Режим администратора</b>\n\n"
            "/genkey — создать ключ\n"
            "/keys — список всех ключей\n"
            "/revoke КЛЮЧ — заблокировать ключ\n"
            "/block ID — заблокировать юзера\n"
            "/unblock ID — разблокировать юзера\n\n"
            "Отправь @username или 0x... для поиска.",
            parse_mode=ParseMode.HTML)
        return

    ok, reason = check_access(uid)
    if not ok:
        msgs = {
            "no_access":   "Нет доступа.\nПолучи ключ у администратора и введи:\n<code>/start OSINT-XXXXXXXXXXXX</code>",
            "blocked":     "Твой доступ заблокирован.",
            "key_revoked": "Твой ключ был отозван.",
        }
        await update.message.reply_text(
            f"🔒 {msgs.get(reason,'Нет доступа.')}", parse_mode=ParseMode.HTML)
        return

    stats = get_user_stats(uid)
    await update.message.reply_text(
        "<b>✅ Crypto OSINT Bot</b>\n\n"
        "Отправь ник:\n"
        "<code>@username</code>\n"
        "<code>https://x.com/username</code>\n"
        "<code>https://t.me/username</code>\n"
        "<code>nickname.eth</code>\n\n"
        "Или адрес кошелька:\n"
        "<code>0x1234...abcd</code>  → найдёт ники + покажет баланс\n\n"
        f"Поисков сделано: <b>{stats.get('searches',0)}</b>",
        parse_mode=ParseMode.HTML)


async def cmd_genkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    key = generate_key()
    await update.message.reply_text(
        f"🔑 <b>Новый ключ:</b>\n\n<code>{key}</code>\n\n"
        f"Отправь пользователю — пусть введёт в боте:\n<code>/start {key}</code>",
        parse_mode=ParseMode.HTML)


async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    keys = list_keys()
    if not keys:
        await update.message.reply_text("Ключей нет. /genkey")
        return
    lines = ["<b>🗝 Ключи:</b>\n"]
    for k in keys:
        icon = "✅" if k["active"] else "❌"
        lines.append(
            f"{icon} <code>{esc(k['key'])}</code>\n"
            f"   {esc(k['user'])} · поисков: {k['searches']}\n")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ключ: /revoke OSINT-XXXXXXXXXXXX")
        return
    ok = revoke_key(ctx.args[0])
    await update.message.reply_text("✅ Ключ заблокирован." if ok else "❌ Ключ не найден.")


async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /block 123456789")
        return
    ok = block_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Заблокирован." if ok else "❌ Не найден.")


async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /unblock 123456789")
        return
    ok = unblock_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Разблокирован." if ok else "❌ Не найден или ключ отозван.")


# ─── ОСНОВНОЙ HANDLER ─────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw = update.message.text.strip()
    if raw.startswith("/"):
        return

    if uid != ADMIN_ID:
        ok, reason = check_access(uid)
        if not ok:
            msgs = {
                "no_access":   "Нет доступа. Напиши /start",
                "blocked":     "Доступ заблокирован.",
                "key_revoked": "Ключ отозван.",
            }
            await update.message.reply_text(msgs.get(reason, "Нет доступа."))
            return

    # ── Обратный поиск по адресу ─────────────────────────────────────────────
    if is_eth_address(raw):
        msg = await update.message.reply_text(
            f"🔄 Ищу по адресу <code>{esc(raw[:10])}...</code>",
            parse_mode=ParseMode.HTML)
        try:
            data = await reverse_lookup(raw)
            text = await fmt_reverse(data)
            if len(text) > 4000:
                text = text[:3900] + "\n<i>...обрезано</i>"
            await msg.edit_text(
                text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            log.error(e, exc_info=True)
            await msg.edit_text(
                f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>", parse_mode=ParseMode.HTML)
        return

    # ── Поиск по нику ─────────────────────────────────────────────────────────
    username = extract_username(raw)
    if not username:
        await update.message.reply_text(
            "Не понял. Отправь:\n<code>@username</code>  или  <code>0x...</code>",
            parse_mode=ParseMode.HTML)
        return

    variants = get_variants(username)
    dom_prev = "  ".join(variants["domains"][:8])
    msg = await update.message.reply_text(
        f"🔍 Ищу <code>@{esc(username)}</code>\n"
        f"<i>Доменов: {len(variants['domains'])} — {esc(dom_prev)}...</i>",
        parse_mode=ParseMode.HTML)

    try:
        data = await run_search(username)
        if uid != ADMIN_ID:
            use_search(uid)

        text    = await fmt_search(data)
        buttons = wallet_buttons(data["all_wallets"])

        if len(text) > 4000:
            text = text[:3900] + "\n<i>...обрезано</i>"

        await msg.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=buttons, disable_web_page_preview=True)

    except Exception as e:
        log.error(e, exc_info=True)
        await msg.edit_text(
            f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>", parse_mode=ParseMode.HTML)


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("ОШИБКА: вставь токен от @BotFather в bot.py")
        return
    if ADMIN_ID == 0:
        print("ОШИБКА: вставь свой Telegram ID (узнать — @userinfobot)")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(CommandHandler("genkey",  cmd_genkey))
    app.add_handler(CommandHandler("keys",    cmd_keys))
    app.add_handler(CommandHandler("revoke",  cmd_revoke))
    app.add_handler(CommandHandler("block",   cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print(f"✅ Бот запущен. Админ ID: {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
