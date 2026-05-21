"""
Система доступа — PostgreSQL
"""
import os, secrets, string
from datetime import datetime, timedelta
from database import get_conn

TRIAL_SEARCHES = 10
TRIAL_HOURS    = 24
SUB_PRICE_USD  = 10

# ── Кошельки для оплаты — добавь свои в Railway Variables ─────────────────────
WALLETS = {
    "eth":        os.getenv("WALLET_ETH", "0xВСТАВЬ_ETH"),
    "usdt_erc20": os.getenv("WALLET_ETH", "0xВСТАВЬ_ETH"),  # тот же ETH адрес
    "usdt_trc20": os.getenv("WALLET_TRX", "TВСТАВЬ_TRX"),
    "usdt_bep20": os.getenv("WALLET_BNB", "0xВСТАВЬ_BNB"),
    "sol":        os.getenv("WALLET_SOL", "ВСТАВЬ_SOL"),
    "ton":        os.getenv("WALLET_TON", "ВСТАВЬ_TON"),
}

NETWORK_INFO = {
    "eth":        {"name": "ETH",         "symbol": "ETH",  "network": "Ethereum",  "emoji": "🔷"},
    "usdt_erc20": {"name": "USDT ERC-20", "symbol": "USDT", "network": "Ethereum",  "emoji": "💚"},
    "usdt_trc20": {"name": "USDT TRC-20", "symbol": "USDT", "network": "TRON",      "emoji": "🔴"},
    "usdt_bep20": {"name": "USDT BEP-20", "symbol": "USDT", "network": "BNB Chain", "emoji": "🟡"},
    "sol":        {"name": "SOL",         "symbol": "SOL",  "network": "Solana",    "emoji": "🟣"},
    "ton":        {"name": "TON",         "symbol": "TON",  "network": "TON",       "emoji": "💎"},
}

PAYMENT_ADDRESS = WALLETS["eth"]  # совместимость


def _gen_code(prefix="", length=10):
    chars = string.ascii_uppercase + string.digits
    return prefix + "".join(secrets.choice(chars) for _ in range(length))


# ─── Пользователи ─────────────────────────────────────────────────────────────

async def get_or_create_user(user_id: int, username: str) -> dict:
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if not user:
        ref_code = _gen_code("REF", 8)
        await conn.execute("""
            INSERT INTO users (user_id, username, active, ref_code)
            VALUES ($1, $2, FALSE, $3)
        """, user_id, username, ref_code)
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    await conn.close()
    return dict(user)


async def check_access(user_id: int) -> tuple[bool, str]:
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    await conn.close()
    if not user:
        return False, "no_sub"
    if user["active"] and user["key"]:
        conn = await get_conn()
        k = await conn.fetchrow("SELECT active FROM keys WHERE key=$1", user["key"])
        await conn.close()
        if k and k["active"]:
            return True, "ok"
    if user["trial_searches"] and user["trial_searches"] > 0 and user["trial_expires"]:
        if datetime.now() < user["trial_expires"]:
            return True, "trial_ok"
        return False, "trial_expired"
    return False, "no_sub"


async def use_search(user_id: int):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT active, key FROM users WHERE user_id=$1", user_id)
    if user and not (user["active"] and user["key"]):
        await conn.execute(
            "UPDATE users SET trial_searches=trial_searches-1 WHERE user_id=$1", user_id)
    await conn.close()


async def get_user_stats(user_id: int) -> dict:
    conn  = await get_conn()
    user  = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    refs  = await conn.fetch("SELECT COUNT(*) as cnt FROM referrals WHERE owner_id=$1", user_id)
    await conn.close()
    if not user:
        return {}
    trial_left = 0
    trial_exp  = None
    if user["trial_expires"] and datetime.now() < user["trial_expires"]:
        trial_left = max(0, user["trial_searches"] or 0)
        trial_exp  = user["trial_expires"].strftime("%d.%m %H:%M")
    return {
        "active":     user["active"],
        "has_key":    bool(user["key"]),
        "trial_left": trial_left,
        "trial_exp":  trial_exp,
        "ref_count":  refs[0]["cnt"] if refs else 0,
        "ref_code":   user["ref_code"],
    }


# ─── Рефералы ─────────────────────────────────────────────────────────────────

async def activate_ref(user_id: int, username: str, ref_code: str) -> tuple[bool, str]:
    conn  = await get_conn()
    user  = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    owner = await conn.fetchrow("SELECT * FROM users WHERE ref_code=$1", ref_code.upper())

    if not owner:
        await conn.close()
        return False, "❌ Реф-код не найден."
    if owner["user_id"] == user_id:
        await conn.close()
        return False, "❌ Нельзя использовать свой реф-код."
    if user and user["invited_by"]:
        await conn.close()
        return False, "❌ Ты уже активировал реф-код ранее."

    trial_exp = datetime.now() + timedelta(hours=TRIAL_HOURS)

    if not user:
        await conn.execute("""
            INSERT INTO users (user_id, username, active, trial_searches, trial_expires, invited_by, ref_code)
            VALUES ($1, $2, FALSE, $3, $4, $5, $6)
        """, user_id, username, TRIAL_SEARCHES, trial_exp, owner["user_id"], _gen_code("REF", 8))
    else:
        await conn.execute("""
            UPDATE users SET trial_searches=$1, trial_expires=$2, invited_by=$3, username=$4
            WHERE user_id=$5
        """, TRIAL_SEARCHES, trial_exp, owner["user_id"], username, user_id)

    exist = await conn.fetchrow(
        "SELECT id FROM referrals WHERE owner_id=$1 AND user_id=$2",
        owner["user_id"], user_id)
    if not exist:
        await conn.execute(
            "INSERT INTO referrals (owner_id, user_id, username) VALUES ($1,$2,$3)",
            owner["user_id"], user_id, username)

    await conn.close()
    return True, (
        f"✅ Реф-код активирован!\n\n"
        f"Тебе выдано <b>{TRIAL_SEARCHES} пробных поисков</b> на {TRIAL_HOURS} часов.\n"
        f"Пригласил тебя: @{owner['username'] or 'пользователь'}"
    )


async def get_referrals(user_id: int) -> list:
    conn = await get_conn()
    rows = await conn.fetch(
        "SELECT * FROM referrals WHERE owner_id=$1 ORDER BY created DESC", user_id)
    await conn.close()
    return [dict(r) for r in rows]


async def list_all_refs() -> list:
    conn = await get_conn()
    rows = await conn.fetch("""
        SELECT u.username, u.user_id, COUNT(r.id) as cnt
        FROM users u JOIN referrals r ON r.owner_id=u.user_id
        GROUP BY u.user_id, u.username ORDER BY cnt DESC
    """)
    result = []
    for row in rows:
        refs = await conn.fetch(
            "SELECT username, created FROM referrals WHERE owner_id=$1 LIMIT 5",
            row["user_id"])
        result.append({
            "username":  row["username"] or str(row["user_id"]),
            "user_id":   row["user_id"],
            "count":     row["cnt"],
            "referrals": [dict(r) for r in refs],
        })
    await conn.close()
    return result


# ─── Ключи ────────────────────────────────────────────────────────────────────

async def generate_key() -> str:
    key  = _gen_code("OSINT-", 12)
    conn = await get_conn()
    await conn.execute("INSERT INTO keys (key, active) VALUES ($1, TRUE)", key)
    await conn.close()
    return key


async def activate_key(user_id: int, username: str, key: str) -> tuple[bool, str]:
    key  = key.strip().upper()
    conn = await get_conn()
    k = await conn.fetchrow("SELECT * FROM keys WHERE key=$1", key)
    if not k:
        await conn.close()
        return False, "Ключ не найден."
    if not k["active"]:
        await conn.close()
        return False, "Ключ заблокирован."
    if k["user_id"] and k["user_id"] != user_id:
        await conn.close()
        return False, "Ключ уже используется."
    await conn.execute("UPDATE keys SET user_id=$1 WHERE key=$2", user_id, key)
    await conn.execute("""
        INSERT INTO users (user_id, username, active, key, ref_code)
        VALUES ($1, $2, TRUE, $3, $4)
        ON CONFLICT (user_id) DO UPDATE SET active=TRUE, key=$3, username=$2
    """, user_id, username, key, _gen_code("REF", 8))
    await conn.close()
    return True, "✅ Доступ активирован!"


async def list_keys() -> list:
    conn = await get_conn()
    rows = await conn.fetch("""
        SELECT k.*, u.username FROM keys k
        LEFT JOIN users u ON u.key=k.key
        ORDER BY k.created DESC
    """)
    await conn.close()
    return [dict(r) for r in rows]


async def revoke_key(key: str) -> bool:
    conn = await get_conn()
    r = await conn.execute(
        "UPDATE keys SET active=FALSE WHERE key=$1", key.strip().upper())
    await conn.execute(
        "UPDATE users SET active=FALSE WHERE key=$1", key.strip().upper())
    await conn.close()
    return r != "UPDATE 0"


async def block_user(user_id: int) -> bool:
    conn = await get_conn()
    await conn.execute("UPDATE users SET active=FALSE WHERE user_id=$1", user_id)
    await conn.close()
    return True


async def unblock_user(user_id: int) -> bool:
    conn = await get_conn()
    await conn.execute("UPDATE users SET active=TRUE WHERE user_id=$1", user_id)
    await conn.close()
    return True


# ─── Платежи ──────────────────────────────────────────────────────────────────

async def create_payment(user_id: int, network: str) -> dict:
    conn    = await get_conn()
    address = WALLETS.get(network, WALLETS["eth"])
    exist   = await conn.fetchrow(
        "SELECT * FROM payments WHERE user_id=$1 AND confirmed=FALSE AND network=$2",
        user_id, network)
    if exist:
        await conn.close()
        return dict(exist)
    await conn.execute(
        "INSERT INTO payments (user_id, address, amount, network) VALUES ($1,$2,$3,$4)",
        user_id, address, SUB_PRICE_USD, network)
    payment = await conn.fetchrow(
        "SELECT * FROM payments WHERE user_id=$1 AND confirmed=FALSE AND network=$2",
        user_id, network)
    await conn.close()
    return dict(payment)


async def confirm_payment(user_id: int) -> str:
    conn = await get_conn()
    key  = _gen_code("OSINT-", 12)
    await conn.execute("INSERT INTO keys (key, user_id, active) VALUES ($1,$2,TRUE)", key, user_id)
    await conn.execute(
        "UPDATE users SET active=TRUE, key=$1 WHERE user_id=$2", key, user_id)
    await conn.execute(
        "UPDATE payments SET confirmed=TRUE WHERE user_id=$1 AND confirmed=FALSE", user_id)
    await conn.close()
    return key
