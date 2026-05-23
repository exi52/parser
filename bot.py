"""
Crypto OSINT Bot — тарифы, хеш транзы, поддержка
"""

import logging, os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode

from searcher import extract_username, run_search, reverse_lookup, is_eth_address, get_variants
from database import init_db, close_pool
from access  import (
    get_or_create_user, check_access, use_search, get_user_stats,
    activate_ref, get_referrals, list_all_refs,
    generate_key, activate_key, list_keys, revoke_key,
    block_user, unblock_user,
    create_payment, confirm_payment, submit_payment_hash,
    start_payment_hash, get_pending_payment,
    PAYMENT_ADDRESS, TRIAL_SEARCHES, TRIAL_HOURS,
    WALLETS, NETWORK_INFO
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
SUPPORT_USER = "ant7h3m"


def parse_admin_ids() -> tuple[int, ...]:
    raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "")
    ids = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            log.warning("Invalid admin id in ADMIN_IDS: %s", part)
    return tuple(dict.fromkeys(ids))


ADMIN_IDS = parse_admin_ids()
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def notify_admins(bot, text: str, **kwargs):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, **kwargs)
        except Exception:
            log.exception("Failed to notify admin %s", admin_id)

# ── Тарифы ────────────────────────────────────────────────────────────────────
PLANS = {
    "plan_1w":  {"name": "1 неделя",  "price": 9,   "days": 7},
    "plan_1m":  {"name": "1 месяц",   "price": 29,  "days": 30},
    "plan_3m":  {"name": "3 месяца",  "price": 69,  "days": 90},
    "plan_life":{"name": "Навсегда",  "price": 149, "days": 36500},
}

def esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def kb_main(is_admin=False):
    if is_admin:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Создать ключ",    callback_data="admin_genkey")],
            [InlineKeyboardButton("🗝 Все ключи",       callback_data="admin_keys")],
            [InlineKeyboardButton("👥 Рефералы",        callback_data="admin_refs")],
            [InlineKeyboardButton("🔗 Моя реф-ссылка", callback_data="myref")],
            [InlineKeyboardButton("💳 Купить подписку", callback_data="buy")],
            [InlineKeyboardButton("🆘 Поддержка",       url=f"https://t.me/{SUPPORT_USER}")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Моя реф-ссылка",  callback_data="myref")],
        [InlineKeyboardButton("👥 Мои рефералы",     callback_data="my_referrals")],
        [InlineKeyboardButton("💳 Купить подписку",  callback_data="buy")],
        [InlineKeyboardButton("📊 Статистика",       callback_data="stats")],
        [InlineKeyboardButton("🆘 Поддержка",        url=f"https://t.me/{SUPPORT_USER}")],
    ])


def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Назад", callback_data="back_main")]
    ])


def kb_plans():
    rows = []
    for plan_id, p in PLANS.items():
        rows.append([InlineKeyboardButton(
            f"{p['name']} — ${p['price']}",
            callback_data=f"plan_{plan_id}"
        )])
    rows.append([InlineKeyboardButton("← Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_networks(plan_id: str):
    p = PLANS.get(plan_id, {})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔷 ETH         — ${p.get('price',0)}",      callback_data=f"pay_eth_{plan_id}")],
        [InlineKeyboardButton(f"💚 USDT ERC-20 — ${p.get('price',0)}",      callback_data=f"pay_usdt_erc20_{plan_id}")],
        [InlineKeyboardButton(f"🔴 USDT TRC-20 — ${p.get('price',0)}",      callback_data=f"pay_usdt_trc20_{plan_id}")],
        [InlineKeyboardButton(f"🟡 USDT BEP-20 — ${p.get('price',0)}",      callback_data=f"pay_usdt_bep20_{plan_id}")],
        [InlineKeyboardButton(f"🟣 SOL         — ${p.get('price',0)}",      callback_data=f"pay_sol_{plan_id}")],
        [InlineKeyboardButton(f"💎 TON         — ${p.get('price',0)}",      callback_data=f"pay_ton_{plan_id}")],
        [InlineKeyboardButton("← Другой тариф", callback_data="buy")],
        [InlineKeyboardButton("← Назад",         callback_data="back_main")],
    ])


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
        source = r.get("platform", "Источник")
        matched = r.get("matched") or u
        lines.append(f"\n<b>{esc(source)}</b>: <code>{esc(matched)}</code>")
        for w in (r.get("wallets") or [])[:3]:
            if w in shown:
                continue
            shown.add(w)
            lines.append(f"<code>{esc(w)}</code>")
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
    await get_or_create_user(uid, uname)

    # /start REF...
    if ctx.args and ctx.args[0].startswith("REF"):
        ref      = ctx.args[0].upper()
        ok, msg  = await activate_ref(uid, uname, ref)
        await update.message.reply_text(
            msg, parse_mode=ParseMode.HTML,
            reply_markup=kb_main() if ok else InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Купить подписку", callback_data="buy")],
                [InlineKeyboardButton("🆘 Поддержка", url=f"https://t.me/{SUPPORT_USER}")],
            ])
        )
        if ok:
            await notify_admins(
                ctx.bot,
                f"👥 Новый реферал:\n@{esc(uname)} (ID: <code>{uid}</code>)\n"
                f"Реф: <code>{ref}</code>",
                parse_mode=ParseMode.HTML)
        return

    # /start OSINT-...
    if ctx.args and ctx.args[0].startswith("OSINT-"):
        ok, msg = await activate_key(uid, uname, ctx.args[0])
        await update.message.reply_text(
            f"{'✅' if ok else '❌'} {esc(msg)}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main() if ok else None)
        return

    if is_admin(uid):
        await update.message.reply_text(
            "<b>👑 Панель администратора</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin=True))
        return

    has_access, reason = await check_access(uid)
    stats = await get_user_stats(uid)

    if has_access:
        if stats.get("sub_active"):
            status = f"✅ Подписка активна до {stats.get('sub_exp')}"
        else:
            status = f"⏳ Пробный доступ: {stats.get('trial_left',0)} поисков до {stats.get('trial_exp','')}"
    else:
        status = {
            "no_sub":       "❌ Нет подписки",
            "trial_expired":"⌛ Пробный период истёк",
            "sub_expired":  "⌛ Подписка истекла",
            "blocked":      "⛔ Аккаунт заблокирован",
        }.get(reason, "❌ Нет доступа")

    await update.message.reply_text(
        f"<b>🔍 Crypto OSINT Bot</b>\n\n"
        f"Статус: {status}\n\n"
        f"Отправь @username или 0x адрес для поиска.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_main())


# ─── Callback кнопки ──────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    uid   = q.from_user.id
    uname = q.from_user.username or str(uid)
    await q.answer()
    await get_or_create_user(uid, uname)
    data  = q.data

    # ── Назад в главное меню ──────────────────────────────────────────────────
    if data == "back_main":
        admin = is_admin(uid)
        has_access, reason = await check_access(uid)
        stats = await get_user_stats(uid)
        if has_access:
            status = f"✅ Подписка до {stats.get('sub_exp')}" if stats.get("sub_active") else f"⏳ Пробный: {stats.get('trial_left',0)} поисков"
        else:
            status = {
                "blocked": "⛔ Аккаунт заблокирован",
                "sub_expired": "⌛ Подписка истекла",
                "trial_expired": "⌛ Пробный период истёк",
            }.get(reason, "❌ Нет подписки")
        await q.edit_message_text(
            f"<b>{'👑 Панель администратора' if admin else '🔍 Crypto OSINT Bot'}</b>"
            + (f"\n\nСтатус: {status}" if not admin else ""),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin=admin))
        return

    # ── Статистика ────────────────────────────────────────────────────────────
    if data == "stats":
        stats = await get_user_stats(uid)
        text  = "<b>📊 Статистика</b>\n\n"
        text += f"Подписка: {'✅ Активна до ' + stats.get('sub_exp','') if stats.get('sub_active') else '❌ Нет'}\n"
        if stats.get("trial_left"):
            text += f"Пробных поисков: {stats['trial_left']} (до {stats.get('trial_exp','')})\n"
        text += f"Рефералов: {stats.get('ref_count', 0)}"
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

    # ── Выбор тарифа ─────────────────────────────────────────────────────────
    if data == "buy":
        await q.edit_message_text(
            "<b>💳 Выбери тариф</b>\n\n"
            "После покупки получишь постоянный ключ доступа:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_plans())
        return

    # ── Выбор сети после выбора тарифа ───────────────────────────────────────
    if data.startswith("plan_plan_"):
        plan_id = data.replace("plan_plan_", "plan_")
        p = PLANS.get(plan_id, {})
        await q.edit_message_text(
            f"<b>💳 Тариф: {p.get('name','')}</b>\n"
            f"Цена: <b>${p.get('price',0)}</b>\n\n"
            f"Выбери сеть оплаты:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_networks(plan_id))
        return

    # ── Оплата: конкретная сеть + тариф ──────────────────────────────────────
    if data.startswith("pay_"):
        # Формат: pay_eth_plan_1m  или  pay_usdt_erc20_plan_1w
        # Вычленяем plan_id — всё начиная с "plan_"
        plan_idx = data.index("plan_")
        plan_id  = data[plan_idx:]
        network  = data[4:plan_idx-1]   # между "pay_" и "_plan_..."

        p   = PLANS.get(plan_id, {})
        net = NETWORK_INFO.get(network, {})
        address = WALLETS.get(network, "")

        if not address or "ВСТАВЬ" in address:
            await q.edit_message_text(
                "❌ Этот способ оплаты временно недоступен.\nВыбери другой.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_networks(plan_id))
            return

        payment = await create_payment(
            uid,
            plan_id,
            p.get("price", 0),
            p.get("days", 30),
            network,
        )

        await q.edit_message_text(
            f"{net.get('emoji','')} <b>Оплата {net.get('name','')}</b>\n\n"
            f"Тариф: <b>{p.get('name','')}</b>\n"
            f"Сумма: <b>${p.get('price',0)} {net.get('symbol','')}</b>\n"
            f"Сеть: <b>{net.get('network','')}</b>\n\n"
            f"Заявка: <code>#{payment['id']}</code>\n"
            f"Адрес для перевода:\n"
            f"<code>{address}</code>\n\n"
            f"⚠️ Отправляй строго в сети <b>{net.get('network','')}</b>\n\n"
            f"После оплаты нажми кнопку и пришли хеш транзакции.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Оплатил — прислать хеш", callback_data="send_hash")],
                [InlineKeyboardButton("← Другая сеть",             callback_data=f"plan_plan_{plan_id[5:]}")],
                [InlineKeyboardButton("← Другой тариф",            callback_data="buy")],
            ]))
        return

    # ── Пользователь нажал "Прислать хеш" ────────────────────────────────────
    if data == "send_hash":
        payment = await start_payment_hash(uid)
        if not payment:
            await q.edit_message_text(
                "❌ Активная заявка на оплату не найдена.\nВыбери тариф заново.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_plans())
            return
        await q.edit_message_text(
            "📝 <b>Пришли хеш транзакции</b>\n\n"
            "Скопируй хеш (txid) из своего кошелька и отправь его сюда.\n\n"
            "Выглядит примерно так:\n"
            "<code>0xabc123...def456</code>\n\n"
            "Мы проверим и активируем доступ.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Отмена", callback_data="cancel_hash")]
            ]))
        return

    # ── Отмена отправки хеша ─────────────────────────────────────────────────
    if data == "cancel_hash":
        await q.edit_message_text(
            "<b>🔍 Crypto OSINT Bot</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
        return

    # ── Админ: создать ключ ───────────────────────────────────────────────────
    if data == "admin_genkey" and is_admin(uid):
        key = await generate_key()
        await q.edit_message_text(
            f"🔑 <b>Новый ключ:</b>\n\n<code>{key}</code>\n\n"
            f"Пользователь вводит:\n<code>/start {key}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Ещё ключ", callback_data="admin_genkey")],
                [InlineKeyboardButton("← Назад",     callback_data="back_main")],
            ]))
        return

    # ── Админ: все ключи ──────────────────────────────────────────────────────
    if data == "admin_keys" and is_admin(uid):
        keys = await list_keys()
        if not keys:
            text = "Ключей нет."
        else:
            lines = ["<b>🗝 Ключи:</b>\n"]
            for k in keys[:20]:
                icon = "✅" if k["active"] else "❌"
                user = f"@{k['username']}" if k.get("username") else "свободен"
                expires = k["expires"].strftime("%d.%m.%Y") if k.get("expires") else "без срока"
                lines.append(f"{icon} <code>{esc(k['key'])}</code> — {user} — до {expires}")
            text = "\n".join(lines)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Админ: рефералы ───────────────────────────────────────────────────────
    if data == "admin_refs" and is_admin(uid):
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


# ─── Текстовые сообщения ──────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    raw   = update.message.text.strip()
    if raw.startswith("/"):
        return

    await get_or_create_user(uid, uname)

    # ── Пользователь присылает хеш транзакции ─────────────────────────────────
    pending_payment = await get_pending_payment(uid)
    if pending_payment and pending_payment.get("status") == "awaiting_hash":
        payment = await submit_payment_hash(uid, raw)
        if not payment:
            await update.message.reply_text(
                "❌ Не нашёл активную заявку на оплату. Выбери тариф заново.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_plans())
            return

        plan_id  = payment.get("plan_id") or "?"
        network  = payment.get("network") or "?"
        plan     = PLANS.get(plan_id, {})
        net_info = NETWORK_INFO.get(network, {})

        await notify_admins(
            ctx.bot,
            f"💳 <b>Новая оплата!</b>\n\n"
            f"Пользователь: @{esc(uname)} (ID: <code>{uid}</code>)\n"
            f"Тариф: <b>{plan.get('name','?')}</b> — ${plan.get('price','?')}\n"
            f"Сеть: {net_info.get('emoji','')} <b>{net_info.get('name','?')}</b>\n\n"
            f"Заявка: <code>#{payment['id']}</code>\n"
            f"Хеш транзакции:\n<code>{esc(raw)}</code>\n\n"
            f"Подтвердить: /confirm {uid}",
            parse_mode=ParseMode.HTML)

        await update.message.reply_text(
            "✅ <b>Хеш получен!</b>\n\n"
            "Мы проверяем транзакцию — обычно это занимает до 10 минут.\n"
            "Как только подтвердим — пришлём ключ доступа.\n\n"
            f"Есть вопросы? @{SUPPORT_USER}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
        return

    # ── Проверка доступа ──────────────────────────────────────────────────────
    if not is_admin(uid):
        has_access, reason = await check_access(uid)
        if not has_access:
            msgs = {
                "no_sub":       "У тебя нет подписки.",
                "trial_expired":"Пробный период истёк.",
                "sub_expired":  "Подписка истекла.",
                "blocked":      "Аккаунт заблокирован.",
            }
            await update.message.reply_text(
                f"🔒 {msgs.get(reason,'Нет доступа.')}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Купить подписку", callback_data="buy")],
                    [InlineKeyboardButton("🆘 Поддержка", url=f"https://t.me/{SUPPORT_USER}")],
                ]))
            return

    # ── Обратный поиск по адресу ──────────────────────────────────────────────
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
            if not is_admin(uid):
                await use_search(uid)
        except Exception as e:
            log.error(e, exc_info=True)
            await msg.edit_text(f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>", parse_mode=ParseMode.HTML)
        return

    # ── Поиск по нику ─────────────────────────────────────────────────────────
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
        if not is_admin(uid):
            await use_search(uid)
        text    = await fmt_search(data)
        buttons = wallet_buttons(data["all_wallets"])
        if len(text) > 4000:
            text = text[:3900] + "\n<i>...обрезано</i>"
        await msg.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=buttons, disable_web_page_preview=True)
    except Exception as e:
        log.error(e, exc_info=True)
        await msg.edit_text(f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>", parse_mode=ParseMode.HTML)


# ─── Админ команды ────────────────────────────────────────────────────────────

async def cmd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подтвердить оплату вручную: /confirm USER_ID"""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /confirm 123456789")
        return
    try:
        user_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /confirm 123456789")
        return

    payment = await confirm_payment(user_id)
    if not payment:
        await update.message.reply_text(
            "❌ Не нашёл отправленную заявку с tx hash для этого пользователя.")
        return

    key = payment["key"]
    expires = payment["expires"].strftime("%d.%m.%Y %H:%M")
    await update.message.reply_text(
        f"✅ Подтверждено. Ключ: <code>{key}</code>\n"
        f"Доступ до: <b>{expires}</b>",
        parse_mode=ParseMode.HTML)
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Твой ключ:\n<code>{key}</code>\n\n"
            f"Доступ активирован до <b>{expires}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
    except Exception:
        pass


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ключ: /revoke OSINT-...")
        return
    ok = await revoke_key(ctx.args[0])
    await update.message.reply_text("✅ Заблокирован." if ok else "❌ Не найден.")


async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /block 123456789")
        return
    await block_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Заблокирован.")


async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /unblock 123456789")
        return
    await unblock_user(int(ctx.args[0]))
    await update.message.reply_text("✅ Разблокирован.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("ОШИБКА: вставь токен")
        return
    if not ADMIN_IDS:
        print("ОШИБКА: вставь ADMIN_IDS или ADMIN_ID")
        return

    async def post_init(app):
        await init_db()

    async def post_shutdown(app):
        await close_pool()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_start))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("revoke",  cmd_revoke))
    app.add_handler(CommandHandler("block",   cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print(f"✅ Бот запущен. Админы: {', '.join(map(str, ADMIN_IDS))}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
