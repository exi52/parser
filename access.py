"""
Access, subscriptions, referrals, keys and payments.
"""
import os, secrets, string
from datetime import datetime, timedelta
from database import get_pool

TRIAL_SEARCHES = 10
TRIAL_HOURS    = 24

WALLETS = {
    "eth":        os.getenv("WALLET_ETH", "0xВСТАВЬ_ETH"),
    "usdt_erc20": os.getenv("WALLET_ETH", "0xВСТАВЬ_ETH"),
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

PAYMENT_ADDRESS = WALLETS["eth"]


def _gen_code(prefix="", length=10):
    chars = string.ascii_uppercase + string.digits
    return prefix + "".join(secrets.choice(chars) for _ in range(length))


def _fmt_exp(dt) -> str:
    if not dt:
        return "без срока"
    if (dt - datetime.now()).days > 30000:
        return "навсегда"
    return dt.strftime("%d.%m.%Y %H:%M")


async def get_or_create_user(user_id: int, username: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        if not user:
            ref_code = _gen_code("REF", 8)
            await conn.execute("""
                INSERT INTO users (user_id, username, active, ref_code)
                VALUES ($1, $2, FALSE, $3)
            """, user_id, username, ref_code)
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        elif user["username"] != username:
            await conn.execute(
                "UPDATE users SET username=$1 WHERE user_id=$2",
                username, user_id)
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    return dict(user)


async def check_access(user_id: int) -> tuple[bool, str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        if not user:
            return False, "no_sub"
        if user["blocked"]:
            return False, "blocked"

        if user["active"] and user["key"]:
            key = await conn.fetchrow(
                "SELECT active, expires FROM keys WHERE key=$1",
                user["key"])
            key_alive = key and key["active"] and (not key["expires"] or datetime.now() < key["expires"])
            sub_alive = not user["subscription_expires"] or datetime.now() < user["subscription_expires"]
            if key_alive and sub_alive:
                return True, "ok"
            await conn.execute("UPDATE users SET active=FALSE WHERE user_id=$1", user_id)
            if key and key["expires"] and datetime.now() >= key["expires"]:
                await conn.execute("UPDATE keys SET active=FALSE WHERE key=$1", user["key"])
            return False, "sub_expired"

        if user["trial_searches"] and user["trial_searches"] > 0 and user["trial_expires"]:
            if datetime.now() < user["trial_expires"]:
                return True, "trial_ok"
            return False, "trial_expired"
    return False, "no_sub"


async def use_search(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE users
            SET trial_searches=trial_searches-1
            WHERE user_id=$1
              AND NOT (active=TRUE AND key IS NOT NULL)
              AND trial_searches > 0
              AND trial_expires > NOW()
            RETURNING trial_searches
        """, user_id)
    return row is not None


async def get_user_stats(user_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        ref_count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE owner_id=$1",
            user_id)
    if not user:
        return {}

    trial_left = 0
    trial_exp  = None
    if user["trial_expires"] and datetime.now() < user["trial_expires"]:
        trial_left = max(0, user["trial_searches"] or 0)
        trial_exp  = user["trial_expires"].strftime("%d.%m %H:%M")

    sub_active = bool(
        user["active"] and user["key"] and
        (not user["subscription_expires"] or datetime.now() < user["subscription_expires"])
    )
    return {
        "active":       user["active"],
        "blocked":      user["blocked"],
        "has_key":      bool(user["key"]),
        "sub_active":   sub_active,
        "sub_exp":      _fmt_exp(user["subscription_expires"]) if user["key"] else None,
        "plan_id":      user["plan_id"],
        "trial_left":   trial_left,
        "trial_exp":    trial_exp,
        "ref_count":    ref_count or 0,
        "ref_code":     user["ref_code"],
    }


async def activate_ref(user_id: int, username: str, ref_code: str) -> tuple[bool, str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
            owner = await conn.fetchrow(
                "SELECT * FROM users WHERE ref_code=$1",
                ref_code.upper())

            if not owner:
                return False, "❌ Реф-код не найден."
            if owner["user_id"] == user_id:
                return False, "❌ Нельзя использовать свой реф-код."
            if user and user["invited_by"]:
                return False, "❌ Ты уже активировал реф-код ранее."

            trial_exp = datetime.now() + timedelta(hours=TRIAL_HOURS)
            if not user:
                await conn.execute("""
                    INSERT INTO users (user_id, username, active, trial_searches, trial_expires, invited_by, ref_code)
                    VALUES ($1, $2, FALSE, $3, $4, $5, $6)
                """, user_id, username, TRIAL_SEARCHES, trial_exp, owner["user_id"], _gen_code("REF", 8))
            else:
                await conn.execute("""
                    UPDATE users
                    SET trial_searches=$1, trial_expires=$2, invited_by=$3, username=$4
                    WHERE user_id=$5
                """, TRIAL_SEARCHES, trial_exp, owner["user_id"], username, user_id)

            exist = await conn.fetchrow(
                "SELECT id FROM referrals WHERE owner_id=$1 AND user_id=$2",
                owner["user_id"], user_id)
            if not exist:
                await conn.execute(
                    "INSERT INTO referrals (owner_id, user_id, username) VALUES ($1,$2,$3)",
                    owner["user_id"], user_id, username)

    return True, (
        f"✅ Реф-код активирован!\n\n"
        f"Тебе выдано <b>{TRIAL_SEARCHES} пробных поисков</b> на {TRIAL_HOURS} часов.\n"
        f"Пригласил тебя: @{owner['username'] or 'пользователь'}"
    )


async def get_referrals(user_id: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM referrals WHERE owner_id=$1 ORDER BY created DESC",
            user_id)
    return [dict(r) for r in rows]


async def list_all_refs() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.username, u.user_id, COUNT(r.id) as cnt
            FROM users u JOIN referrals r ON r.owner_id=u.user_id
            GROUP BY u.user_id, u.username ORDER BY cnt DESC
        """)
        result = []
        for row in rows:
            refs = await conn.fetch(
                "SELECT username, created FROM referrals WHERE owner_id=$1 ORDER BY created DESC LIMIT 5",
                row["user_id"])
            result.append({
                "username":  row["username"] or str(row["user_id"]),
                "user_id":   row["user_id"],
                "count":     row["cnt"],
                "referrals": [dict(r) for r in refs],
            })
    return result


async def generate_key(plan_id: str | None = None, expires=None) -> str:
    key = _gen_code("OSINT-", 12)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO keys (key, active, plan_id, expires) VALUES ($1, TRUE, $2, $3)",
            key, plan_id, expires)
    return key


async def activate_key(user_id: int, username: str, key: str) -> tuple[bool, str]:
    key = key.strip().upper()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            k = await conn.fetchrow("SELECT * FROM keys WHERE key=$1", key)
            if not k:
                return False, "Ключ не найден."
            if not k["active"]:
                return False, "Ключ заблокирован."
            if k["expires"] and datetime.now() >= k["expires"]:
                await conn.execute("UPDATE keys SET active=FALSE WHERE key=$1", key)
                return False, "Срок ключа истёк."
            if k["user_id"] and k["user_id"] != user_id:
                return False, "Ключ уже используется."

            await conn.execute("UPDATE keys SET user_id=$1 WHERE key=$2", user_id, key)
            await conn.execute("""
                INSERT INTO users (user_id, username, active, key, ref_code, plan_id, subscription_expires, blocked)
                VALUES ($1, $2, TRUE, $3, $4, $5, $6, FALSE)
                ON CONFLICT (user_id) DO UPDATE
                SET active=TRUE,
                    key=$3,
                    username=$2,
                    plan_id=$5,
                    subscription_expires=$6,
                    blocked=FALSE
            """, user_id, username, key, _gen_code("REF", 8), k["plan_id"], k["expires"])
    return True, "✅ Доступ активирован!"


async def list_keys() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT k.*, u.username FROM keys k
            LEFT JOIN users u ON u.key=k.key
            ORDER BY k.created DESC
        """)
    return [dict(r) for r in rows]


async def revoke_key(key: str) -> bool:
    key = key.strip().upper()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute("UPDATE keys SET active=FALSE WHERE key=$1", key)
            await conn.execute(
                "UPDATE users SET active=FALSE WHERE key=$1",
                key)
    return result != "UPDATE 0"


async def block_user(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET blocked=TRUE, active=FALSE WHERE user_id=$1",
            user_id)
    return True


async def unblock_user(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET blocked=FALSE WHERE user_id=$1",
            user_id)
    return True


async def create_payment(user_id: int, plan_id: str, amount: float, days: int, network: str) -> dict:
    pool = await get_pool()
    address = WALLETS.get(network, WALLETS["eth"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE payments
                SET status='canceled', updated=NOW()
                WHERE user_id=$1 AND product='sub' AND status IN ('pending', 'awaiting_hash', 'submitted')
            """, user_id)
            payment = await conn.fetchrow("""
                INSERT INTO payments (user_id, address, amount, network, plan_id, subscription_days, status, confirmed, product)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', FALSE, 'sub')
                RETURNING *
            """, user_id, address, amount, network, plan_id, days)
    return dict(payment)


async def submit_payment_hash(user_id: int, tx_hash: str) -> dict | None:
    tx_hash = tx_hash.strip()
    if len(tx_hash) < 8:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("""
            UPDATE payments
            SET tx_hash=$2, status='submitted', updated=NOW()
            WHERE id = (
                SELECT id FROM payments
                WHERE user_id=$1 AND product='sub' AND status='awaiting_hash'
                ORDER BY created DESC
                LIMIT 1
            )
            RETURNING *
        """, user_id, tx_hash)
    return dict(payment) if payment else None


async def start_payment_hash(user_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("""
            UPDATE payments
            SET status='awaiting_hash', updated=NOW()
            WHERE id = (
                SELECT id FROM payments
                WHERE user_id=$1 AND product='sub' AND status='pending'
                ORDER BY created DESC
                LIMIT 1
            )
            RETURNING *
        """, user_id)
    return dict(payment) if payment else None


async def get_pending_payment(user_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("""
            SELECT * FROM payments
            WHERE user_id=$1 AND product='sub' AND status IN ('pending', 'awaiting_hash', 'submitted')
            ORDER BY created DESC
            LIMIT 1
        """, user_id)
    return dict(payment) if payment else None


async def confirm_payment(user_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            payment = await conn.fetchrow("""
                SELECT * FROM payments
                WHERE user_id=$1 AND product='sub' AND status='submitted' AND tx_hash IS NOT NULL
                ORDER BY updated DESC
                LIMIT 1
                FOR UPDATE
            """, user_id)
            if not payment:
                return None

            days = payment["subscription_days"] or 30
            expires = datetime.now() + timedelta(days=days)
            key = _gen_code("OSINT-", 12)

            await conn.execute("UPDATE keys SET active=FALSE WHERE user_id=$1", user_id)
            await conn.execute("""
                INSERT INTO keys (key, user_id, active, plan_id, expires)
                VALUES ($1, $2, TRUE, $3, $4)
            """, key, user_id, payment["plan_id"], expires)
            await conn.execute("""
                UPDATE users
                SET active=TRUE,
                    key=$1,
                    plan_id=$2,
                    subscription_expires=$3,
                    blocked=FALSE
                WHERE user_id=$4
            """, key, payment["plan_id"], expires, user_id)
            await conn.execute("""
                UPDATE payments
                SET confirmed=TRUE, status='confirmed', updated=NOW()
                WHERE id=$1
            """, payment["id"])
    result = dict(payment)
    result["key"] = key
    result["expires"] = expires
    return result


# ─── Bulk: оплата за количество поисков (кредиты) ─────────────────────────────
# Доступ — это счётчик прогонов users.bulk_credits. Каждый bulk-поиск списывает 1.
# Оплата идёт через payments (product='bulk'); число купленных прогонов хранится
# в payments.subscription_days. Подписочные функции выше отфильтрованы по
# product='sub', поэтому потоки не пересекаются.


async def check_bulk_access(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        credits = await conn.fetchval("SELECT bulk_credits FROM users WHERE user_id=$1", user_id)
    return bool(credits and credits > 0)


async def get_bulk_status(user_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        credits = await conn.fetchval("SELECT bulk_credits FROM users WHERE user_id=$1", user_id)
    credits = int(credits or 0)
    return {"active": credits > 0, "credits": credits}


async def grant_bulk_access(user_id: int, count: int) -> int:
    """Начислить count прогонов. Создаёт юзера, если его ещё нет. Возвращает новый остаток."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO users (user_id, ref_code, bulk_credits)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id)
                DO UPDATE SET bulk_credits = COALESCE(users.bulk_credits, 0) + $3
            """, user_id, _gen_code("REF", 8), count)
            total = await conn.fetchval("SELECT bulk_credits FROM users WHERE user_id=$1", user_id)
    return int(total or 0)


async def consume_bulk_credit(user_id: int) -> int | None:
    """Списать 1 прогон атомарно. Возвращает остаток или None, если прогонов нет."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        remaining = await conn.fetchval("""
            UPDATE users
            SET bulk_credits = bulk_credits - 1
            WHERE user_id=$1 AND COALESCE(bulk_credits, 0) > 0
            RETURNING bulk_credits
        """, user_id)
    return int(remaining) if remaining is not None else None


async def revoke_bulk_access(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cur = await conn.fetchval("SELECT bulk_credits FROM users WHERE user_id=$1", user_id)
            had = bool(cur and cur > 0)
            await conn.execute("UPDATE users SET bulk_credits=0 WHERE user_id=$1", user_id)
            await conn.execute("""
                UPDATE payments SET status='canceled', updated=NOW()
                WHERE user_id=$1 AND product='bulk' AND status IN ('pending', 'awaiting_hash', 'submitted')
            """, user_id)
    return had


async def request_bulk_payment(user_id: int, plan_id: str, amount: float, credits: int, network: str) -> dict:
    pool = await get_pool()
    address = WALLETS.get(network, WALLETS["eth"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                UPDATE payments
                SET status='canceled', updated=NOW()
                WHERE user_id=$1 AND product='bulk' AND status IN ('pending', 'awaiting_hash', 'submitted')
            """, user_id)
            payment = await conn.fetchrow("""
                INSERT INTO payments (user_id, address, amount, network, plan_id, subscription_days, status, confirmed, product)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', FALSE, 'bulk')
                RETURNING *
            """, user_id, address, amount, network, plan_id, credits)
    return dict(payment)


async def start_bulk_hash(user_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("""
            UPDATE payments
            SET status='awaiting_hash', updated=NOW()
            WHERE id = (
                SELECT id FROM payments
                WHERE user_id=$1 AND product='bulk' AND status='pending'
                ORDER BY created DESC
                LIMIT 1
            )
            RETURNING *
        """, user_id)
    return dict(payment) if payment else None


async def submit_bulk_hash(user_id: int, tx_hash: str) -> dict | None:
    tx_hash = tx_hash.strip()
    if len(tx_hash) < 8:
        return None
    pool = await get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("""
            UPDATE payments
            SET tx_hash=$2, status='submitted', updated=NOW()
            WHERE id = (
                SELECT id FROM payments
                WHERE user_id=$1 AND product='bulk' AND status='awaiting_hash'
                ORDER BY created DESC
                LIMIT 1
            )
            RETURNING *
        """, user_id, tx_hash)
    return dict(payment) if payment else None


async def get_pending_bulk(user_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        payment = await conn.fetchrow("""
            SELECT * FROM payments
            WHERE user_id=$1 AND product='bulk' AND status IN ('pending', 'awaiting_hash', 'submitted')
            ORDER BY created DESC
            LIMIT 1
        """, user_id)
    return dict(payment) if payment else None


async def confirm_bulk_payment(user_id: int) -> dict | None:
    """Подтвердить bulk-оплату вручную (админ). Начисляет купленные прогоны."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            payment = await conn.fetchrow("""
                SELECT * FROM payments
                WHERE user_id=$1 AND product='bulk' AND status='submitted' AND tx_hash IS NOT NULL
                ORDER BY updated DESC
                LIMIT 1
                FOR UPDATE
            """, user_id)
            if not payment:
                return None

            credits = int(payment["subscription_days"] or 1)
            await conn.execute("""
                INSERT INTO users (user_id, ref_code, bulk_credits)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id)
                DO UPDATE SET bulk_credits = COALESCE(users.bulk_credits, 0) + $3
            """, user_id, _gen_code("REF", 8), credits)
            total = await conn.fetchval("SELECT bulk_credits FROM users WHERE user_id=$1", user_id)
            await conn.execute("""
                UPDATE payments
                SET confirmed=TRUE, status='confirmed', updated=NOW()
                WHERE id=$1
            """, payment["id"])
    result = dict(payment)
    result["credits"] = credits
    result["total"] = int(total or 0)
    return result
