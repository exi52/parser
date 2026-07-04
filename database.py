"""
PostgreSQL helpers and schema migrations.
"""
import os
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is empty")
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=DB_POOL_MIN,
            max_size=DB_POOL_MAX,
            command_timeout=30,
        )
    return _pool


async def get_pool() -> asyncpg.Pool:
    return await init_pool()


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_db():
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bulk_jobs (
                    id              SERIAL PRIMARY KEY,
                    user_id          BIGINT NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'queued',
                    total_count      INTEGER NOT NULL DEFAULT 0,
                    processed_count  INTEGER NOT NULL DEFAULT 0,
                    found_count      INTEGER NOT NULL DEFAULT 0,
                    error            TEXT,
                    created_at       TIMESTAMP DEFAULT NOW(),
                    started_at       TIMESTAMP,
                    finished_at      TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bulk_items (
                    id          SERIAL PRIMARY KEY,
                    job_id      INTEGER NOT NULL REFERENCES bulk_jobs(id) ON DELETE CASCADE,
                    username    TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    wallets     JSONB DEFAULT '[]'::jsonb,
                    platforms   JSONB DEFAULT '[]'::jsonb,
                    matched     JSONB DEFAULT '[]'::jsonb,
                    balances    JSONB DEFAULT '{}'::jsonb,
                    result      JSONB DEFAULT '{}'::jsonb,
                    error       TEXT,
                    elapsed_ms  INTEGER DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT NOW(),
                    updated_at  TIMESTAMP DEFAULT NOW()
                )
            """)

            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_expires TIMESTAMP")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_id TEXT")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bulk_expires TIMESTAMP")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bulk_credits INTEGER DEFAULT 0")

            await conn.execute("ALTER TABLE keys ADD COLUMN IF NOT EXISTS plan_id TEXT")
            await conn.execute("ALTER TABLE keys ADD COLUMN IF NOT EXISTS expires TIMESTAMP")

            await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS network TEXT DEFAULT 'eth'")
            await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS plan_id TEXT")
            await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS tx_hash TEXT")
            await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'")
            await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS subscription_days INTEGER")
            await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS updated TIMESTAMP DEFAULT NOW()")
            await conn.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS product TEXT DEFAULT 'sub'")

            await conn.execute("""
                UPDATE payments
                SET status = CASE WHEN confirmed THEN 'confirmed' ELSE COALESCE(status, 'pending') END
                WHERE status IS NULL OR status = ''
            """)
            await conn.execute("""
                UPDATE payments
                SET product = 'sub'
                WHERE product IS NULL OR product = ''
            """)
            await conn.execute("""
                UPDATE users
                SET blocked = FALSE
                WHERE blocked IS NULL
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_payments_pending_user
                ON payments (user_id, status, created DESC)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_payments_product_user
                ON payments (user_id, product, status, created DESC)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_keys_user
                ON keys (user_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bulk_jobs_user_created
                ON bulk_jobs (user_id, created_at DESC)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bulk_items_job_status
                ON bulk_items (job_id, status)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bulk_items_job_username
                ON bulk_items (job_id, username)
            """)
    print("DB ready")
