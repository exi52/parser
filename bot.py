"""
Crypto OSINT Bot — тарифы, хеш транзы, поддержка
"""

import asyncio, csv, io, logging, os, time
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, WebAppInfo
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode

from searcher import extract_username, run_search, run_bulk_search, reverse_lookup, is_eth_address, get_variants, enrich_balances, GOLDRUSH_API_KEY
from database import init_db, close_pool, get_pool
from access  import (
    get_or_create_user, check_access, use_search, get_user_stats,
    activate_ref, get_referrals, list_all_refs,
    generate_key, activate_key, list_keys, revoke_key,
    block_user, unblock_user,
    create_payment, confirm_payment, submit_payment_hash,
    grant_subscription,
    start_payment_hash, get_pending_payment,
    check_bulk_access, get_bulk_status, grant_bulk_access, revoke_bulk_access,
    consume_bulk_credit,
    request_bulk_payment, start_bulk_hash, get_pending_bulk,
    submit_bulk_hash, confirm_bulk_payment,
    PAYMENT_ADDRESS, TRIAL_SEARCHES, TRIAL_HOURS,
    WALLETS, NETWORK_INFO
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")
SUPPORT_USER = "ant7h3m"
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")  # например: @findtargetinfo_channel
MINIAPP_URL = os.getenv("MINIAPP_URL", "")

RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
MAX_BULK_LINES = int(os.getenv("MAX_BULK_LINES", "10000"))
BULK_WORKERS = int(os.getenv("BULK_WORKERS", "20"))
BULK_MAX_ACTIVE_JOBS = int(os.getenv("BULK_MAX_ACTIVE_JOBS", "2"))
BULK_MAX_FILE_BYTES = int(os.getenv("BULK_MAX_FILE_BYTES", str(2 * 1024 * 1024)))
BULK_PROGRESS_STEP = int(os.getenv("BULK_PROGRESS_STEP", "250"))

_user_requests: dict[int, list[float]] = defaultdict(list)
_active_bulk_users: set[int] = set()
_bulk_slots = asyncio.Semaphore(BULK_MAX_ACTIVE_JOBS)


def check_rate_limit(user_id: int) -> bool:
    """True = разрешено, False = лимит превышен."""
    now = time.time()
    reqs = [t for t in _user_requests[user_id] if now - t < RATE_LIMIT_WINDOW]
    _user_requests[user_id] = reqs
    if len(reqs) >= RATE_LIMIT_MAX:
        return False
    reqs.append(now)
    return True


async def is_subscribed(bot, user_id: int) -> bool:
    """Проверяет подписан ли юзер на обязательный канал"""
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning(f"Subscription check failed: {e}")
        return True  # не блокируем если бот не смог проверить (например не админ канала)


def kb_subscribe():
    channel_link = REQUIRED_CHANNEL.lstrip("@")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{channel_link}")],
        [InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")],
    ])


def parse_id_list(raw: str) -> set[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("Invalid user id in list: %s", part)
    return ids


BULK_USER_IDS = parse_id_list(os.getenv("BULK_USER_IDS", ""))


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

# ── Bulk (отдельная платная функция, оплата за количество поисков) ─────────────
BULK_PLANS = {
    "bulk_1":  {"name": "1 поиск",    "price": 9,   "credits": 1},
    "bulk_5":  {"name": "5 поисков",  "price": 39,  "credits": 5},
    "bulk_20": {"name": "20 поисков", "price": 129, "credits": 20},
}

def esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

def kb_main(is_admin=False):
    bulk_web_row = []
    if MINIAPP_URL:
        bulk_web_row = [InlineKeyboardButton("📊 Bulk Web App", web_app=WebAppInfo(url=MINIAPP_URL))]

    if is_admin:
        rows = [
            [InlineKeyboardButton("🔑 Создать ключ",    callback_data="admin_genkey")],
            [InlineKeyboardButton("🗝 Все ключи",       callback_data="admin_keys")],
            [InlineKeyboardButton("👥 Рефералы",        callback_data="admin_refs")],
            [InlineKeyboardButton("📦 Bulk по файлу",   callback_data="bulk_info")],
            [InlineKeyboardButton("🛒 Купить Bulk",     callback_data="bulk_buy")],
        ]
        if bulk_web_row:
            rows.append(bulk_web_row)
        rows.extend([
            [InlineKeyboardButton("🔗 Моя реф-ссылка", callback_data="myref")],
            [InlineKeyboardButton("💳 Купить подписку", callback_data="buy")],
            [InlineKeyboardButton("🆘 Поддержка",       url=f"https://t.me/{SUPPORT_USER}")],
        ])
        return InlineKeyboardMarkup(rows)

    rows = [
        [InlineKeyboardButton("🔗 Моя реф-ссылка",  callback_data="myref")],
        [InlineKeyboardButton("👥 Мои рефералы",     callback_data="my_referrals")],
        [InlineKeyboardButton("💳 Купить подписку",  callback_data="buy")],
        [InlineKeyboardButton("📦 Bulk по файлу",    callback_data="bulk_info")],
        [InlineKeyboardButton("🛒 Купить Bulk",      callback_data="bulk_buy")],
    ]
    if bulk_web_row:
        rows.append(bulk_web_row)
    rows.extend([
        [InlineKeyboardButton("📊 Статистика",       callback_data="stats")],
        [InlineKeyboardButton("🆘 Поддержка",        url=f"https://t.me/{SUPPORT_USER}")],
    ])
    return InlineKeyboardMarkup(rows)


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


def kb_bulk_plans():
    rows = []
    for plan_id, p in BULK_PLANS.items():
        rows.append([InlineKeyboardButton(
            f"{p['name']} — ${p['price']}",
            callback_data=f"bulkplan:{plan_id}"
        )])
    rows.append([InlineKeyboardButton("← Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_bulk_networks(plan_id: str):
    p = BULK_PLANS.get(plan_id, {})
    price = p.get("price", 0)
    nets = [
        ("eth",        f"🔷 ETH         — ${price}"),
        ("usdt_erc20", f"💚 USDT ERC-20 — ${price}"),
        ("usdt_trc20", f"🔴 USDT TRC-20 — ${price}"),
        ("usdt_bep20", f"🟡 USDT BEP-20 — ${price}"),
        ("sol",        f"🟣 SOL         — ${price}"),
        ("ton",        f"💎 TON         — ${price}"),
    ]
    rows = [[InlineKeyboardButton(label, callback_data=f"bulkpay:{net}:{plan_id}")]
            for net, label in nets]
    rows.append([InlineKeyboardButton("← Другой тариф", callback_data="bulk_buy")])
    rows.append([InlineKeyboardButton("← Назад",         callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


# ─── Форматирование поиска ────────────────────────────────────────────────────

async def fmt_search(data: dict, balances: dict | None = None) -> str:
    balances = balances or {}

    def _bal_line(w: str):
        info = balances.get(w)
        if not info or not isinstance(info.get("balance_usd"), (int, float)):
            return None
        usd = info["balance_usd"]
        bits = [f"💰 ${usd:,.2f}"]
        if info.get("top_tokens"):
            bits.append(", ".join(info["top_tokens"][:3]))
        if info.get("chains"):
            bits.append(", ".join(info["chains"]))
        return " · ".join(bits)

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
            bal = _bal_line(w)
            if bal:
                lines.append(bal)
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

    # Проверка подписки на канал (кроме админов)
    if not is_admin(uid) and REQUIRED_CHANNEL:
        subscribed = await is_subscribed(ctx.bot, uid)
        if not subscribed:
            await update.message.reply_text(
                "<b>📢 Для использования бота подпишись на канал</b>\n\n"
                "После подписки нажми кнопку ниже.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_subscribe())
            return

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
            "<b>👑 Панель администратора · BULK-v2</b>",
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
        f"<b>🔍 Crypto OSINT Bot · BULK-v2</b>\n\n"
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

    # ── Проверка подписки на канал ────────────────────────────────────────────
    if data == "check_sub":
        subscribed = await is_subscribed(ctx.bot, uid)
        if subscribed:
            admin = is_admin(uid)
            await q.edit_message_text(
                "<b>✅ Подписка подтверждена!</b>\n\nТеперь можешь пользоваться ботом.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_main(is_admin=admin))
        else:
            await q.answer("❌ Ты ещё не подписался. Подпишись и попробуй снова.", show_alert=True)
        return

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
            f"<b>{'👑 Панель администратора' if admin else '🔍 Crypto OSINT Bot'} · BULK-v2</b>"
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
            "<b>🔍 Crypto OSINT Bot · BULK-v2</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
        return

    # ── Bulk: инфо / статус ───────────────────────────────────────────────────
    if data == "bulk_info":
        status = await get_bulk_status(uid)
        if status["active"]:
            await q.edit_message_text(
                "<b>📦 Bulk по файлу</b>\n\n"
                f"✅ Осталось поисков: <b>{status['credits']}</b>\n"
                "Один загруженный файл = один поиск.\n\n"
                "Пришли <code>.txt</code> или <code>.csv</code> со списком "
                "<code>@username</code> (по одному на строку) и бот проверит "
                "их пачкой, вернёт CSV с найденными кошельками.\n\n"
                f"Лимит: до {MAX_BULK_LINES} строк, файл до "
                f"{BULK_MAX_FILE_BYTES // 1024 // 1024} MB.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Докупить поиски", callback_data="bulk_buy")],
                    [InlineKeyboardButton("← Назад",            callback_data="back_main")],
                ]))
            return
        await q.edit_message_text(
            "<b>📦 Bulk по файлу — отдельная функция</b>\n\n"
            "Кидаешь <code>.txt</code>/<code>.csv</code> со списком "
            "<code>@username</code> (по одному на строку) — бот сканит всех "
            "пачкой и возвращает CSV с кошельками, платформами и балансами.\n\n"
            "Оплата за количество поисков: один файл = один поиск.\n\n"
            f"Лимит: до {MAX_BULK_LINES} строк за поиск, файл до "
            f"{BULK_MAX_FILE_BYTES // 1024 // 1024} MB.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Купить Bulk", callback_data="bulk_buy")],
                [InlineKeyboardButton("🆘 Поддержка",   url=f"https://t.me/{SUPPORT_USER}")],
                [InlineKeyboardButton("← Назад",        callback_data="back_main")],
            ]))
        return

    # ── Bulk: выбор тарифа ────────────────────────────────────────────────────
    if data == "bulk_buy":
        await q.edit_message_text(
            "<b>📦 Bulk по файлу — выбери тариф</b>\n\n"
            "После оплаты доступ к загрузке файлов включится автоматически:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_bulk_plans())
        return

    # ── Bulk: выбор сети ──────────────────────────────────────────────────────
    if data.startswith("bulkplan:"):
        plan_id = data.split(":", 1)[1]
        p = BULK_PLANS.get(plan_id, {})
        await q.edit_message_text(
            f"<b>📦 {p.get('name','')}</b>\n"
            f"Цена: <b>${p.get('price',0)}</b>\n\n"
            f"Выбери сеть оплаты:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_bulk_networks(plan_id))
        return

    # ── Bulk: оплата (сеть + тариф) ───────────────────────────────────────────
    if data.startswith("bulkpay:"):
        _, network, plan_id = data.split(":", 2)
        p   = BULK_PLANS.get(plan_id, {})
        net = NETWORK_INFO.get(network, {})
        address = WALLETS.get(network, "")

        if not address or "ВСТАВЬ" in address:
            await q.edit_message_text(
                "❌ Этот способ оплаты временно недоступен.\nВыбери другой.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_bulk_networks(plan_id))
            return

        req = await request_bulk_payment(
            uid, plan_id, p.get("price", 0), p.get("credits", 1), network)

        await q.edit_message_text(
            f"{net.get('emoji','')} <b>Оплата Bulk · {net.get('name','')}</b>\n\n"
            f"Тариф: <b>{p.get('name','')}</b>\n"
            f"Сумма: <b>${p.get('price',0)} {net.get('symbol','')}</b>\n"
            f"Сеть: <b>{net.get('network','')}</b>\n\n"
            f"Заявка: <code>#{req['id']}</code>\n"
            f"Адрес для перевода:\n"
            f"<code>{address}</code>\n\n"
            f"⚠️ Отправляй строго в сети <b>{net.get('network','')}</b>\n\n"
            f"После оплаты нажми кнопку и пришли хеш транзакции.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Оплатил — прислать хеш", callback_data="bulk_send_hash")],
                [InlineKeyboardButton("← Другая сеть",             callback_data=f"bulkplan:{plan_id}")],
                [InlineKeyboardButton("← Другой тариф",            callback_data="bulk_buy")],
            ]))
        return

    # ── Bulk: пользователь нажал "Прислать хеш" ───────────────────────────────
    if data == "bulk_send_hash":
        req = await start_bulk_hash(uid)
        if not req:
            await q.edit_message_text(
                "❌ Активная заявка на Bulk не найдена.\nВыбери тариф заново.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_bulk_plans())
            return
        await q.edit_message_text(
            "📝 <b>Пришли хеш транзакции (Bulk)</b>\n\n"
            "Скопируй txid из кошелька и отправь сюда.\n"
            "Мы проверим и включим доступ к загрузке файлов.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Отмена", callback_data="bulk_cancel_hash")]
            ]))
        return

    if data == "bulk_cancel_hash":
        await q.edit_message_text(
            "<b>🔍 Crypto OSINT Bot · BULK-v2</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin=is_admin(uid)))
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


def parse_bulk_usernames(text: str, limit: int = MAX_BULK_LINES) -> tuple[list[str], int]:
    usernames = []
    seen = set()
    total_lines = 0
    for line in text.splitlines():
        total_lines += 1
        raw = line.strip()
        if not raw:
            continue
        first_cell = raw.split(",", 1)[0].split(";", 1)[0].split("\t", 1)[0].strip()
        username = extract_username(first_cell)
        if not username:
            continue
        key = username.lower()
        if key in seen:
            continue
        seen.add(key)
        usernames.append(username)
        if len(usernames) >= limit:
            break
    return usernames, total_lines


def bulk_result_to_csv(results: list[dict], balances: dict | None = None) -> bytes:
    balances = balances or {}
    show_balance = bool(GOLDRUSH_API_KEY)
    output = io.StringIO()
    writer = csv.writer(output)
    header = [
        "username", "found_count", "wallets", "platforms",
        "matched", "cache_hit", "elapsed_ms", "errors",
    ]
    if show_balance:
        header[3:3] = ["wallet_balances_usd", "wallet_top_tokens", "wallet_chains"]
    writer.writerow(header)

    def _short(w: str) -> str:
        return f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w

    for data in results:
        found = [r for r in data.get("results", []) if r.get("found")]
        wallets = data.get("all_wallets") or []
        platforms = list(dict.fromkeys(r.get("platform", "") for r in found if r.get("platform")))
        matched = list(dict.fromkeys(str(r.get("matched") or "") for r in found if r.get("matched")))
        errors = data.get("diagnostics", {}).get("errors", [])
        row = [
            data.get("username", ""),
            data.get("found_count", 0),
            " ".join(wallets),
            " | ".join(platforms),
            " | ".join(matched),
            int(bool(data.get("cache_hit"))),
            data.get("diagnostics", {}).get("elapsed_ms", 0),
            " | ".join(f"{e.get('platform')}: {e.get('error')}" for e in errors),
        ]
        if show_balance:
            bal_parts, tok_parts, chain_parts = [], [], []
            for w in wallets:
                info = balances.get(w) or {}
                usd = info.get("balance_usd")
                if isinstance(usd, (int, float)):
                    bal_parts.append(f"{w}=${usd:,.2f}")
                else:
                    bal_parts.append(f"{w}={info.get('note') or 'n/a'}")
                if info.get("top_tokens"):
                    tok_parts.append(f"{_short(w)}: {', '.join(info['top_tokens'][:5])}")
                if info.get("chains"):
                    chain_parts.append(f"{_short(w)}: {','.join(info['chains'])}")
            row[3:3] = [" | ".join(bal_parts), " | ".join(tok_parts), " | ".join(chain_parts)]
        writer.writerow(row)
    return output.getvalue().encode("utf-8-sig")


async def run_bulk_job(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, usernames: list[str], status_message_id: int):
    started = time.perf_counter()
    order = {username: idx for idx, username in enumerate(usernames)}
    results: list[dict] = []
    processed = 0
    found_count = 0
    lock = asyncio.Lock()
    worker_sem = asyncio.Semaphore(BULK_WORKERS)
    last_update = 0.0

    async def update_progress(force: bool = False):
        nonlocal last_update
        now = time.time()
        if not force and now - last_update < 10:
            return
        last_update = now
        elapsed = max(1, int(time.perf_counter() - started))
        speed = processed / elapsed
        left = len(usernames) - processed
        eta = int(left / speed) if speed > 0 else 0
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message_id,
                text=(
                    f"📦 Bulk search\n\n"
                    f"Проверено: <b>{processed}/{len(usernames)}</b>\n"
                    f"Найдено: <b>{found_count}</b>\n"
                    f"Скорость: <b>{speed:.2f} users/sec</b>\n"
                    f"ETA: <b>{eta // 60}м {eta % 60}с</b>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            log.debug("Failed to update bulk progress", exc_info=True)

    async def scan_one(username: str):
        nonlocal processed, found_count
        async with worker_sem:
            try:
                data = await run_bulk_search(username)
            except Exception as exc:
                data = {
                    "username": username,
                    "found_count": 0,
                    "all_wallets": [],
                    "results": [],
                    "diagnostics": {"elapsed_ms": 0, "errors": [{"platform": "bulk", "error": str(exc)[:120]}]},
                    "cache_hit": False,
                }
            async with lock:
                results.append(data)
                processed += 1
                if data.get("found_count", 0) > 0:
                    found_count += 1
                if processed % BULK_PROGRESS_STEP == 0 or processed == len(usernames):
                    await update_progress(force=True)

    _active_bulk_users.add(user_id)
    try:
        async with _bulk_slots:
            await update_progress(force=True)
            await asyncio.gather(*(scan_one(username) for username in usernames))
            results.sort(key=lambda item: order.get(item.get("username", ""), 10**9))

            # ── Балансы найденных кошельков (GoldRush, USD) ──────────────────
            balances: dict = {}
            if GOLDRUSH_API_KEY:
                all_wallets = list(dict.fromkeys(
                    w for data in results for w in (data.get("all_wallets") or [])
                ))
                if all_wallets:
                    try:
                        await ctx.bot.edit_message_text(
                            chat_id=chat_id, message_id=status_message_id,
                            text=(f"📦 Bulk search\n\n"
                                  f"Проверено: <b>{len(usernames)}/{len(usernames)}</b>\n"
                                  f"Найдено: <b>{found_count}</b>\n"
                                  f"💰 Считаю балансы кошельков: <b>{len(all_wallets)}</b>..."),
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                    balances = await enrich_balances(all_wallets)

            csv_bytes = bulk_result_to_csv(results, balances)
            elapsed = int(time.perf_counter() - started)

            if GOLDRUSH_API_KEY:
                wallets_priced = sum(
                    1 for b in balances.values()
                    if isinstance(b.get("balance_usd"), (int, float))
                )
                balance_line = f"\n💰 Балансы посчитаны: {wallets_priced} кошельков (точные цифры в CSV)"
            else:
                balance_line = "\n💡 Балансы выключены (нет GOLDRUSH_API_KEY)"

            await ctx.bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(csv_bytes), filename="bulk_results.csv"),
                caption=(
                    f"✅ Bulk готов\n"
                    f"Строк: {len(usernames)}\n"
                    f"С найденными кошельками: {found_count}\n"
                    f"Время: {elapsed // 60}м {elapsed % 60}с"
                    f"{balance_line}"
                ),
            )
            await update_progress(force=True)
    finally:
        _active_bulk_users.discard(user_id)


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    doc = update.message.document
    await get_or_create_user(uid, uname)

    if not doc:
        return
    filename = doc.file_name or "users.txt"
    if not filename.lower().endswith((".txt", ".csv")):
        await update.message.reply_text("Пришли .txt или .csv файл со списком usernames.")
        return
    if doc.file_size and doc.file_size > BULK_MAX_FILE_BYTES:
        await update.message.reply_text(
            f"Файл слишком большой. Лимит: {BULK_MAX_FILE_BYTES // 1024 // 1024} MB."
        )
        return

    if not is_admin(uid):
        allowed = (uid in BULK_USER_IDS) or await check_bulk_access(uid)
        if not allowed:
            await update.message.reply_text(
                "🔒 <b>Bulk по файлу</b> — отдельная платная функция.\n"
                "Оформи доступ, чтобы сканить списки пачкой.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Купить Bulk", callback_data="bulk_buy")],
                    [InlineKeyboardButton("🆘 Поддержка",   url=f"https://t.me/{SUPPORT_USER}")],
                ]))
            return

    if uid in _active_bulk_users:
        await update.message.reply_text("У тебя уже идёт bulk-задача. Дождись CSV-результата.")
        return
    if _bulk_slots.locked():
        await update.message.reply_text("Сейчас bulk-слоты заняты. Попробуй позже.")
        return

    file = await doc.get_file()
    buffer = io.BytesIO()
    await file.download_to_memory(out=buffer)
    text = buffer.getvalue().decode("utf-8-sig", errors="ignore")
    usernames, total_lines = parse_bulk_usernames(text)

    if not usernames:
        await update.message.reply_text("Не нашёл usernames в файле. Формат: по одному @username на строку.")
        return
    if total_lines > MAX_BULK_LINES and len(usernames) >= MAX_BULK_LINES:
        note = f"\n\n⚠️ Взял первые {MAX_BULK_LINES} уникальных usernames."
    else:
        note = ""

    # Списываем один прогон. Админ и allowlist — безлимит.
    unlimited = is_admin(uid) or (uid in BULK_USER_IDS)
    remaining = None
    if not unlimited:
        remaining = await consume_bulk_credit(uid)
        if remaining is None:
            await update.message.reply_text(
                "🔒 У тебя закончились bulk-поиски.\nДокупи пакет, чтобы продолжить.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Купить Bulk", callback_data="bulk_buy")],
                ]))
            return
    credit_line = "" if unlimited else f"\nОсталось поисков: <b>{remaining}</b>"

    status = await update.message.reply_text(
        f"📦 Bulk search принят\n\n"
        f"Строк в файле: <b>{total_lines}</b>\n"
        f"Уникальных usernames: <b>{len(usernames)}</b>\n"
        f"Workers: <b>{BULK_WORKERS}</b>{credit_line}{note}",
        parse_mode=ParseMode.HTML,
    )
    ctx.application.create_task(run_bulk_job(ctx, update.effective_chat.id, uid, usernames, status.message_id))


# ─── Текстовые сообщения ──────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    raw   = update.message.text.strip()
    if raw.startswith("/"):
        return

    await get_or_create_user(uid, uname)

    # Проверка подписки на канал (кроме админов)
    if not is_admin(uid) and REQUIRED_CHANNEL:
        subscribed = await is_subscribed(ctx.bot, uid)
        if not subscribed:
            await update.message.reply_text(
                "<b>📢 Для использования бота подпишись на канал</b>\n\n"
                "После подписки нажми кнопку ниже.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_subscribe())
            return

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

    # ── Пользователь присылает хеш для Bulk ───────────────────────────────────
    pending_bulk = await get_pending_bulk(uid)
    if pending_bulk and pending_bulk.get("status") == "awaiting_hash":
        req = await submit_bulk_hash(uid, raw)
        if not req:
            await update.message.reply_text(
                "❌ Активная заявка на Bulk не найдена. Выбери тариф заново.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_bulk_plans())
            return

        plan     = BULK_PLANS.get(req.get("plan_id"), {})
        net_info = NETWORK_INFO.get(req.get("network"), {})
        await notify_admins(
            ctx.bot,
            f"📦 <b>Новая оплата Bulk!</b>\n\n"
            f"Пользователь: @{esc(uname)} (ID: <code>{uid}</code>)\n"
            f"Тариф: <b>{plan.get('name','?')}</b> — ${plan.get('price','?')}\n"
            f"Сеть: {net_info.get('emoji','')} <b>{net_info.get('name','?')}</b>\n\n"
            f"Заявка: <code>#{req['id']}</code>\n"
            f"Хеш транзакции:\n<code>{esc(raw)}</code>\n\n"
            f"Подтвердить: /confirmbulk {uid}",
            parse_mode=ParseMode.HTML)

        await update.message.reply_text(
            "✅ <b>Хеш получен!</b>\n\n"
            "Проверяем транзакцию — обычно до 10 минут.\n"
            "Как подтвердим, доступ к Bulk включится автоматически.\n\n"
            f"Вопросы? @{SUPPORT_USER}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin=is_admin(uid)))
        return

    # ── Проверка доступа ──────────────────────────────────────────────────────
    if not is_admin(uid):
        # Rate limit — не больше 5 поисков в минуту
        if not check_rate_limit(uid):
            await update.message.reply_text(
                "⏳ Слишком много запросов. Подожди минуту и попробуй снова.",
                parse_mode=ParseMode.HTML)
            return

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
        balances = await enrich_balances(data["all_wallets"]) if (GOLDRUSH_API_KEY and data.get("all_wallets")) else {}
        text    = await fmt_search(data, balances)
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


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выдать обычный доступ вручную: /grant USER_ID DAYS"""
    if not is_admin(update.effective_user.id):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Формат: /grant USER_ID DAYS\n\n"
            "Пример: /grant 123456789 30"
        )
        return
    try:
        user_id = int(ctx.args[0])
        days = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("USER_ID и DAYS должны быть числами.")
        return
    if days <= 0 or days > 36500:
        await update.message.reply_text("DAYS должен быть от 1 до 36500.")
        return

    granted = await grant_subscription(user_id, days)
    expires = granted["expires"].strftime("%d.%m.%Y %H:%M")
    key = granted["key"]

    await update.message.reply_text(
        f"✅ Доступ выдан.\n\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Дней: <b>{days}</b>\n"
        f"До: <b>{expires}</b>\n"
        f"Ключ: <code>{key}</code>",
        parse_mode=ParseMode.HTML,
    )
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Доступ активирован!</b>\n\n"
            f"Твой ключ:\n<code>{key}</code>\n\n"
            f"Подписка активна до <b>{expires}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(),
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ Доступ выдан, но не смог отправить сообщение пользователю. "
            "Скорее всего, он ещё не писал боту."
        )


async def cmd_confirmbulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подтвердить bulk-оплату: /confirmbulk USER_ID"""
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /confirmbulk 123456789")
        return
    try:
        user_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    res = await confirm_bulk_payment(user_id)
    if not res:
        await update.message.reply_text(
            "❌ Не нашёл отправленную bulk-заявку с хешем для этого пользователя.")
        return

    await update.message.reply_text(
        f"✅ Bulk подтверждён для <code>{user_id}</code>.\n"
        f"Начислено: <b>{res['credits']}</b> поисков. Всего: <b>{res['total']}</b>",
        parse_mode=ParseMode.HTML)
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Оплата Bulk подтверждена!</b>\n\n"
            f"Начислено поисков: <b>{res['credits']}</b>. Всего доступно: <b>{res['total']}</b>.\n"
            f"Пришли .txt/.csv со списком @username, один файл = один поиск.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
    except Exception:
        pass


async def cmd_grantbulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Начислить bulk-поиски вручную: /grantbulk USER_ID COUNT"""
    if not is_admin(update.effective_user.id):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Формат: /grantbulk USER_ID COUNT")
        return
    try:
        user_id, count = int(ctx.args[0]), int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("USER_ID и COUNT должны быть числами.")
        return
    total = await grant_bulk_access(user_id, count)
    await update.message.reply_text(
        f"✅ Начислено <b>{count}</b> поисков <code>{user_id}</code>. Всего: <b>{total}</b>.",
        parse_mode=ParseMode.HTML)
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ Тебе начислили <b>{count}</b> bulk-поисков. Всего доступно: <b>{total}</b>.",
            parse_mode=ParseMode.HTML, reply_markup=kb_main())
    except Exception:
        pass


async def cmd_revokebulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /revokebulk 123456789")
        return
    ok = await revoke_bulk_access(int(ctx.args[0]))
    await update.message.reply_text("✅ Bulk-доступ снят." if ok else "❌ У пользователя не было bulk-доступа.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

# ─── Рассылка (только для админа) ─────────────────────────────────────────────

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Рассылка всем пользователям бота.
    Использование: /broadcast текст сообщения (поддерживает HTML)
    """
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    text = update.message.text.replace("/broadcast", "", 1).strip()
    if not text:
        await update.message.reply_text(
            "Использование:\n<code>/broadcast текст сообщения</code>\n\n"
            "Поддерживает HTML: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;",
            parse_mode=ParseMode.HTML)
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
    user_ids = [r["user_id"] for r in rows]

    status_msg = await update.message.reply_text(
        f"📤 Начинаю рассылку на {len(user_ids)} пользователей...")

    sent, failed = 0, 0
    for user_id in user_ids:
        try:
            await ctx.bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ Рассылка завершена\n\nОтправлено: {sent}\nОшибок: {failed}")


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
    app.add_handler(CommandHandler("grant",   cmd_grant))
    app.add_handler(CommandHandler("revoke",  cmd_revoke))
    app.add_handler(CommandHandler("block",   cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("confirmbulk", cmd_confirmbulk))
    app.add_handler(CommandHandler("grantbulk",   cmd_grantbulk))
    app.add_handler(CommandHandler("revokebulk",  cmd_revokebulk))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    print(f"✅ Бот запущен. Админы: {', '.join(map(str, ADMIN_IDS))}", flush=True)
    print("=== CODE VERSION: BULK-v2 (меню + bulk оплата через payments) ===", flush=True)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
