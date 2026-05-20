"""
Система доступа — PostgreSQL версия
Ключи, пользователи, рефералы хранятся в базе (не сбрасываются при деплое)
"""

import secrets
import string
from datetime import datetime
import asyncpg
from database import get_conn


def _now():
    return datetime.now().isoformat()[:10]


# ─── Ключи ────────────────────────────────────────────────────────────────────

async def generate_key() -> str:
    chars = string.ascii_uppercase + string.digits
    key   = "OSINT-" + "".join(secrets.choice(chars) for _ in range(12))
    conn  = await get_conn()
    await conn.execute(
        "INSERT INTO keys (key, created, searches, active) VALUES ($1, $2, 0, TRUE)",
        key, _now()
    )
    await conn.close()
    return key


async def activate_key(user_id: int, username: str, key: str):
    key  = key.strip().upper()
    conn = await get_conn()

    # Уже есть доступ?
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if user and user["active"]:
        await conn.close()
        return False, "У тебя уже есть активный доступ!"

    # Ключ существует?
    k = await conn.fetchrow("SELECT * FROM keys WHERE key=$1", key)
    if not k:
        await conn.close()
        return False, "Ключ не найден. Проверь правильность."

    if not k["active"]:
        await conn.close()
        return False, "Этот ключ заблокирован."

    if k["activated_by_id"] and k["activated_by_id"] != user_id:
        await conn.close()
        return False, "Этот ключ уже использован другим пользователем."

    # Привязываем ключ к юзеру
    await conn.execute(
        "UPDATE keys SET activated_by_id=$1, activated_by_name=$2 WHERE key=$3",
        user_id, username, key
    )
    await conn.execute("""
        INSERT INTO users (user_id, username, key, activated, active, searches)
        VALUES ($1, $2, $3, $4, TRUE, 0)
        ON CONFLICT (user_id) DO UPDATE
        SET key=$3, activated=$4, active=TRUE, username=$2
    """, user_id, username, key, _now())
    await conn.close()
    return True, "Доступ активирован! Теперь отправляй @username или адрес кошелька."


async def check_access(user_id: int):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if not user:
        await conn.close()
        return False, "no_access"
    if not user["active"]:
        await conn.close()
        return False, "blocked"
    # Если доступ через реф — нет ключа, это нормально
    if user["key"]:
        k = await conn.fetchrow("SELECT * FROM keys WHERE key=$1", user["key"])
        if k and not k["active"]:
            await conn.close()
            return False, "key_revoked"
    await conn.close()
    return True, "ok"


async def use_search(user_id: int):
    conn = await get_conn()
    user = await conn.fetchrow("SELECT key FROM users WHERE user_id=$1", user_id)
    await conn.execute(
        "UPDATE users SET searches=searches+1 WHERE user_id=$1", user_id)
    if user and user["key"]:
        await conn.execute(
            "UPDATE keys SET searches=searches+1 WHERE key=$1", user["key"])
    await conn.close()


async def get_user_stats(user_id: int) -> dict:
    conn  = await get_conn()
    user  = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    await conn.close()
    if not user:
        return {}
    return {
        "searches":  user["searches"],
        "activated": user["activated"],
        "active":    user["active"],
    }


async def list_keys() -> list:
    conn = await get_conn()
    rows = await conn.fetch("SELECT * FROM keys ORDER BY created DESC")
    await conn.close()
    result = []
    for k in rows:
        result.append({
            "key":      k["key"],
            "active":   k["active"],
            "searches": k["searches"],
            "user":     f"@{k['activated_by_name']}" if k["activated_by_name"] else "не активирован",
            "user_id":  k["activated_by_id"],
            "created":  k["created"],
        })
    return result


async def revoke_key(key: str) -> bool:
    key  = key.strip().upper()
    conn = await get_conn()
    k    = await conn.fetchrow("SELECT * FROM keys WHERE key=$1", key)
    if not k:
        await conn.close()
        return False
    await conn.execute("UPDATE keys SET active=FALSE WHERE key=$1", key)
    await conn.execute(
        "UPDATE users SET active=FALSE WHERE key=$1", key)
    await conn.close()
    return True


async def block_user(user_id: int) -> bool:
    conn = await get_conn()
    res  = await conn.execute(
        "UPDATE users SET active=FALSE WHERE user_id=$1", user_id)
    await conn.close()
    return res != "UPDATE 0"


async def unblock_user(user_id: int) -> bool:
    conn = await get_conn()
    user = await conn.fetchrow("SELECT key FROM users WHERE user_id=$1", user_id)
    if not user:
        await conn.close()
        return False
    # Проверяем ключ если есть
    if user["key"]:
        k = await conn.fetchrow("SELECT active FROM keys WHERE key=$1", user["key"])
        if k and not k["active"]:
            await conn.close()
            return False
    await conn.execute(
        "UPDATE users SET active=TRUE WHERE user_id=$1", user_id)
    await conn.close()
    return True


# ─── Реферальная система ──────────────────────────────────────────────────────

async def generate_ref(user_id: int) -> str:
    conn = await get_conn()
    user = await conn.fetchrow("SELECT ref_code FROM users WHERE user_id=$1", user_id)
    if user and user["ref_code"]:
        await conn.close()
        return user["ref_code"]
    # Генерируем новый
    chars    = string.ascii_uppercase + string.digits
    ref_code = "REF" + "".join(secrets.choice(chars) for _ in range(8))
    await conn.execute(
        "UPDATE users SET ref_code=$1 WHERE user_id=$2", ref_code, user_id)
    await conn.close()
    return ref_code


async def activate_ref(user_id: int, username: str, ref_code: str):
    conn = await get_conn()

    # Уже есть доступ?
    user = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if user and user["active"]:
        await conn.close()
        return False, "У тебя уже есть активный доступ!"

    # Ищем владельца реф-кода
    owner = await conn.fetchrow(
        "SELECT * FROM users WHERE ref_code=$1", ref_code)
    if not owner:
        await conn.close()
        return False, "Реф-код не найден."

    if owner["user_id"] == user_id:
        await conn.close()
        return False, "Нельзя использовать свой реф-код."

    if not owner["active"]:
        await conn.close()
        return False, "Этот реф-код недействителен."

    # Даём доступ
    await conn.execute("""
        INSERT INTO users (user_id, username, key, activated, active, searches, invited_by, invited_by_name)
        VALUES ($1, $2, NULL, $3, TRUE, 0, $4, $5)
        ON CONFLICT (user_id) DO UPDATE
        SET active=TRUE, username=$2, invited_by=$4, invited_by_name=$5
    """, user_id, username, _now(), owner["user_id"], owner["username"])

    # Записываем реферала
    await conn.execute(
        "INSERT INTO referrals (owner_id, user_id, username, date) VALUES ($1, $2, $3, $4)",
        owner["user_id"], user_id, username, _now()
    )
    await conn.close()
    return True, f"Доступ активирован! Тебя пригласил @{owner['username']}."


async def get_referrals(user_id: int) -> list:
    conn = await get_conn()
    rows = await conn.fetch(
        "SELECT * FROM referrals WHERE owner_id=$1 ORDER BY id DESC", user_id)
    await conn.close()
    return [dict(r) for r in rows]


async def list_all_refs() -> list:
    conn = await get_conn()
    rows = await conn.fetch("""
        SELECT u.username, u.user_id,
               COUNT(r.id) as cnt
        FROM users u
        LEFT JOIN referrals r ON r.owner_id = u.user_id
        GROUP BY u.user_id, u.username
        HAVING COUNT(r.id) > 0
        ORDER BY cnt DESC
    """)
    result = []
    for row in rows:
        refs = await conn.fetch(
            "SELECT * FROM referrals WHERE owner_id=$1 ORDER BY id DESC LIMIT 5",
            row["user_id"]
        )
        result.append({
            "username":  row["username"],
            "user_id":   row["user_id"],
            "count":     row["cnt"],
            "referrals": [dict(r) for r in refs],
        })
    await conn.close()
    return result
