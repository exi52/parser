"""
База данных PostgreSQL
"""
import os
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "")

async def get_conn():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    conn = await get_conn()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         BIGINT PRIMARY KEY,
            username        TEXT,
            active          BOOLEAN DEFAULT FALSE,
            key             TEXT,
            trial_searches  INTEGER DEFAULT 0,
            trial_expires   TIMESTAMP,
            invited_by      BIGINT,
            ref_code        TEXT UNIQUE,
            created         TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            key         TEXT PRIMARY KEY,
            user_id     BIGINT,
            created     TIMESTAMP DEFAULT NOW(),
            active      BOOLEAN DEFAULT TRUE
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id          SERIAL PRIMARY KEY,
            owner_id    BIGINT,
            user_id     BIGINT,
            username    TEXT,
            created     TIMESTAMP DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT,
            address     TEXT,
            amount      NUMERIC,
            network     TEXT DEFAULT 'eth',
            confirmed   BOOLEAN DEFAULT FALSE,
            created     TIMESTAMP DEFAULT NOW()
        )
    """)
    # Добавляем колонку network если не существует (для старых БД)
    await conn.execute("""
        ALTER TABLE payments ADD COLUMN IF NOT EXISTS network TEXT DEFAULT 'eth'
    """)
    await conn.close()
    print("DB ready")
