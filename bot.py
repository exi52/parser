"""
Crypto OSINT Bot — тарифы, хеш транзы, поддержка
"""

import asyncio, logging, os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes)
from telegram.constants import ParseMode

from searcher import extract_username, run_search, reverse_lookup, is_eth_address, get_variants, enrich_balances
from database import consume_rate_limit, init_db, close_pool, get_pool
from access  import (
    get_or_create_user, check_access, use_search, get_user_stats,
    activate_ref, get_referrals, list_all_refs,
    block_user, unblock_user, revoke_subscription,
    create_payment, confirm_payment, submit_payment_hash,
    get_admin_stats,
    grant_subscription, grant_trial_searches,
    start_payment_hash, get_pending_payment,
    get_bulk_status, grant_bulk_access, revoke_bulk_access,
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
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()

RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "5"))
async def check_rate_limit(user_id: int) -> bool:
    """Shared rate limit that works across Railway replicas and restarts."""
    return await consume_rate_limit(user_id, "bot:search", RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)


def search_failed_technically(data: dict) -> bool:
    diagnostics = data.get("diagnostics") or {}
    checked = int(diagnostics.get("platforms_checked") or 0)
    errors = diagnostics.get("errors") or []
    return not data.get("found_count") and (checked <= 0 or len(errors) >= checked)


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


def valid_webapp_url(url: str) -> bool:
    return bool(url and url.startswith("https://") and " " not in url)


async def notify_admins(bot, text: str, **kwargs):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, **kwargs)
        except Exception:
            log.exception("Failed to notify admin %s", admin_id)


async def log_admin_action(ctx: ContextTypes.DEFAULT_TYPE, actor_id: int, action: str):
    await notify_admins(
        ctx.bot,
        f"🛡 <b>Admin action</b>\n"
        f"Admin: <code>{actor_id}</code>\n"
        f"Action: {action}",
        parse_mode=ParseMode.HTML,
    )

# ── Тарифы ────────────────────────────────────────────────────────────────────
PLANS = {
    "plan_1w":  {"name": "1 неделя",  "price": 5,  "days": 7},
    "plan_1m":  {"name": "1 месяц",   "price": 15, "days": 30},
    "plan_3m":  {"name": "3 месяца",  "price": 35, "days": 90},
    "plan_life":{"name": "Навсегда",  "price": 79, "days": 36500},
}

# ── Bulk (отдельная платная функция, оплата за количество поисков) ─────────────
BULK_PLANS = {
    "bulk_1":  {"name": "1 запуск",    "price": 4,  "credits": 1},
    "bulk_5":  {"name": "5 запусков",  "price": 15, "credits": 5},
    "bulk_20": {"name": "20 запусков", "price": 49, "credits": 20},
}

def esc(t):
    return str(t).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


# ─── Клавиатуры ───────────────────────────────────────────────────────────────

BUTTON_EMOJI = {
    "myref": "5062404628113524215",
    "referrals": "5062138387385813889",
    "buy": "5062106574563052023",
    "bulk_buy": "5089326088715241089",
    "bulk_web": "5062163272426325775",
    "stats": "5062159634589027240",
    "support": "5062249249081657127",
}

TITLE_EMOJI = '<tg-emoji emoji-id="5089259641276205618">🔍</tg-emoji>'
ACTIVE_SUB_EMOJI = '<tg-emoji emoji-id="5089626255389623979">✅</tg-emoji>'
TRIAL_ACCESS_EMOJI = '<tg-emoji emoji-id="5089366616026645907">🎟️</tg-emoji>'
INACTIVE_SUB_EMOJI = '<tg-emoji emoji-id="5089370193734403650">❌</tg-emoji>'
BLOCKED_ACCOUNT_EMOJI = '<tg-emoji emoji-id="5089249290405021457">⛔</tg-emoji>'
REF_LINK_EMOJI = '<tg-emoji emoji-id="5062404628113524215">🔗</tg-emoji>'
REFERRALS_EMOJI = '<tg-emoji emoji-id="5062138387385813889">👥</tg-emoji>'
STATS_EMOJI = '<tg-emoji emoji-id="5062159634589027240">📊</tg-emoji>'

def kb_main(is_admin=False):
    bulk_web_row = []
    if valid_webapp_url(MINIAPP_URL):
        bulk_web_row = [InlineKeyboardButton(
            "Bulk Web App",
            web_app=WebAppInfo(url=MINIAPP_URL),
            icon_custom_emoji_id=BUTTON_EMOJI["bulk_web"],
        )]

    if is_admin:
        rows = [
            [InlineKeyboardButton(
                "Рефералы",
                callback_data="admin_refs",
                icon_custom_emoji_id=BUTTON_EMOJI["referrals"],
            )],
            [InlineKeyboardButton(
                "Купить Bulk",
                callback_data="bulk_buy",
                icon_custom_emoji_id=BUTTON_EMOJI["bulk_buy"],
            )],
        ]
        if bulk_web_row:
            rows.append(bulk_web_row)
        rows.extend([
            [InlineKeyboardButton(
                "Моя реф-ссылка",
                callback_data="myref",
                icon_custom_emoji_id=BUTTON_EMOJI["myref"],
            )],
            [InlineKeyboardButton(
                "Купить подписку",
                callback_data="buy",
                icon_custom_emoji_id=BUTTON_EMOJI["buy"],
            )],
            [InlineKeyboardButton(
                "Поддержка",
                url=f"https://t.me/{SUPPORT_USER}",
                icon_custom_emoji_id=BUTTON_EMOJI["support"],
            )],
        ])
        return InlineKeyboardMarkup(rows)

    rows = [
        [InlineKeyboardButton(
            "Моя реф-ссылка",
            callback_data="myref",
            icon_custom_emoji_id=BUTTON_EMOJI["myref"],
        )],
        [InlineKeyboardButton(
            "Мои рефералы",
            callback_data="my_referrals",
            icon_custom_emoji_id=BUTTON_EMOJI["referrals"],
        )],
        [InlineKeyboardButton(
            "Купить подписку",
            callback_data="buy",
            icon_custom_emoji_id=BUTTON_EMOJI["buy"],
        )],
        [InlineKeyboardButton(
            "Купить Bulk",
            callback_data="bulk_buy",
            icon_custom_emoji_id=BUTTON_EMOJI["bulk_buy"],
        )],
    ]
    if bulk_web_row:
        rows.append(bulk_web_row)
    rows.extend([
        [InlineKeyboardButton(
            "Статистика",
            callback_data="stats",
            icon_custom_emoji_id=BUTTON_EMOJI["stats"],
        )],
        [InlineKeyboardButton(
            "Поддержка",
            url=f"https://t.me/{SUPPORT_USER}",
            icon_custom_emoji_id=BUTTON_EMOJI["support"],
        )],
    ])
    return InlineKeyboardMarkup(rows)


def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Назад", callback_data="back_main")]
    ])


def kb_bulk_web():
    rows = []
    if valid_webapp_url(MINIAPP_URL):
        rows.append([InlineKeyboardButton("📊 Открыть Bulk Web App", web_app=WebAppInfo(url=MINIAPP_URL))])
    rows.append([InlineKeyboardButton("🛒 Купить Bulk", callback_data="bulk_buy")])
    rows.append([InlineKeyboardButton("← Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


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
        [InlineKeyboardButton(f"🔴 USDT TRC-20 — ${p.get('price',0)}",      callback_data=f"pay_usdt_trc20_{plan_id}")],
        [InlineKeyboardButton(f"🟡 USDT BEP-20 — ${p.get('price',0)}",      callback_data=f"pay_usdt_bep20_{plan_id}")],
        [InlineKeyboardButton(f"🔵 USDC SOL    — ${p.get('price',0)}",      callback_data=f"pay_usdc_sol_{plan_id}")],
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
        elif len(w) > 30:
            btns.append([
                InlineKeyboardButton("Solscan", url=f"https://solscan.io/account/{w}"),
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
        ("usdt_trc20", f"🔴 USDT TRC-20 — ${price}"),
        ("usdt_bep20", f"🟡 USDT BEP-20 — ${price}"),
        ("usdc_sol",   f"🔵 USDC SOL    — ${price}"),
        ("sol",        f"🟣 SOL         — ${price}"),
        ("ton",        f"💎 TON         — ${price}"),
    ]
    rows = [[InlineKeyboardButton(label, callback_data=f"bulkpay:{net}:{plan_id}")]
            for net, label in nets]
    rows.append([InlineKeyboardButton("← Другой тариф", callback_data="bulk_buy")])
    rows.append([InlineKeyboardButton("← Назад",         callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


# ─── Форматирование поиска ────────────────────────────────────────────────────

async def fmt_search(
    data: dict,
    balances: dict | None = None,
    show_balance_error: bool = False,
) -> str:
    balances = balances or {}

    def _bal_line(w: str):
        info = balances.get(w)
        if not info:
            return None
        bits = []
        usd = info.get("balance_usd")
        if isinstance(usd, (int, float)):
            bits.append(f"💰 ${usd:,.2f}")
        if info.get("top_tokens"):
            bits.append(", ".join(info["top_tokens"][:3]))
        if info.get("chains"):
            bits.append(", ".join(info["chains"]))
        if not bits and info.get("note") not in (None, "", "empty_solana"):
            detail = f" <code>{esc(info['note'])}</code>" if show_balance_error else ""
            return f"⚠️ баланс временно недоступен{detail}"
        if not bits:
            return None
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

    # /start REF...
    if ctx.args and ctx.args[0].strip().upper().startswith("REF"):
        ref      = ctx.args[0].strip().upper()
        ok, msg  = await activate_ref(uid, uname, ref)
        subscribed = True
        if not is_admin(uid) and REQUIRED_CHANNEL:
            subscribed = await is_subscribed(ctx.bot, uid)
        await update.message.reply_text(
            msg if subscribed else (
                f"{msg}\n\n"
                "<b>📢 Для использования бота подпишись на канал</b>\n"
                "После подписки нажми кнопку ниже."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main() if ok and subscribed else (kb_subscribe() if ok else InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Купить подписку", callback_data="buy")],
                [InlineKeyboardButton("🆘 Поддержка", url=f"https://t.me/{SUPPORT_USER}")],
            ]))
        )
        if ok:
            await notify_admins(
                ctx.bot,
                f"👥 Новый реферал:\n@{esc(uname)} (ID: <code>{uid}</code>)\n"
                f"Реф: <code>{ref}</code>",
                parse_mode=ParseMode.HTML)
        return

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
            status = f"{ACTIVE_SUB_EMOJI} Подписка активна до {stats.get('sub_exp')}"
        else:
            status = f"{TRIAL_ACCESS_EMOJI} Пробный доступ: {stats.get('trial_left',0)} поисков до {stats.get('trial_exp','')}"
    else:
        status = {
            "no_sub":       f"{INACTIVE_SUB_EMOJI} Нет подписки",
            "trial_expired":f"{INACTIVE_SUB_EMOJI} Пробный период истёк",
            "sub_expired":  f"{INACTIVE_SUB_EMOJI} Подписка истекла",
            "blocked":      f"{BLOCKED_ACCOUNT_EMOJI} Аккаунт заблокирован",
        }.get(reason, f"{INACTIVE_SUB_EMOJI} Нет доступа")

    await update.message.reply_text(
        f"<b>{TITLE_EMOJI} Crypto OSINT Bot · BULK-v2</b>\n\n"
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
            status = f"{ACTIVE_SUB_EMOJI} Подписка до {stats.get('sub_exp')}" if stats.get("sub_active") else f"{TRIAL_ACCESS_EMOJI} Пробный: {stats.get('trial_left',0)} поисков"
        else:
            status = {
                "blocked": f"{BLOCKED_ACCOUNT_EMOJI} Аккаунт заблокирован",
                "sub_expired": f"{INACTIVE_SUB_EMOJI} Подписка истекла",
                "trial_expired": f"{INACTIVE_SUB_EMOJI} Пробный период истёк",
            }.get(reason, f"{INACTIVE_SUB_EMOJI} Нет подписки")
        heading = "👑 Панель администратора" if admin else f"{TITLE_EMOJI} Crypto OSINT Bot"
        await q.edit_message_text(
            f"<b>{heading} · BULK-v2</b>"
            + (f"\n\nСтатус: {status}" if not admin else ""),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin=admin))
        return

    # ── Статистика ────────────────────────────────────────────────────────────
    if data == "stats":
        stats = await get_user_stats(uid)
        text  = f"<b>{STATS_EMOJI} Статистика</b>\n\n"
        subscription_status = (
            f"{ACTIVE_SUB_EMOJI} Активна до {stats.get('sub_exp', '')}"
            if stats.get("sub_active")
            else f"{INACTIVE_SUB_EMOJI} Нет"
        )
        text += f"Подписка: {subscription_status}\n"
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
            f"<b>{REF_LINK_EMOJI} Твоя реф-ссылка:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"Друг получит {TRIAL_SEARCHES} пробных поисков на {TRIAL_HOURS}ч.\n"
            f"Приглашено: <b>{stats.get('ref_count', 0)}</b> чел.",
            parse_mode=ParseMode.HTML, reply_markup=kb_back())
        return

    # ── Мои рефералы ──────────────────────────────────────────────────────────
    if data == "my_referrals":
        refs = await get_referrals(uid)
        if not refs:
            text = f"<b>{REFERRALS_EMOJI} Мои рефералы</b>\n\nПока никого нет.\nПоделись реф-ссылкой!"
        else:
            lines = [f"<b>{REFERRALS_EMOJI} Мои рефералы ({len(refs)}):</b>\n"]
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
        # Формат: pay_usdt_trc20_plan_1m или pay_usdc_sol_plan_1w
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
            f"<b>{TITLE_EMOJI} Crypto OSINT Bot · BULK-v2</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
        return

    # ── Bulk: инфо / статус ───────────────────────────────────────────────────
    if data == "bulk_info":
        status = await get_bulk_status(uid)
        await q.edit_message_text(
            "<b>📊 Bulk Search теперь работает через Web App</b>\n\n"
            f"Осталось bulk-поисков: <b>{status['credits']}</b>\n\n"
            "Открой Web App, загрузи <code>.txt</code>/<code>.csv</code> со списком "
            "<code>@username</code> и забери результат в удобной таблице.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_bulk_web())
        return

    # ── Bulk: выбор тарифа ────────────────────────────────────────────────────
    if data == "bulk_buy":
        await q.edit_message_text(
            "<b>📊 Bulk Web App — выбери тариф</b>\n\n"
            "После оплаты bulk-поиски начислятся на аккаунт, а запускать их можно в Web App:",
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
            "Мы проверим оплату и начислим bulk-поиски для Web App.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Отмена", callback_data="bulk_cancel_hash")]
            ]))
        return

    if data == "bulk_cancel_hash":
        await q.edit_message_text(
            f"<b>{TITLE_EMOJI} Crypto OSINT Bot · BULK-v2</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin=is_admin(uid)))
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


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    await get_or_create_user(uid, uname)

    await update.message.reply_text(
        "<b>📊 Bulk Search теперь только в Web App</b>\n\n"
        "Файлы <code>.txt</code>/<code>.csv</code> больше не обрабатываются прямо в чате.\n"
        "Открой Web App, загрузи список там и смотри результат в таблице.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb_bulk_web())


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
        if not await check_rate_limit(uid):
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
            technical_failure = search_failed_technically(data)
            text = await fmt_reverse(data)
            if technical_failure:
                text += "\n\n<i>Источники временно недоступны — поиск не списан.</i>"
            if len(text) > 4000:
                text = text[:3900] + "\n<i>...обрезано</i>"
            await msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            if not is_admin(uid) and not technical_failure:
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
        technical_failure = search_failed_technically(data)
        balances = await enrich_balances(data["all_wallets"]) if data.get("all_wallets") else {}
        text    = await fmt_search(data, balances, show_balance_error=is_admin(uid))
        if technical_failure:
            text += "\n\n<i>Источники временно недоступны — поиск не списан.</i>"
        buttons = wallet_buttons(data["all_wallets"])
        if len(text) > 4000:
            text = text[:3900] + "\n<i>...обрезано</i>"
        await msg.edit_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=buttons, disable_web_page_preview=True)
        if not is_admin(uid) and not technical_failure:
            await use_search(uid)
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

    expires = payment["expires"].strftime("%d.%m.%Y %H:%M")
    await update.message.reply_text(
        f"✅ Подтверждено.\n"
        f"Доступ до: <b>{expires}</b>",
        parse_mode=ParseMode.HTML)
    await log_admin_action(ctx, update.effective_user.id, f"confirmed subscription for <code>{user_id}</code> until <b>{expires}</b>")
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Доступ активирован до <b>{expires}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main())
    except Exception:
        pass


async def cmd_adminstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show admin dashboard numbers: users, paid users and revenue."""
    if not is_admin(update.effective_user.id):
        return

    stats = await get_admin_stats()

    def money(value) -> str:
        return f"${float(value or 0):,.2f}"

    await update.message.reply_text(
        "<b>📊 Admin stats</b>\n\n"
        "<b>Users</b>\n"
        f"Всего: <b>{stats.get('users_total', 0)}</b>\n"
        f"Активные подписки: <b>{stats.get('active_subscriptions', 0)}</b>\n"
        f"Активный trial: <b>{stats.get('active_trials', 0)}</b>\n"
        f"Bulk credits active: <b>{stats.get('bulk_active_users', 0)}</b>\n"
        f"Заблокированы: <b>{stats.get('users_blocked', 0)}</b>\n\n"
        "<b>Payments</b>\n"
        f"Платящих всего: <b>{stats.get('paying_users_total', 0)}</b>\n"
        f"Платящих за месяц: <b>{stats.get('paying_users_month', 0)}</b>\n"
        f"Оплат всего: <b>{stats.get('payments_total', 0)}</b>\n"
        f"Оплат за месяц: <b>{stats.get('payments_month', 0)}</b>\n\n"
        "<b>Revenue</b>\n"
        f"Этот месяц: <b>{money(stats.get('revenue_month'))}</b>\n"
        f"За все время: <b>{money(stats.get('revenue_total'))}</b>\n"
        f"Подписки месяц/все время: <b>{money(stats.get('sub_revenue_month'))}</b> / "
        f"<b>{money(stats.get('sub_revenue_total'))}</b>\n"
        f"Bulk месяц/все время: <b>{money(stats.get('bulk_revenue_month'))}</b> / "
        f"<b>{money(stats.get('bulk_revenue_total'))}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /revoke 123456789")
        return
    try:
        user_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /revoke 123456789")
        return
    ok = await revoke_subscription(user_id)
    await update.message.reply_text("✅ Доступ снят." if ok else "❌ Пользователь не найден.")
    if ok:
        await log_admin_action(ctx, update.effective_user.id, f"revoked subscription for <code>{user_id}</code>")


async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /block 123456789")
        return
    try:
        user_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /block 123456789")
        return
    await block_user(user_id)
    await update.message.reply_text("✅ Заблокирован.")
    await log_admin_action(ctx, update.effective_user.id, f"blocked user <code>{user_id}</code>")


async def cmd_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /unblock 123456789")
        return
    try:
        user_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /unblock 123456789")
        return
    await unblock_user(user_id)
    await update.message.reply_text("✅ Разблокирован.")
    await log_admin_action(ctx, update.effective_user.id, f"unblocked user <code>{user_id}</code>")


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
    await update.message.reply_text(
        f"✅ Доступ выдан.\n\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Дней: <b>{days}</b>\n"
        f"До: <b>{expires}</b>",
        parse_mode=ParseMode.HTML,
    )
    await log_admin_action(ctx, update.effective_user.id, f"granted subscription to <code>{user_id}</code> for <b>{days}</b> days")
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Доступ активирован!</b>\n\n"
            f"Подписка активна до <b>{expires}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(),
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ Доступ выдан, но не смог отправить сообщение пользователю. "
            "Скорее всего, он ещё не писал боту."
        )


async def cmd_granttrial(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Выдать пробные поиски вручную: /granttrial USER_ID COUNT [HOURS]"""
    if not is_admin(update.effective_user.id):
        return
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Формат: /granttrial USER_ID COUNT [HOURS]\n\n"
            "Пример: /granttrial 123456789 10 24"
        )
        return
    try:
        user_id = int(ctx.args[0])
        count = int(ctx.args[1])
        hours = int(ctx.args[2]) if len(ctx.args) >= 3 else TRIAL_HOURS
    except ValueError:
        await update.message.reply_text("USER_ID, COUNT и HOURS должны быть числами.")
        return
    if count <= 0 or count > 10000:
        await update.message.reply_text("COUNT должен быть от 1 до 10000.")
        return
    if hours <= 0 or hours > 8760:
        await update.message.reply_text("HOURS должен быть от 1 до 8760.")
        return

    granted = await grant_trial_searches(user_id, count, hours)
    expires = granted["expires"].strftime("%d.%m.%Y %H:%M")
    await update.message.reply_text(
        f"✅ Пробные поиски выданы.\n\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Поисков: <b>{count}</b>\n"
        f"До: <b>{expires}</b>",
        parse_mode=ParseMode.HTML,
    )
    await log_admin_action(ctx, update.effective_user.id, f"granted trial to <code>{user_id}</code>: <b>{count}</b> searches for <b>{hours}</b> hours")
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Тебе выдали пробный доступ</b>\n\n"
            f"Поисков: <b>{count}</b>\n"
            f"Доступно до <b>{expires}</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(),
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ Trial выдан, но не смог отправить сообщение пользователю. "
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
    await log_admin_action(ctx, update.effective_user.id, f"confirmed bulk payment for <code>{user_id}</code>, credits <b>{res['credits']}</b>")
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ <b>Оплата Bulk подтверждена!</b>\n\n"
            f"Начислено поисков: <b>{res['credits']}</b>. Всего доступно: <b>{res['total']}</b>.\n"
            f"Открой Bulk Web App, загрузи .txt/.csv со списком @username и запусти поиск там.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_bulk_web())
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
    await log_admin_action(ctx, update.effective_user.id, f"granted bulk credits to <code>{user_id}</code>: <b>{count}</b>")
    try:
        await ctx.bot.send_message(
            user_id,
            f"✅ Тебе начислили <b>{count}</b> bulk-поисков. Всего доступно: <b>{total}</b>.\n\n"
            f"Запуск bulk теперь доступен через Web App.",
            parse_mode=ParseMode.HTML, reply_markup=kb_bulk_web())
    except Exception:
        pass


async def cmd_revokebulk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Укажи ID: /revokebulk 123456789")
        return
    try:
        user_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /revokebulk 123456789")
        return
    ok = await revoke_bulk_access(user_id)
    await update.message.reply_text("✅ Bulk-доступ снят." if ok else "❌ У пользователя не было bulk-доступа.")
    if ok:
        await log_admin_action(ctx, update.effective_user.id, f"revoked bulk access for <code>{user_id}</code>")


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
    await log_admin_action(ctx, uid, f"broadcast sent to <b>{sent}</b> users, failed <b>{failed}</b>")


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
    app.add_handler(CommandHandler("adminstats", cmd_adminstats))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("grant",   cmd_grant))
    app.add_handler(CommandHandler("granttrial", cmd_granttrial))
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
