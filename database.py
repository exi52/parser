"""
База данных PostgreSQL — хранение ключей, пользователей, рефералов
Работает через asyncpg (быстро и просто)
"""

import os
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "")


async def get_conn():
    return await asyncpg.connect(DATABASE_URL)


async def init_db():
    """Создаёт таблицы если их нет — вызывается при старте бота"""
    conn = await get_conn()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            key         TEXT PRIMARY KEY,
            created     TEXT,
            activated_by_id   BIGINT,
            activated_by_name TEXT,
            searches    INTEGER DEFAULT 0,
            active      BOOLEAN DEFAULT TRUE
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     BIGINT PRIMARY KEY,
            username    TEXT,
            key         TEXT,
            activated   TEXT,
            active      BOOLEAN DEFAULT TRUE,
            searches    INTEGER DEFAULT 0,
            ref_code    TEXT,
            invited_by  BIGINT,
            invited_by_name TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id          SERIAL PRIMARY KEY,
            owner_id    BIGINT,
            user_id     BIGINT,
            username    TEXT,
            date        TEXT
        )
    """)
    await conn.close()
