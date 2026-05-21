"""
Crypto OSINT Bot — полная версия с инлайн кнопками
"""

import logging, os, asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode

from searcher import extract_username, run_search, reverse_lookup, is_eth_address, get_variants
from database import init_db
from access  import (
    get_or_create_user, check_access, use_search, get_user_stats,
    activate_ref, get_referrals, list_all_refs,
    generate_key, activate_key, list_keys, revoke_key,
    block_user, unblock_user,
    create_payment, confirm_payment,
    PAYMENT_ADDRESS, SUB_PRICE_USD, TRIAL_SEARCHES, TRIAL_HOURS,
    WALLETS, NETWORK_INFO
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "0"))


def esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def kb_main(is_admin=False):
    if is_admin:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Создать ключ",   callback_data="admin_genkey")],
            [InlineKeyboardButton("🗝 Все ключи",      callback_data="admin_keys")],
            [InlineKeyboardButton("👥 Рефералы",       callback_data="admin_refs")],
            [InlineKeyboardButton("🔍 Поиск (тест)",   callback_data="help_search")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Как искать",        callback_data="help_search")],
        [InlineKeyboardButton("🔗 Моя реф-ссылка",   callback_data="myref")],
        [InlineKeyboardButton("👥 Мои рефералы",      callback_data="my_referrals")],
        [InlineKeyboardButton("💳 Купить подписку",   callback_data="buy")],
        [InlineKeyboardButton("📊 Статистика",        callback_data="stats")],
    ])

def kb_back(to="main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data=f"back_{to}")]])

def kb_buy():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Выбрать способ оплаты", callback_data="pay_choose")],
        [InlineKeyboardButton("← Назад",                  callback_data="back_main")],
    ])

def kb_networks():
    """Клавиатура выбора сети/монеты"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔷 ETH",         callback_data="pay_net_eth")],
        [InlineKeyboardButton("💚 USDT ERC-20", callback_data="pay_net_usdt_erc20")],
        [InlineKeyboardButton("🔴 USDT TRC-20", callback_data="pay_net_usdt_trc20")],
        [InlineKeyboardButton("🟡 USDT BEP-20", callback_data="pay_net_usdt_bep20")],
        [InlineKeyboardButton("🟣 SOL",         callback_data="pay_net_sol")],
        [InlineKeyboardButton("💎 TON",         callback_data="pay_net_ton")],
        [InlineKeyboardButton("← Назад",        callback_data="back_main")],
    ])

def kb_after_pay():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил",         callback_data="check_payment")],
        [InlineKeyboardButton("← Назад",              callback_data="back_main")],
    ])


# ─── Форматирование поиска ────────────────────────────────────────────────────

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


def wallet_buttons(wallets):
    if not wallets:
        return None
    btns = []
    for w in wallets[:2]:
        short = f"{w[:6]}...{w[-4:]}"
        btns.append([InlineKeyboardButton(f"⚡ {short} → Zapper", url=f"https://zapper.xyz/account/{w}")])
        if w.startswith("0x"):
            btns.append([
                InlineKeyboardButton("Etherscan", url=f"https://etherscan.io/address/{w}"),
                InlineKeyboardButton("DeBank",    url=f"https://debank.com/profile/{w}"),
            ])
    return InlineKeyboardMarkup(btns)


# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.username or str(uid)

    # Создаём пользователя если нет
    await get_or_create_user(uid, uname)

    # /start REF... — активация реф-кода
    if ctx.args and ctx.args[0].startswith("REF"):
        ref  = ctx.args[0].upper()
        ok, msg = await activate_ref(uid, uname, ref)
        await update.message.reply_text(
            msg, parse_mode=ParseMode.HTML,
            reply_markup=kb_main() if ok else InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Купить подписку", callback_data="buy")]
            ])
        )
        if ok:
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"👥 Новый реферал:\n@{esc(uname)} (ID: <code>{uid}</code>)\n"
                    f"Реф: <code>{ref}</code>",
                    parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    # /start OSINT-... — активация ключа
    if ctx.args and ctx.args[0].startswith("OSINT-"):
        arg  = ctx.args[0]
        ok, msg = await activate_key(uid, uname, arg)
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {esc(msg)}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main() if ok else None
        )
        return

    # Админ
    if uid == ADMIN_ID:
        await update.message.reply_text(
            "<b>👑 Панель администратора</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin=True)
        )
        return

    # Обычный пользователь
    stats = await get_user_stats(uid)
    has_access, reason = await check_access(uid)

    if has_access:
        status = "✅ Подписка активна" if stats.get("has_key") else f"⏳ Пробный доступ: {stats.get('trial_left',0)} поисков до {stats.get('trial_exp','')}"
    else:
        reason_text = {
            "no_sub":       "❌ Нет подписки",
            "trial_expired":"⌛ Пробный период истёк",
        }.get(reason, "❌ Нет доступа")
        status = reason_text

    await update.message.reply_text(
        f"<b>🔍 Crypto OSINT Bot</b>\n\n"
        f"Статус: {status}\n\n"
        f"Отправь @username или 0x адрес для поиска.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main()
    )


# ─── Callback кнопки ──────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    uid   = q.from_user.id
    uname = q.from_user.username or str(uid)
    await q.answer()
    await get_or_create_user(uid, uname)
    data  = q.data

    # ── Назад ─────────────────────────────────────────────────────────────────
    if data.startswith("back_"):
        if uid == ADMIN_ID:
            await q.edit_message_text("<b>👑 Панель администратора</b>",
                parse_mode=ParseMode.HTML, reply_markup=kb_main(is_admin=True))
        else:
            stats = await get_user_stats(uid)
            has_access, reason = await check_access(uid)
            status = "✅ Подписка активна" if stats.get("has_key") else (
                f"⏳ Пробный: {stats.get('trial_left',0)} поисков" if has_access else "❌ Нет подписки")
            await q.edit_message_text(
                f"<b>🔍 Crypto OSINT Bot</b>\n\nСтатус: {status}",
                parse_mode=ParseMode.HTML, reply_markup=kb_main())
        return

    # ── Как искать ────────────────────────────────────────────────────────────
    if data == "help_search":
        await q.edit_message_text(
            "<b>🔍 Как искать</b>\n\n"
            "Просто отправь в чат:\n"
            "<code>@username</code>\n"
            "<code>https://x.com/username</code>\n"
            "<code>https://t.me/username</code>\n"
            "<code>nickname.eth</code>\n\n"
            "Или адрес кошелька:\n"
            "<code>0x1234...abcd</code> → найдёт ники",
            parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Статистика ────────────────────────────────────────────────────────────
    if data == "stats":
        stats = await get_user_stats(uid)
        text  = (
            f"<b>📊 Статистика</b>\n\n"
            f"Подписка: {'✅ Активна' if stats.get('has_key') else '❌ Нет'}\n"
        )
        if stats.get("trial_left"):
            text += f"Пробных поисков: {stats['trial_left']} (до {stats.get('trial_exp','')})\n"
        text += f"Рефералов приглашено: {stats.get('ref_count', 0)}"
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Реф-ссылка ────────────────────────────────────────────────────────────
    if data == "myref":
        stats    = await get_user_stats(uid)
        ref_code = stats.get("ref_code") or ""
        bot_info = await ctx.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={ref_code}"
        await q.edit_message_text(
            f"<b>🔗 Твоя реф-ссылка:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"Друг получит {TRIAL_SEARCHES} пробных поисков на {TRIAL_HOURS}ч.\n"
            f"Приглашено: <b>{stats.get('ref_count', 0)}</b> чел.",
            parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Мои рефералы ──────────────────────────────────────────────────────────
    if data == "my_referrals":
        refs = await get_referrals(uid)
        if not refs:
            text = "👥 <b>Мои рефералы</b>\n\nПока никого нет.\nПоделись реф-ссылкой!"
        else:
            lines = [f"👥 <b>Мои рефералы ({len(refs)}):</b>\n"]
            for r in refs[:15]:
                name = r.get("username") or str(r.get("user_id","?"))
                date = str(r.get("created",""))[:10]
                lines.append(f"  @{esc(name)} — {date}")
            text = "\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Купить подписку ───────────────────────────────────────────────────────
    if data == "buy":
        await q.edit_message_text(
            f"<b>💳 Подписка</b>\n\n"
            f"Цена: <b>${SUB_PRICE_USD} USDT/ETH</b>\n\n"
            f"После оплаты ты получишь постоянный ключ доступа.\n"
            f"Нажми кнопку ниже чтобы получить адрес для оплаты.",
            parse_mode=ParseMode.HTML, reply_markup=kb_buy())
        return

    # ── Выбор способа оплаты ─────────────────────────────────────────────────
    if data == "pay_choose":
        await q.edit_message_text(
            f"<b>💳 Выбери способ оплаты</b>\n\n"
            f"Сумма: <b>${SUB_PRICE_USD}</b>\n"
            f"Выбери монету и сеть:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_networks()
        )
        return

    # ── Оплата через конкретную сеть ──────────────────────────────────────────
    if data.startswith("pay_net_"):
        network = data.replace("pay_net_", "")
        net     = NETWORK_INFO.get(network, {})
        address = WALLETS.get(network, "")
        payment = await create_payment(uid, network)

        await q.edit_message_text(
            f"{net.get('emoji','')} <b>Оплата {net.get('name','')}</b>\n\n"
            f"Отправь <b>${payment['amount']} {net.get('symbol','')}</b>\n"
            f"Сеть: <b>{net.get('network','')}</b>\n\n"
            f"На адрес:\n<code>{address}</code>\n\n"
            f"⚠️ Отправляй только в сети <b>{net.get('network','')}</b>\n\n"
            f"После оплаты нажми кнопку ниже.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Я оплатил",      callback_data="check_payment")],
                [InlineKeyboardButton("← Другая монета",   callback_data="pay_choose")],
                [InlineKeyboardButton("← Назад",           callback_data="back_main")],
            ])
        )
        return

    # ── Проверка оплаты ───────────────────────────────────────────────────────
    if data == "check_payment":
        # Здесь в будущем можно добавить реальную проверку через API
        # Пока — ручное подтверждение через команду /confirm ID (только для админа)
        await q.edit_message_text(
            "⏳ <b>Проверяем оплату...</b>\n\n"
            "Обычно это занимает до 5 минут.\n"
            "Как только оплата подтвердится — тебе придёт сообщение с ключом.\n\n"
            "Если прошло больше 10 минут — напиши администратору.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Проверить снова", callback_data="check_payment")],
                [InlineKeyboardButton("← Назад", callback_data="back_main")],
            ])
        )
        # Уведомляем админа
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"💳 Пользователь @{esc(uname)} (ID: <code>{uid}</code>) "
                f"нажал 'Я оплатил'.\n"
                f"Для подтверждения: /confirm {uid}",
                parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return

    # ── Админ: создать ключ ───────────────────────────────────────────────────
    if data == "admin_genkey" and uid == ADMIN_ID:
        key = await generate_key()
        await q.edit_message_text(
            f"🔑 <b>Новый ключ:</b>\n\n<code>{key}</code>\n\n"
            f"Пользователь вводит:\n<code>/start {key}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Ещё ключ", callback_data="admin_genkey")],
                [InlineKeyboardButton("← Назад",     callback_data="back_main")],
            ])
        )
        return

    # ── Админ: все ключи ──────────────────────────────────────────────────────
    if data == "admin_keys" and uid == ADMIN_ID:
        keys = await list_keys()
        if not keys:
            text = "Ключей нет. Создай: нажми 'Создать ключ'"
        else:
            lines = ["<b>🗝 Ключи:</b>\n"]
            for k in keys[:20]:
                icon = "✅" if k["active"] else "❌"
                user = f"@{k['username']}" if k.get("username") else "свободен"
                lines.append(f"{icon} <code>{esc(k['key'])}</code> — {user}")
            text = "\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Админ: рефералы ───────────────────────────────────────────────────────
    if data == "admin_refs" and uid == ADMIN_ID:
        all_refs = await list_all_refs()
        if not all_refs:
            text = "Рефералов пока нет."
        else:
            lines = ["<b>👥 Рефералы:</b>\n"]
            for entry in all_refs[:15]:
                lines.append(f"@{esc(entry['username'])} → {entry['count']} чел.")
                for r in entry["referrals"][:3]:
                    lines.append(f"  └ @{esc(r.get('username','?'))}")
                lines.append("")
            text = "\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return


# ─── Текстовые сообщения — поиск ──────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    raw   = update.message.text.strip()
    if raw.startswith("/"):
        return

    await get_or_create_user(uid, uname)

    # Проверка доступа
    if uid != ADMIN_ID:
        has_access, reason = await check_access(uid)
        if not has_access:
            msgs = {
                "no_sub":       "У тебя нет подписки.",
                "trial_expired":"Пробный период истёк.",
            }
            await update.message.reply_text(
                f"🔒 {msgs.get(reason, 'Нет доступа.')}\n\nКупи подписку или активируй реф-код.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Купить подписку", callback_data="buy")],
                    [InlineKeyboardButton("🔗 Есть реф-код?",  callback_data="help_ref")],
                ])
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
            if uid != ADMIN_ID:
                await use_search(uid)
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
        buttons = wallet_buttons(data["all_wallets"])
        if len(text) > 4000:
            text = text[:3900] + "\n<i>...обрезано</i>"
        await msg.edit_text(text, parse_mode=ParseMode.HTML,
                            reply_markup=buttons, disable_web_page_preview=True)
    except Exception as e:
        log.error(e, exc_info=True)
        await msg.edit_text(f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>", parse_mode=ParseMode.HTML)


# ─── Админ команды ────────────────────────────────────────────────────────────

async def cmd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подтвердить оплату вручную: /confirm USER_ID"""
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /confirm 123456789")
        return
    user_id = int(ctx.args[0])
    key = await confirm_payment(user_id)
    await update.message.reply_text(f"✅ Оплата подтверждена. Ключ: <code>{key}</code>", parse_mode=ParseMode.HTML)
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Твой ключ доступа:\n<code>{key}</code>\n\n"
            f"Теперь у тебя полный доступ к боту.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
    except Exception:
        pass


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
    await block_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Заблокирован.")


async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /unblock 123456789")
        return
    await unblock_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Разблокирован.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("ОШИБКА: вставь токен от @BotFather")
        return
    if ADMIN_ID == 0:
        print("ОШИБКА: вставь ADMIN_ID")
        return

    async def post_init(app):
        await init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("revoke",  cmd_revoke))
    app.add_handler(CommandHandler("block",   cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print(f"✅ Бот запущен. Админ: {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
