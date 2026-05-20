"""
Crypto OSINT Bot — всё через инлайн кнопки
"""

import logging, os, asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode

from searcher import extract_username, run_search, reverse_lookup, is_eth_address, get_variants
from database import init_db
from access  import (generate_key, activate_key, check_access,
                     use_search, get_user_stats, list_keys,
                     revoke_key, block_user, unblock_user,
                     generate_ref, activate_ref, get_referrals, list_all_refs)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "ВСТАВЬ_СВОЙ_ID_СЮДА"))


def esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Поиск по нику",    callback_data="help_search")],
        [InlineKeyboardButton("🔗 Моя реф-ссылка",  callback_data="myref")],
        [InlineKeyboardButton("📊 Статистика",       callback_data="stats")],
    ])

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Создать ключ",     callback_data="genkey")],
        [InlineKeyboardButton("🗝 Все ключи",        callback_data="keys")],
        [InlineKeyboardButton("👥 Все рефералы",     callback_data="refs")],
        [InlineKeyboardButton("🔍 Поиск по нику",    callback_data="help_search")],
    ])

def kb_wallet(wallets: list):
    if not wallets:
        return None
    btns = []
    for w in wallets[:2]:
        short = f"{w[:6]}...{w[-4:]}"
        btns.append([InlineKeyboardButton(f"⚡ Zapper {short}", url=f"https://zapper.xyz/account/{w}")])
        if w.startswith("0x"):
            btns.append([
                InlineKeyboardButton("Etherscan", url=f"https://etherscan.io/address/{w}"),
                InlineKeyboardButton("DeBank",    url=f"https://debank.com/profile/{w}"),
            ])
    return InlineKeyboardMarkup(btns)

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data="back_main")]])


# ─── Форматирование результата поиска ─────────────────────────────────────────

async def fmt_search(data: dict) -> str:
    u     = data["username"]
    found = [r for r in data["results"] if r.get("found")]

    lines = [f"<b>@{esc(u)}</b>"]
    lines.append(
        f'<a href="https://x.com/{esc(u)}">Twitter</a>'
        f' · <a href="https://app.ethos.network/profile/x/{esc(u)}">Ethos</a>'
        f' · <a href="https://platform.arkhamintelligence.com/explorer/entities?q={esc(u)}">Arkham</a>'
    )

    if not found:
        lines.append("\nничего не найдено")
        return "\n".join(lines)

    shown = set()
    for r in found:
        for w in (r.get("wallets") or [])[:3]:
            if w in shown:
                continue
            shown.add(w)
            lines.append(f"\n<code>{esc(w)}</code>")
            if w.startswith("0x"):
                lines.append(
                    f'<a href="https://platform.arkhamintelligence.com/explorer/address/{w}">Arkham</a>'
                    f' · <a href="https://zapper.xyz/account/{w}">Zapper</a>'
                    f' · <a href="https://debank.com/profile/{w}">DeBank</a>'
                )
            elif len(w) > 30:
                lines.append(f'<a href="https://solscan.io/account/{w}">Solscan</a>')

    if not shown:
        lines.append("\nпрофили найдены, кошельки не раскрыты публично")

    return "\n".join(lines)


async def fmt_reverse(data: dict) -> str:
    addr  = data["address"]
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
        for h in (r.get("handles") or [])[:2]:
            h = h.replace("lens/@", "")
            lines.append(f"<b>@{esc(h)}</b>")
            lines.append(
                f'<a href="https://x.com/{esc(h)}">Twitter</a>'
                f' · <a href="https://app.ethos.network/profile/x/{esc(h)}">Ethos</a>'
            )
    return "\n".join(lines)


# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.username or str(uid)

    # /start REF... — активация по реф-коду
    if ctx.args and ctx.args[0].startswith("REF"):
        arg = ctx.args[0]
        ok, msg = await activate_ref(uid, uname, arg)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {esc(msg)}\n\n"
            + ("Теперь можешь использовать бота 👇" if ok else ""),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main() if ok else None
        )
        if ok:
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"👥 Новый пользователь по реф-ссылке:\n"
                    f"@{esc(uname)} (ID: <code>{uid}</code>)\n"
                    f"Реф-код: <code>{esc(arg)}</code>",
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    # /start OSINT-... — активация по ключу
    if ctx.args and ctx.args[0].startswith("OSINT-"):
        arg = ctx.args[0]
        ok, msg = await activate_key(uid, uname, arg)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {esc(msg)}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main() if ok else None
        )
        if ok:
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"🔑 Новый пользователь по ключу:\n"
                    f"@{esc(uname)} (ID: <code>{uid}</code>)\n"
                    f"Ключ: <code>{esc(arg)}</code>",
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    # Админ
    if uid == ADMIN_ID:
        await update.message.reply_text(
            "<b>👑 Панель администратора</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_admin()
        )
        return

    # Проверка доступа
    ok, reason = await check_access(uid)
    if not ok:
        msgs = {
            "no_access":   "🔒 Нет доступа.\n\nПолучи ключ у администратора и введи его:\n<code>/start OSINT-XXXXXXXXXXXX</code>\n\nИли попроси реф-ссылку у кого-то кто уже пользуется ботом.",
            "blocked":     "🔒 Твой доступ заблокирован.",
            "key_revoked": "🔒 Твой ключ был отозван.",
        }
        await update.message.reply_text(
            msgs.get(reason, "🔒 Нет доступа."),
            parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text(
        "<b>✅ Crypto OSINT Bot</b>\n\n"
        "Отправь @username, ссылку или 0x адрес — найду кошельки.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main()
    )


# ─── Callback кнопки ──────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()

    # Главное меню
    if q.data == "back_main":
        if uid == ADMIN_ID:
            await q.edit_message_text("<b>👑 Панель администратора</b>",
                                       parse_mode=ParseMode.HTML, reply_markup=kb_admin())
        else:
            await q.edit_message_text(
                "<b>✅ Crypto OSINT Bot</b>\n\nОтправь @username, ссылку или 0x адрес.",
                parse_mode=ParseMode.HTML, reply_markup=kb_main())
        return

    # Подсказка как искать
    if q.data == "help_search":
        await q.edit_message_text(
            "<b>🔍 Как искать</b>\n\n"
            "Просто отправь в чат:\n"
            "<code>@username</code>\n"
            "<code>https://x.com/username</code>\n"
            "<code>https://t.me/username</code>\n"
            "<code>nickname.eth</code>\n\n"
            "Или адрес кошелька:\n"
            "<code>0x1234...abcd</code> → найдёт ники",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back()
        )
        return

    # Статистика
    if q.data == "stats":
        stats = await get_user_stats(uid)
        refs  = await get_referrals(uid)
        await q.edit_message_text(
            f"<b>📊 Статистика</b>\n\n"
            f"Поисков сделано: <b>{stats.get('searches', 0)}</b>\n"
            f"Приглашено людей: <b>{len(refs)}</b>\n"
            f"Доступ с: {stats.get('activated', '—')[:10]}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_back()
        )
        return

    # Реф-ссылка
    if q.data == "myref":
        ref      = await generate_ref(uid)
        bot_info = await ctx.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={ref}"
        refs     = await get_referrals(uid)
        text = (
            f"🔗 <b>Твоя реф-ссылка:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"Приглашено: <b>{len(refs)}</b> чел."
        )
        if refs:
            text += "\n"
            for r in refs[-5:]:
                text += f"\n  @{esc(r.get('username','?'))} — {r.get('date','')}"
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Админ кнопки ──────────────────────────────────────────────────────────

    if q.data == "genkey" and uid == ADMIN_ID:
        key = await generate_key()
        await q.edit_message_text(
            f"🔑 <b>Новый ключ создан:</b>\n\n"
            f"<code>{key}</code>\n\n"
            f"Отправь пользователю, пусть введёт:\n"
            f"<code>/start {key}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Создать ещё", callback_data="genkey")],
                [InlineKeyboardButton("← Назад",        callback_data="back_main")],
            ])
        )
        return

    if q.data == "keys" and uid == ADMIN_ID:
        keys = await list_keys()
        if not keys:
            text = "Ключей нет."
        else:
            lines = ["<b>🗝 Ключи:</b>\n"]
            for k in keys:
                icon = "✅" if k["active"] else "❌"
                lines.append(
                    f"{icon} <code>{esc(k['key'])}</code>\n"
                    f"   {esc(k['user'])} · поисков: {k['searches']}\n")
            text = "\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    if q.data == "refs" and uid == ADMIN_ID:
        all_refs = await list_all_refs()
        if not all_refs:
            text = "Рефералов пока нет."
        else:
            lines = ["<b>👥 Рефералы:</b>\n"]
            for entry in all_refs:
                lines.append(f"@{esc(entry['username'])} → {entry['count']} чел.")
                for r in entry["referrals"][:3]:
                    lines.append(f"  └ @{esc(r.get('username','?'))} ({r.get('date','')})")
                lines.append("")
            text = "\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return


# ─── Текстовые сообщения — поиск ──────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw = update.message.text.strip()
    if raw.startswith("/"):
        return

    if uid != ADMIN_ID:
        ok, reason = await check_access(uid)
        if not ok:
            await update.message.reply_text(
                "🔒 Нет доступа. Напиши /start",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📋 Как получить доступ", callback_data="help_search")
                ]])
            )
            return

    # Обратный поиск по адресу
    if is_eth_address(raw):
        msg = await update.message.reply_text(
            f"🔄 Ищу по адресу <code>{esc(raw[:10])}...</code>",
            parse_mode=ParseMode.HTML)
        try:
            data = await reverse_lookup(raw)
            text = await fmt_reverse(data)
            if len(text) > 4000:
                text = text[:3900] + "\n<i>...обрезано</i>"
            await msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            log.error(e, exc_info=True)
            await msg.edit_text(f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>", parse_mode=ParseMode.HTML)
        return

    # Поиск по нику
    username = extract_username(raw)
    if not username:
        await update.message.reply_text(
            "Не понял. Отправь <code>@username</code> или <code>0x...</code>",
            parse_mode=ParseMode.HTML)
        return

    variants = get_variants(username)
    msg = await update.message.reply_text(
        f"🔍 Ищу <code>@{esc(username)}</code>\n"
        f"<i>Проверяю {len(variants['domains'])} доменов...</i>",
        parse_mode=ParseMode.HTML)

    try:
        data = await run_search(username)
        if uid != ADMIN_ID:
            await use_search(uid)

        text    = await fmt_search(data)
        buttons = kb_wallet(data["all_wallets"])

        if len(text) > 4000:
            text = text[:3900] + "\n<i>...обрезано</i>"

        await msg.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=buttons, disable_web_page_preview=True)

    except Exception as e:
        log.error(e, exc_info=True)
        await msg.edit_text(f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>", parse_mode=ParseMode.HTML)


# ─── Текстовые команды для блокировки (только для админа) ─────────────────────

async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ключ: /revoke OSINT-XXXXXXXXXXXX")
        return
    ok = await revoke_key(ctx.args[0])
    await update.message.reply_text("✅ Ключ заблокирован." if ok else "❌ Ключ не найден.")

async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /block 123456789")
        return
    ok = await block_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Заблокирован." if ok else "❌ Не найден.")

async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /unblock 123456789")
        return
    ok = await unblock_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Разблокирован." if ok else "❌ Не найден или ключ отозван.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("ОШИБКА: вставь токен от @BotFather")
        return
    if ADMIN_ID == 0:
        print("ОШИБКА: вставь свой Telegram ID (узнать — @userinfobot)")
        return

    async def post_init(app):
        await init_db()
        print("✅ База данных инициализирована")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(CommandHandler("revoke",  cmd_revoke))
    app.add_handler(CommandHandler("block",   cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print(f"✅ Бот запущен. Админ ID: {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
