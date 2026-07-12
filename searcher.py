"""
Crypto OSINT — searcher.py
Только надёжные API-чекеры (без HTML парсинга — он давал одинаковые кошельки для всех)
"""

import asyncio
import copy
import logging
import math
import os
import re
import time
from typing import Optional
import httpx

log = logging.getLogger(__name__)

SEARCH_CACHE_SECONDS          = int(os.getenv("SEARCH_CACHE_SECONDS", "900"))
PLATFORM_TIMEOUT_SECONDS      = int(os.getenv("PLATFORM_TIMEOUT_SECONDS", "16"))
BULK_PLATFORM_TIMEOUT_SECONDS = int(os.getenv("BULK_PLATFORM_TIMEOUT_SECONDS", "5"))
BULK_USERNAME_TIMEOUT_SECONDS = int(os.getenv("BULK_USERNAME_TIMEOUT_SECONDS", "12"))
BULK_SOURCE_CONCURRENCY       = max(1, int(os.getenv("BULK_SOURCE_CONCURRENCY", "6")))
BULK_ENS_RETRY_DELAY_SECONDS  = max(0.0, float(os.getenv("BULK_ENS_RETRY_DELAY_SECONDS", "0.35")))
BULK_ENS_RETRY_TIMEOUT_SECONDS = int(os.getenv("BULK_ENS_RETRY_TIMEOUT_SECONDS", "8"))
OPENSEA_API_KEY               = os.getenv("OPENSEA_KEY", "")
WEB3BIO_API_KEY               = os.getenv("WEB3BIO_API_KEY", "")

# ── GoldRush (Covalent) — суммарный баланс кошелька в USD ──────────────────────
GOLDRUSH_API_KEY    = os.getenv("GOLDRUSH_API_KEY", "")
GOLDRUSH_SOLANA_ENABLED = os.getenv("GOLDRUSH_SOLANA", "0").lower() in ("1", "true", "yes", "on")
GOLDRUSH_CHAINS     = os.getenv(
    "GOLDRUSH_CHAINS",
    "eth-mainnet,base-mainnet,matic-mainnet,bsc-mainnet,arbitrum-mainnet,optimism-mainnet",
)
GOLDRUSH_TIMEOUT    = int(os.getenv("GOLDRUSH_TIMEOUT", "20"))
GOLDRUSH_CONCURRENCY = max(1, int(os.getenv("GOLDRUSH_CONCURRENCY", "8")))
GOLDRUSH_RPS = max(1, int(os.getenv("GOLDRUSH_RPS", "4")))
GOLDRUSH_CACHE_SECONDS = int(os.getenv("GOLDRUSH_CACHE_SECONDS", "1800"))
BALANCE_CACHE_SECONDS = int(os.getenv("BALANCE_CACHE_SECONDS", str(GOLDRUSH_CACHE_SECONDS)))
GOLDRUSH_RETRIES = max(1, int(os.getenv("GOLDRUSH_RETRIES", "3")))
BALANCE_MAX_TOKEN_USD = float(os.getenv("BALANCE_MAX_TOKEN_USD", "100000000000"))
BALANCE_PROVIDER = os.getenv("BALANCE_PROVIDER", "free").strip().lower()
FREE_RPC_TIMEOUT = int(os.getenv("FREE_RPC_TIMEOUT", "10"))
FREE_RPC_CONCURRENCY = max(1, int(os.getenv("FREE_RPC_CONCURRENCY", "8")))
FREE_PRICE_CACHE_SECONDS = int(os.getenv("FREE_PRICE_CACHE_SECONDS", "300"))

SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet.solana.com")
SOLANA_TIMEOUT = int(os.getenv("SOLANA_TIMEOUT", "12"))
SOLANA_USDC_MINT = os.getenv("SOLANA_USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
SOLANA_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOLANA_TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
FREE_SOLANA_MAX_TOKENS = max(1, int(os.getenv("FREE_SOLANA_MAX_TOKENS", "100")))
SOLANA_SOL_PRICE_ID = os.getenv(
    "SOLANA_SOL_PRICE_ID",
    "solana:So11111111111111111111111111111111111111112",
)

_BALANCE_CACHE: dict[str, tuple[float, dict]] = {}
_BALANCE_INFLIGHT: dict[str, asyncio.Task] = {}
_BALANCE_SEMAPHORE = asyncio.Semaphore(max(GOLDRUSH_CONCURRENCY, 1))
_GOLDRUSH_RATE_LOCK = asyncio.Lock()
_GOLDRUSH_NEXT_REQUEST = 0.0
_FREE_RPC_SEMAPHORE = asyncio.Semaphore(FREE_RPC_CONCURRENCY)
_FREE_PRICE_CACHE: tuple[float, dict[str, float]] | None = None
_FREE_PRICE_INFLIGHT: asyncio.Task | None = None
_FREE_TOKEN_PRICE_CACHE: dict[str, tuple[float, float | None, str]] = {}
_FREE_PRICE_SEMAPHORE = asyncio.Semaphore(2)

_SEARCH_CACHE:    dict[str, tuple[float, dict]] = {}
_BULK_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}
_PLATFORM_HIT_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
PLATFORM_HIT_CACHE_SECONDS = int(os.getenv("PLATFORM_HIT_CACHE_SECONDS", "3600"))


def _rpc_urls(env_name: str, defaults: list[str]) -> list[str]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return defaults
    return [url.strip() for url in raw.replace(";", ",").split(",") if url.strip()]


FREE_EVM_CHAINS = {
    "ethereum": {
        "native": "ETH",
        "price": "ETH",
        "rpcs": _rpc_urls("ETH_RPC_URLS", [
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
        ]),
        "tokens": [
            ("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6, "USD"),
            ("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6, "USD"),
            ("DAI", "0x6b175474e89094c44da98b954eedeac495271d0f", 18, "USD"),
            ("WETH", "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 18, "ETH"),
        ],
    },
    "base": {
        "native": "ETH",
        "price": "ETH",
        "rpcs": _rpc_urls("BASE_RPC_URLS", [
            "https://mainnet.base.org",
            "https://base-rpc.publicnode.com",
        ]),
        "tokens": [
            ("USDC", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6, "USD"),
            ("USDbC", "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", 6, "USD"),
            ("WETH", "0x4200000000000000000000000000000000000006", 18, "ETH"),
        ],
    },
    "arbitrum": {
        "native": "ETH",
        "price": "ETH",
        "rpcs": _rpc_urls("ARBITRUM_RPC_URLS", [
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum-one-rpc.publicnode.com",
        ]),
        "tokens": [
            ("USDC", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6, "USD"),
            ("USDC.e", "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8", 6, "USD"),
            ("USDT", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9", 6, "USD"),
            ("WETH", "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", 18, "ETH"),
        ],
    },
    "optimism": {
        "native": "ETH",
        "price": "ETH",
        "rpcs": _rpc_urls("OPTIMISM_RPC_URLS", [
            "https://mainnet.optimism.io",
            "https://optimism-rpc.publicnode.com",
        ]),
        "tokens": [
            ("USDC", "0x0b2c639c533813f4aa9d7837caf62653d097ff85", 6, "USD"),
            ("USDC.e", "0x7f5c764cbc14f9669b88837ca1490cca17c31607", 6, "USD"),
            ("USDT", "0x94b008aa00579c1307b0ef2c499ad98a8c58e58e", 6, "USD"),
            ("WETH", "0x4200000000000000000000000000000000000006", 18, "ETH"),
        ],
    },
    "polygon": {
        "native": "POL",
        "price": "POL",
        "rpcs": _rpc_urls("POLYGON_RPC_URLS", [
            "https://polygon-rpc.com",
            "https://polygon-bor-rpc.publicnode.com",
        ]),
        "tokens": [
            ("USDC", "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359", 6, "USD"),
            ("USDC.e", "0x2791bca1f2de4661ed88a30c99a7a9449aa84174", 6, "USD"),
            ("USDT", "0xc2132d05d31c914a87c6611c10748aeb04b58e8f", 6, "USD"),
            ("DAI", "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063", 18, "USD"),
            ("WETH", "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619", 18, "ETH"),
        ],
    },
    "bsc": {
        "native": "BNB",
        "price": "BNB",
        "rpcs": _rpc_urls("BSC_RPC_URLS", [
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc-rpc.publicnode.com",
        ]),
        "tokens": [
            ("USDT", "0x55d398326f99059ff775485246999027b3197955", 18, "USD"),
            ("USDC", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", 18, "USD"),
            ("DAI", "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3", 18, "USD"),
            ("WBNB", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", 18, "BNB"),
        ],
    },
}

FREE_PRICE_IDS = {
    "ETH": "coingecko:ethereum",
    "BNB": "coingecko:binancecoin",
    "POL": "coingecko:polygon-ecosystem-token",
    "SOL": SOLANA_SOL_PRICE_ID,
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class _TrackedClient:
    """Tracks whether a source was reachable even when a checker handles exceptions itself."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.attempts = 0
        self.available_responses = 0

    async def _request(self, method: str, *args, **kwargs):
        self.attempts += 1
        try:
            response = await getattr(self.client, method)(*args, **kwargs)
        except Exception:
            raise
        if response.status_code < 400 or response.status_code == 404:
            self.available_responses += 1
        return response

    async def get(self, *args, **kwargs):
        return await self._request("get", *args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._request("post", *args, **kwargs)

    @property
    def unavailable(self) -> bool:
        return self.attempts > 0 and self.available_responses == 0


def web3bio_headers() -> dict:
    if not WEB3BIO_API_KEY:
        return HEADERS
    return {**HEADERS, "X-API-KEY": f"Bearer {WEB3BIO_API_KEY}"}

DOMAIN_SUFFIXES = [
    ".eth", ".sol", ".btc", ".bnb", ".arb", ".lens",
    ".crypto", ".nft", ".wallet", ".x", ".blockchain",
    ".dao", ".888", ".zk", ".near", ".avax", ".sui", ".apt",
]


def extract_username(raw: str) -> Optional[str]:
    raw = raw.strip().rstrip("/")
    for suffix in DOMAIN_SUFFIXES:
        if raw.lower().endswith(suffix):
            raw = raw[:-len(suffix)]
            break
    if raw.startswith("@"):
        return raw[1:]
    for p in [
        r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,50})",
        r"t\.me/([A-Za-z0-9_]{5,50})",
        r"^([A-Za-z0-9_]{1,50})$",
    ]:
        m = re.search(p, raw)
        if m:
            return m.group(1)
    return None


def is_eth_address(s: str) -> bool:
    return bool(re.match(r"^0x[a-fA-F0-9]{40}$", s.strip()))


def is_solana_address(s: str) -> bool:
    return bool(re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", s.strip()))


def get_variants(username: str) -> dict:
    u  = username.strip()
    ul = u.lower()
    clean      = re.sub(r"[^a-z0-9]", "", ul)
    clean_dash = ul.replace("_", "-")

    base = list(dict.fromkeys(filter(None, [
        u, ul, clean,
        u.replace("_", "."),
        clean_dash,
        re.sub(r"\d+$", "", ul),
        ul.replace("0", "o"),
        ul.replace("1", "l"),
    ])))
    base = [v for v in base if len(v) >= 2][:8]

    domains = []
    for suffix in DOMAIN_SUFFIXES:
        if clean:
            domains.append(f"{clean}{suffix}")
        if clean_dash != clean and suffix in [".eth", ".crypto", ".nft", ".wallet", ".x"]:
            domains.append(f"{clean_dash}{suffix}")

    return {"base": base, "domains": domains, "clean": clean}


# ─── НАДЁЖНЫЕ ЧЕКЕРЫ (только API, не HTML парсинг) ───────────────────────────

async def check_farcaster(client, username, variants):
    """Farcaster API — возвращает точный адрес верифицированного кошелька"""
    for v in variants["base"][:4]:
        try:
            r = await client.get(
                f"https://api.warpcast.com/v2/user-by-username?username={v}",
                timeout=10)
            if r.status_code == 200:
                user = r.json().get("result", {}).get("user", {})
                if not user:
                    continue
                fid = user.get("fid")
                wallets = []
                if fid:
                    r2 = await client.get(
                        f"https://api.warpcast.com/v2/verifications?fid={fid}",
                        timeout=8)
                    if r2.status_code == 200:
                        wallets = [x["address"] for x in
                                   r2.json().get("result", {}).get("verifications", [])
                                   if x.get("address")]
                bio = ((user.get("profile") or {}).get("bio") or {})
                bio = bio.get("text", "") if isinstance(bio, dict) else ""
                return {"found": True, "platform": "Farcaster", "emoji": "🔵",
                        "url": f"https://warpcast.com/{v}", "matched": v,
                        "wallets": wallets, "extra": {"имя": user.get("displayName", "")}}
        except Exception:
            pass
    return {"found": False, "platform": "Farcaster", "emoji": "🔵"}


async def check_sns(client, username, variants):
    """Solana Name Service: username.sol → Solana адрес через Bonfida API"""
    sol_names = [d for d in variants["domains"] if d.endswith(".sol")]
    for name in sol_names[:3]:
        domain = name[:-4]
        try:
            r = await client.get(
                f"https://sns-sdk-proxy.bonfida.workers.dev/resolve/{domain}",
                timeout=10)
            if r.status_code == 200:
                payload = r.json()
                addr = payload.get("result")
                if addr and is_solana_address(addr):
                    return {"found": True, "platform": "Solana NS (.sol)", "emoji": "🟣",
                            "url": f"https://naming.bonfida.org/#/domain/{domain}", "matched": name,
                            "wallets": [addr], "extra": {"домен": name}}
                if addr:
                    log.debug("sns returned non-address result name=%s result=%s", name, addr)
        except Exception:
            log.debug("sns check failed name=%s", name, exc_info=True)
    return {"found": False, "platform": "Solana NS (.sol)", "emoji": "🟣"}


async def check_unstoppable(client, username, variants):
    """Unstoppable Domains API — возвращает точный owner адрес"""
    ud_ext   = [".crypto", ".nft", ".wallet", ".x", ".blockchain", ".dao", ".888"]
    ud_names = [d for d in variants["domains"] if any(d.endswith(e) for e in ud_ext)]
    for name in ud_names[:6]:
        try:
            r = await client.get(
                f"https://api.unstoppabledomains.com/resolve/domains/{name}",
                timeout=8)
            if r.status_code == 200:
                owner = (r.json().get("meta") or {}).get("owner")
                if owner and owner != "0x0000000000000000000000000000000000000000":
                    return {"found": True, "platform": "Unstoppable Domains", "emoji": "🔓",
                            "url": f"https://unstoppabledomains.com/d/{name}", "matched": name,
                            "wallets": [owner], "extra": {"домен": name}}
        except Exception:
            pass
    return {"found": False, "platform": "Unstoppable Domains", "emoji": "🔓"}


async def check_lens(client, username, variants):
    """Lens Protocol API — handle.lens → ownedBy (точный адрес владельца)"""
    for v in variants["base"][:3]:
        try:
            r = await client.post(
                "https://api.lens.xyz/graphql",
                json={
                    "query": """query($h:String!){
                      profile(request:{forHandle:$h}){
                        ownedBy{address}
                        metadata{displayName}
                      }
                    }""",
                    "variables": {"h": f"lens/@{v.lower()}"}
                },
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=12)
            if r.status_code == 200:
                profile = r.json().get("data", {}).get("profile")
                if profile:
                    wallet = (profile.get("ownedBy") or {}).get("address")
                    if wallet:
                        return {"found": True, "platform": "Lens Protocol", "emoji": "🌿",
                                "url": f"https://hey.xyz/u/{v.lower()}", "matched": v,
                                "wallets": [wallet],
                                "extra": {"имя": (profile.get("metadata") or {}).get("displayName", "")}}
        except Exception:
            pass
    return {"found": False, "platform": "Lens Protocol", "emoji": "🌿"}


async def check_spaceid(client, username, variants):
    """SPACE ID API — .bnb/.arb домены → адрес"""
    target = [d for d in variants["domains"] if d.endswith((".bnb", ".arb"))]
    for name in target[:4]:
        try:
            r = await client.get(f"https://api.web3.bio/profile/{name}", headers=web3bio_headers(), timeout=10)
            if r.status_code != 200:
                continue
            data     = r.json()
            profiles = data if isinstance(data, list) else [data]
            for p in profiles:
                handle = (p.get("handle") or p.get("identity") or "").lower()
                addr   = p.get("address", "")
                # Строго проверяем что домен совпадает
                if handle == name.lower() and addr and addr != "0x0000000000000000000000000000000000000000":
                    return {"found": True, "platform": "SPACE ID (.bnb/.arb)", "emoji": "🔶",
                            "url": f"https://space.id/profile/{name}", "matched": name,
                            "wallets": [addr], "extra": {"домен": name}}
        except Exception:
            pass
    return {"found": False, "platform": "SPACE ID (.bnb/.arb)", "emoji": "🔶"}


async def check_web3bio(client, username, variants):
    """Web3.bio — строго проверяем что найденный handle совпадает с ником"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(f"https://api.web3.bio/profile/{v}", headers=web3bio_headers(), timeout=10)
            if r.status_code != 200:
                continue
            data     = r.json()
            profiles = data if isinstance(data, list) else [data]

            # Фильтруем только те профили где handle реально совпадает с ником
            matched = []
            for p in profiles:
                handle = (p.get("handle") or p.get("identity") or "").lower()
                addr   = p.get("address", "")
                # Проверяем что это именно наш ник, а не случайный результат
                if (handle == v.lower() or handle == f"{v.lower()}.eth"
                        or handle == f"lens/@{v.lower()}"):
                    if addr and addr != "0x0000000000000000000000000000000000000000":
                        matched.append(p)

            if matched:
                wallets = list(dict.fromkeys(p["address"] for p in matched if p.get("address")))
                info    = " | ".join(
                    f"{p.get('platform','')}: {p.get('handle','')}"
                    for p in matched if p.get("platform"))[:150]
                return {"found": True, "platform": "Web3.bio", "emoji": "🌐",
                        "url": f"https://web3.bio/{v}", "matched": v,
                        "wallets": wallets[:3], "extra": {"профили": info}}
        except Exception:
            pass
    return {"found": False, "platform": "Web3.bio", "emoji": "🌐"}


async def check_github(client, username, variants):
    """GitHub — ищем 0x адреса в bio (только то что пользователь сам написал)"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://api.github.com/users/{v}",
                headers={**HEADERS, "Accept": "application/vnd.github+json"},
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                # Ищем только в bio и blog — не в HTML странице
                text  = " ".join(filter(None, [data.get("bio"), data.get("blog")]))
                addrs = list(dict.fromkeys(re.findall(r"0x[a-fA-F0-9]{40}", text)))
                ens   = re.findall(r"\b\w+\.eth\b", text)
                if addrs or ens:
                    return {"found": True, "platform": "GitHub", "emoji": "🐙",
                            "url": data.get("html_url"), "matched": v,
                            "wallets": addrs,
                            "extra": {"имя": data.get("name", ""),
                                      "bio": (data.get("bio") or "")[:80],
                                      "ens": ", ".join(ens) if ens else None}}
                return {"found": False, "platform": "GitHub", "emoji": "🐙",
                        "profile": {"url": data.get("html_url"), "matched": v}}
        except Exception:
            pass
    return {"found": False, "platform": "GitHub", "emoji": "🐙"}


async def check_gitcoin(client, username, variants):
    """Gitcoin API — handle → eth_address"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://gitcoin.co/api/v0.1/profile/{v.lower()}", timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("handle"):
                    addr = data.get("eth_address") or data.get("preferred_payout_address")
                    return {"found": True, "platform": "Gitcoin", "emoji": "💚",
                            "url": f"https://gitcoin.co/{v}", "matched": v,
                            "wallets": [addr] if addr else [],
                            "extra": {"имя": data.get("name", "")}}
        except Exception:
            pass
    return {"found": False, "platform": "Gitcoin", "emoji": "💚"}


async def check_snapshot(client, username, variants):
    """Snapshot — ник в пространстве → адреса adminов через GraphQL API"""
    for v in variants["base"][:3]:
        try:
            r = await client.post(
                "https://hub.snapshot.org/graphql",
                json={"query": "query($s:String!){spaces(first:3,where:{id_contains:$s}){id name admins}}",
                      "variables": {"s": v.lower()}},
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=10)
            if r.status_code == 200:
                spaces = r.json().get("data", {}).get("spaces", [])
                if spaces:
                    s = spaces[0]
                    admins = [a for a in (s.get("admins") or [])
                              if a != "0x0000000000000000000000000000000000000000"]
                    return {"found": True, "platform": "Snapshot (DAO)", "emoji": "📸",
                            "url": f"https://snapshot.org/#/{s['id']}", "matched": v,
                            "wallets": admins[:3], "extra": {"DAO": s.get("name", "")}}
        except Exception:
            pass
    return {"found": False, "platform": "Snapshot (DAO)", "emoji": "📸"}


# ─── Обратный поиск: адрес → ники ────────────────────────────────────────────

async def reverse_lookup(address: str) -> dict:
    address = address.strip().lower()
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=12) as client:
        raw = await asyncio.gather(
            _run_reverse_source(_rev_ens, client, address),
            _run_reverse_source(_rev_farcaster, client, address),
            _run_reverse_source(_rev_lens, client, address),
            _run_reverse_source(_rev_web3bio, client, address),
            return_exceptions=True
        )
    results = [r for r in raw if r and not isinstance(r, Exception) and r.get("found")]
    errors = [
        {"platform": r.get("platform", "reverse"), "error": r.get("error")}
        for r in raw if isinstance(r, dict) and r.get("error")
    ]
    errors.extend(
        {"platform": "reverse", "error": str(r)[:120]}
        for r in raw if isinstance(r, Exception)
    )
    return {
        "address": address,
        "found_count": len(results),
        "results": results,
        "diagnostics": {"platforms_checked": len(raw), "errors": errors},
    }


async def _run_reverse_source(fn, client, address: str) -> dict:
    tracked = _TrackedClient(client)
    result = await fn(tracked, address)
    if not result.get("found") and tracked.unavailable:
        result = dict(result)
        result["platform"] = result.get("platform") or fn.__name__.replace("_rev_", "")
        result["error"] = "source_unavailable"
    return result


async def _rev_ens(client, address):
    try:
        r = await client.post(
            "https://api.thegraph.com/subgraphs/name/ensdomains/ens",
            json={"query": f'{{domains(where:{{resolvedAddress:"{address}"}}){{name}}}}'},
            timeout=10)
        if r.status_code == 200:
            names = [d["name"] for d in r.json().get("data", {}).get("domains", []) if d.get("name")]
            if names:
                return {"found": True, "platform": "ENS", "emoji": "🔷",
                        "url": f"https://app.ens.domains/{names[0]}",
                        "handles": names, "extra": {}}
    except Exception:
        pass
    return {"found": False}


async def _rev_farcaster(client, address):
    try:
        r = await client.get(
            f"https://api.warpcast.com/v2/user-by-verification?address={address}",
            timeout=10)
        if r.status_code == 200:
            user = r.json().get("result", {}).get("user", {})
            if user:
                return {"found": True, "platform": "Farcaster", "emoji": "🔵",
                        "url": f"https://warpcast.com/{user.get('username','')}",
                        "handles": [user.get("username", "")], "extra": {}}
    except Exception:
        pass
    return {"found": False}


async def _rev_lens(client, address):
    try:
        r = await client.post(
            "https://api.lens.xyz/graphql",
            json={"query": "query($a:EvmAddress!){profiles(request:{where:{ownedBy:[$a]}}){items{handle{fullHandle}}}}",
                  "variables": {"a": address}},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=12)
        if r.status_code == 200:
            items   = r.json().get("data", {}).get("profiles", {}).get("items", [])
            handles = [(i.get("handle") or {}).get("fullHandle", "") for i in items]
            handles = [h.replace("lens/@", "") for h in handles if h]
            if handles:
                return {"found": True, "platform": "Lens", "emoji": "🌿",
                        "url": f"https://hey.xyz/u/{handles[0]}",
                        "handles": handles, "extra": {}}
    except Exception:
        pass
    return {"found": False}


async def _rev_web3bio(client, address):
    try:
        r = await client.get(f"https://api.web3.bio/profile/{address}", headers=web3bio_headers(), timeout=10)
        if r.status_code == 200:
            profiles = r.json() if isinstance(r.json(), list) else [r.json()]
            handles  = [f"{p.get('platform','')}: {p.get('handle','')}"
                        for p in profiles if p.get("handle")]
            if handles:
                return {"found": True, "platform": "Web3.bio", "emoji": "🌐",
                        "url": f"https://web3.bio/{address}",
                        "handles": handles, "extra": {}}
    except Exception:
        pass
    return {"found": False}


# ─── Известные контрактные адреса которые НЕ являются кошельками ─────────────
# Это адреса самих платформ/токенов — они появляются в HTML у всех

BLACKLIST = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH contract
    "0x0000000000000000000000000000000000000000",  # zero address
    "0x000000000000000000000000000000000000dead",  # burn address
    "0x3439153eb7af838ad19d56e1571fbd09333c2809",  # OpenSea contract
    "0x164906a76f1a2ea933366c446ae0ec6a37062c42",  # OpenSea contract
    "0x00000000006c3852cbef3e08e8df289169ede581",  # Seaport contract
    "0x2953399124f0cbb46d2cbacd8a89cf0599974963",  # OpenSea token
    "0x495f947276749ce646f68ac8c248420045cb7b5e",  # OpenSea shared storefront
    "0x1e0049783f008a0085193e00003d00cd54003c71",  # OpenSea conduit
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc",  # Seaport 1.5
    "0x0000000000000068f116a894984e2db1123eb395",  # Seaport 1.6
    "0x83c8f28c26bf6aaca652df1dbbe0e1b56f8baba2",  # Gem/OpenSea
    "0xa5409ec958c83c3f309868babaca7c86dcb077c1",  # OpenSea registry
    "0xf849de01b080adc3a814fabe1e2087475cf2e354",  # Blur contract
    "0x0000000000a39bb272e79075ade125fd351887ac",  # Blur pool
    "0xb16c1342e617a5b6e4b631eb114483fdb289c0a4",  # Blur bidding
}


def is_real_wallet(addr: str) -> bool:
    """Проверяет что адрес не является известным контрактом платформы"""
    return addr.lower() not in BLACKLIST and not addr.lower().startswith("0x000000")


async def check_opensea_html(client, username, variants):
    """OpenSea: парсим страницу но фильтруем контракты"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://opensea.io/{v}",
                headers={**HEADERS, "Accept": "text/html"},
                timeout=12)
            if r.status_code == 200 and v.lower() in str(r.url).lower():
                # Ищем адрес в JSON данных страницы — он идёт до контрактных адресов
                # OpenSea вставляет данные юзера в __NEXT_DATA__
                next_data = re.search(r'"address":"(0x[a-fA-F0-9]{40})"', r.text)
                if next_data:
                    addr = next_data.group(1)
                    if is_real_wallet(addr):
                        return {"found": True, "platform": "OpenSea", "emoji": "🌊",
                                "url": f"https://opensea.io/{v}", "matched": v,
                                "wallets": [addr], "extra": {}}
                # Fallback: берём все адреса и фильтруем
                addrs = [a for a in dict.fromkeys(re.findall(r"0x[a-fA-F0-9]{40}", r.text))
                         if is_real_wallet(a)]
                if addrs:
                    return {"found": True, "platform": "OpenSea", "emoji": "🌊",
                            "url": f"https://opensea.io/{v}", "matched": v,
                            "wallets": addrs[:2], "extra": {}}
        except Exception:
            pass
    return {"found": False, "platform": "OpenSea", "emoji": "🌊"}


async def check_blur_api(client, username, variants):
    """Blur: API с правильными заголовками"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://core-api.prod.blur.io/v1/users/{v.lower()}",
                headers={**HEADERS, "Origin": "https://blur.io", "Referer": "https://blur.io/"},
                timeout=10)
            if r.status_code == 200:
                addr = (r.json().get("user") or {}).get("walletAddress")
                if addr and is_real_wallet(addr):
                    return {"found": True, "platform": "Blur", "emoji": "💎",
                            "url": f"https://blur.io/user/{v}", "matched": v,
                            "wallets": [addr], "extra": {}}
        except Exception:
            pass
    return {"found": False, "platform": "Blur", "emoji": "💎"}


async def check_rainbow_html(client, username, variants):
    """Rainbow: парсим страницу с фильтрацией контрактов"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://rainbow.me/{v}",
                headers={**HEADERS, "Accept": "text/html"},
                timeout=12)
            if r.status_code == 200 and len(r.text) > 3000:
                addrs = [a for a in dict.fromkeys(re.findall(r"0x[a-fA-F0-9]{40}", r.text))
                         if is_real_wallet(a)]
                ens_m = re.search(r'"ens"\s*:\s*"([^"]+\.eth)"', r.text)
                if addrs or ens_m:
                    return {"found": True, "platform": "Rainbow", "emoji": "🌈",
                            "url": f"https://rainbow.me/{v}", "matched": v,
                            "wallets": addrs[:3],
                            "extra": {"ens": ens_m.group(1) if ens_m else None}}
        except Exception:
            pass
    return {"found": False, "platform": "Rainbow", "emoji": "🌈"}


async def check_zapper_html(client, username, variants):
    """Zapper: парсим страницу с фильтрацией контрактов"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://zapper.xyz/account/{v}",
                headers={**HEADERS, "Referer": "https://zapper.xyz/"},
                timeout=12)
            if r.status_code == 200 and len(r.text) > 3000:
                addrs = [a for a in dict.fromkeys(re.findall(r"0x[a-fA-F0-9]{40}", r.text))
                         if is_real_wallet(a)]
                if addrs:
                    return {"found": True, "platform": "Zapper", "emoji": "⚡",
                            "url": f"https://zapper.xyz/account/{v}", "matched": v,
                            "wallets": addrs[:3], "extra": {}}
        except Exception:
            pass
    return {"found": False, "platform": "Zapper", "emoji": "⚡"}


# ─── Retry helper ────────────────────────────────────────────────────────────

async def _retry(coro_fn, retries=3, base_delay=1.0):
    """Повторяет запрос при 429/5xx или timeout с exponential backoff"""
    for attempt in range(retries):
        try:
            return await coro_fn()
        except httpx.TimeoutException:
            if attempt < retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503) and attempt < retries - 1:
                await asyncio.sleep(base_delay * (2 ** attempt))
            else:
                raise
        except Exception:
            raise
    return None


# ─── Новые чекеры ────────────────────────────────────────────────────────────

async def check_opensea_v2(client, username, variants):
    """OpenSea API v2 — возвращает address по username (требует API ключ)"""
    if not OPENSEA_API_KEY:
        return {"found": False, "platform": "OpenSea", "emoji": "🌊"}
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://api.opensea.io/api/v2/accounts/{v}",
                headers={**HEADERS, "X-API-KEY": OPENSEA_API_KEY},
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                addr = data.get("address")
                if addr and is_real_wallet(addr):
                    return {
                        "found": True, "platform": "OpenSea", "emoji": "🌊",
                        "url": f"https://opensea.io/{v}", "matched": v,
                        "wallets": [addr],
                        "extra": {
                            "bio": data.get("bio", ""),
                            "twitter": data.get("twitter_username", ""),
                        }
                    }
        except Exception:
            pass
    return {"found": False, "platform": "OpenSea", "emoji": "🌊"}


async def check_basename(client, username, variants):
    """Base Names (.base.eth) через Web3.bio, без старого ENS subgraph."""
    candidates = []
    for v in [variants.get("clean")] + variants["base"][:2]:
        if v:
            candidates.append(f"{v}.base.eth")
    for name in list(dict.fromkeys(candidates))[:3]:
        try:
            r = await client.get(
                f"https://api.web3.bio/profile/basenames/{name}",
                headers=web3bio_headers(),
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                profiles = data if isinstance(data, list) else [data]
                for p in profiles:
                    handle = (p.get("handle") or p.get("identity") or "").lower()
                    wallet = p.get("address") or p.get("ownerAddress") or p.get("resolvedAddress")
                    if handle == name.lower() and wallet and is_real_wallet(wallet):
                        return {
                            "found": True, "platform": "Base Names", "emoji": "🔵",
                            "url": f"https://www.base.org/name/{name[:-9]}", "matched": name,
                            "wallets": [wallet], "extra": {"domain": name}
                        }
        except Exception:
            log.debug("basename check failed name=%s", name, exc_info=True)
    return {"found": False, "platform": "Base Names", "emoji": "🔵"}


async def check_friendtech(client, username, variants):
    """friend.tech — связывает Twitter username с Base кошельком"""
    for v in variants["base"][:3]:
        try:
            r = await client.get(
                f"https://prod-api.kosetto.com/users/{v}",
                headers={**HEADERS, "Authorization": ""},
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                addr = data.get("address")
                if addr and is_real_wallet(addr):
                    return {
                        "found": True, "platform": "friend.tech", "emoji": "👥",
                        "url": f"https://friend.tech/rooms/{addr}", "matched": v,
                        "wallets": [addr],
                        "extra": {
                            "twitter": data.get("twitterUsername", ""),
                            "name": data.get("twitterName", ""),
                        }
                    }
        except Exception:
            pass
    return {"found": False, "platform": "friend.tech", "emoji": "👥"}


async def check_aptos_names(client, username, variants):
    """Aptos Name Service — .apt домены"""
    apt_names = [d for d in variants["domains"] if d.endswith(".apt")]
    for name in apt_names[:3]:
        domain = name[:-4]  # убираем .apt
        try:
            r = await client.get(
                f"https://www.aptosnames.com/api/mainnet/v1/address/{domain}",
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                addr = data.get("address")
                if addr:
                    return {
                        "found": True, "platform": "Aptos Names (.apt)", "emoji": "🟦",
                        "url": f"https://www.aptosnames.com/name/{domain}", "matched": name,
                        "wallets": [addr], "extra": {"domain": name}
                    }
        except Exception:
            pass
    return {"found": False, "platform": "Aptos Names (.apt)", "emoji": "🟦"}


async def check_sui_names(client, username, variants):
    """Sui Name Service — .sui домены"""
    sui_names = [d for d in variants["domains"] if d.endswith(".sui")]
    for name in sui_names[:3]:
        domain = name[:-4]
        try:
            r = await client.get(
                f"https://api.suins.io/api/v1/name/{domain}.sui",
                timeout=10)
            if r.status_code == 200:
                data = r.json()
                addr = data.get("target_address") or data.get("owner")
                if addr:
                    return {
                        "found": True, "platform": "Sui Names (.sui)", "emoji": "🌀",
                        "url": f"https://suins.io/name/{domain}", "matched": name,
                        "wallets": [addr], "extra": {"domain": name}
                    }
        except Exception:
            pass
    return {"found": False, "platform": "Sui Names (.sui)", "emoji": "🌀"}


# ─── Список всех платформ (после всех функций) ───────────────────────────────

async def check_ens(client, username, variants):
    """ENS lookup with multiple free fallbacks for unstable public endpoints."""
    eth_names = [d for d in variants["domains"] if d.endswith(".eth")]
    for name in eth_names[:3]:
        try:
            r = await client.post(
                "https://api.thegraph.com/subgraphs/name/ensdomains/ens",
                json={"query": f'{{domains(where:{{name:"{name}"}}){{name owner{{id}} resolvedAddress{{id}}}}}}'},
                timeout=10,
            )
            if r.status_code == 200:
                doms = r.json().get("data", {}).get("domains", [])
                if doms:
                    domain = doms[0]
                    wallet = (domain.get("resolvedAddress") or {}).get("id") or (domain.get("owner") or {}).get("id")
                    if wallet and is_real_wallet(wallet):
                        return {"found": True, "platform": "ENS (.eth)", "emoji": "🔷",
                                "url": f"https://app.ens.domains/{name}", "matched": name,
                                "wallets": [wallet], "extra": {"domain": name, "source": "thegraph"}}
            elif r.status_code in (429, 500, 502, 503, 504):
                log.debug("ens graph temporary failure name=%s status=%s", name, r.status_code)
        except Exception:
            log.debug("ens graph check failed name=%s", name, exc_info=True)

        try:
            r = await client.get(f"https://api.ensideas.com/ens/resolve/{name}", timeout=8)
            if r.status_code == 200:
                wallet = r.json().get("address")
                if wallet and is_real_wallet(wallet):
                    return {"found": True, "platform": "ENS (.eth)", "emoji": "🔷",
                            "url": f"https://app.ens.domains/{name}", "matched": name,
                            "wallets": [wallet], "extra": {"domain": name, "source": "ensideas"}}
        except Exception:
            log.debug("ensideas fallback failed name=%s", name, exc_info=True)

        try:
            r = await client.get(
                f"https://api.web3.bio/profile/{name}",
                headers=web3bio_headers(),
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                profiles = data if isinstance(data, list) else [data]
                for profile in profiles:
                    identity = (profile.get("identity") or profile.get("handle") or "").lower()
                    platform = (profile.get("platform") or "").lower()
                    wallet = profile.get("address")
                    if identity == name.lower() and platform == "ens" and wallet and is_real_wallet(wallet):
                        return {"found": True, "platform": "ENS (.eth)", "emoji": "🔷",
                                "url": f"https://app.ens.domains/{name}", "matched": name,
                                "wallets": [wallet], "extra": {"domain": name, "source": "web3.bio"}}
        except Exception:
            log.debug("ens web3bio fallback failed name=%s", name, exc_info=True)

    return {"found": False, "platform": "ENS (.eth)", "emoji": "🔷"}


PLATFORMS = [
    # Tier 1 — быстрые, высокий hit rate
    check_farcaster,
    check_ens,
    check_web3bio,
    check_lens,
    # Tier 2 — средние
    check_sns,
    check_unstoppable,
    check_spaceid,
    check_basename,       # новый — Base Names
    # Tier 3 — нишевые но полезные
    check_github,
    check_gitcoin,
    check_snapshot,
    check_blur_api,
    check_opensea_v2,     # новый — OpenSea API v2 (нужен OPENSEA_KEY)
]

BULK_PLATFORMS = [
    check_ens,
    check_web3bio,
    check_farcaster,
    check_lens,
    check_sns,
    check_spaceid,
    check_basename,
    check_opensea_v2,
]

_BULK_PLATFORM_SEMAPHORES = {
    fn.__name__: asyncio.Semaphore(BULK_SOURCE_CONCURRENCY)
    for fn in BULK_PLATFORMS
}

def _cache_key(username: str) -> str:
    return username.strip().lower()


def _get_cached(username: str) -> dict | None:
    if SEARCH_CACHE_SECONDS <= 0:
        return None
    entry = _SEARCH_CACHE.get(_cache_key(username))
    if not entry:
        return None
    created, data = entry
    if time.time() - created > SEARCH_CACHE_SECONDS:
        _SEARCH_CACHE.pop(_cache_key(username), None)
        return None
    cached = copy.deepcopy(data)
    cached["cache_hit"] = True
    return cached


def _set_cached(username: str, data: dict):
    if SEARCH_CACHE_SECONDS <= 0:
        return
    key = _cache_key(username)
    existing = _SEARCH_CACHE.get(key)
    if existing:
        _, old_data = existing
        data = _merge_search_data(old_data, data)
    _SEARCH_CACHE[key] = (time.time(), copy.deepcopy(data))
    if len(_SEARCH_CACHE) > 512:
        oldest = sorted(_SEARCH_CACHE.items(), key=lambda item: item[1][0])[:128]
        for key, _ in oldest:
            _SEARCH_CACHE.pop(key, None)


def _get_bulk_cached(username: str) -> dict | None:
    entry = _BULK_SEARCH_CACHE.get(_cache_key(username))
    if not entry:
        return None
    created, data = entry
    if SEARCH_CACHE_SECONDS <= 0 or time.time() - created > SEARCH_CACHE_SECONDS:
        _BULK_SEARCH_CACHE.pop(_cache_key(username), None)
        return None
    cached = copy.deepcopy(data)
    cached["cache_hit"] = True
    cached["bulk_mode"] = True
    return cached


def _set_bulk_cached(username: str, data: dict):
    if SEARCH_CACHE_SECONDS <= 0:
        return
    key = _cache_key(username)
    existing = _BULK_SEARCH_CACHE.get(key)
    if existing:
        _, old_data = existing
        data = _merge_search_data(old_data, data)
        data["bulk_mode"] = True
        data["bulk_complete"] = bool(
            old_data.get("bulk_complete") or data.get("bulk_complete")
        )
    _BULK_SEARCH_CACHE[key] = (time.time(), copy.deepcopy(data))
    if len(_BULK_SEARCH_CACHE) > 512:
        oldest = sorted(_BULK_SEARCH_CACHE.items(), key=lambda item: item[1][0])[:128]
        for key, _ in oldest:
            _BULK_SEARCH_CACHE.pop(key, None)


def _result_identity(result: dict) -> tuple[str, str]:
    return (
        str(result.get("platform") or "").lower(),
        str(result.get("matched") or "").lower(),
    )


def _merge_search_data(old_data: dict, new_data: dict) -> dict:
    merged = copy.deepcopy(new_data)
    results = list(merged.get("results") or [])
    seen = {_result_identity(r) for r in results if r.get("found")}
    new_wallets = set(merged.get("all_wallets") or [])

    for old_result in old_data.get("results") or []:
        if not old_result.get("found") or not old_result.get("wallets"):
            continue
        identity = _result_identity(old_result)
        old_wallets = set(old_result.get("wallets") or [])
        if identity in seen or old_wallets.issubset(new_wallets):
            continue
        restored = copy.deepcopy(old_result)
        restored["restored_from_cache"] = True
        results.insert(0, restored)
        seen.add(identity)
        new_wallets.update(old_wallets)

    found = [r for r in results if r.get("found")]
    not_found = [r for r in results if not r.get("found")]
    merged["results"] = found + not_found
    merged["found_count"] = len(found)
    merged["all_wallets"] = list(dict.fromkeys(
        w for r in found for w in (r.get("wallets") or []) if w
    ))
    return merged


def _clean_result(result: dict, username: str, elapsed_ms: int, error: str | None = None) -> dict:
    if not result:
        result = {"found": False, "platform": "Unknown", "emoji": "?"}

    result = dict(result)
    result["elapsed_ms"] = elapsed_ms

    if error:
        result["found"] = False
        result["error"] = error

    wallets = []
    for wallet in result.get("wallets") or []:
        if not wallet:
            continue
        wallet = str(wallet).strip()
        if wallet.lower() in {"domain not found", "not found", "none", "null"}:
            continue
        if wallet.startswith("0x") and not is_real_wallet(wallet):
            continue
        if is_eth_address(wallet):
            wallet = wallet.lower()
        wallets.append(wallet)
    result["wallets"] = list(dict.fromkeys(wallets))
    return result


def _platform_cache_values(username: str, variants: dict, result: dict | None = None) -> set[str]:
    values = {username.strip().lower()}
    values.update(str(v).lower() for v in variants.get("base", []) if v)
    values.update(str(v).lower() for v in variants.get("domains", []) if v)
    if result:
        matched = result.get("matched")
        if matched:
            values.add(str(matched).lower())
    return values


def _remember_platform_hit(fn_name: str, username: str, variants: dict, result: dict):
    if not result.get("found") or not result.get("wallets"):
        return
    payload = copy.deepcopy(result)
    now = time.time()
    for value in _platform_cache_values(username, variants, result):
        _PLATFORM_HIT_CACHE[(fn_name, value)] = (now, payload)
    if len(_PLATFORM_HIT_CACHE) > 2048:
        oldest = sorted(_PLATFORM_HIT_CACHE.items(), key=lambda item: item[1][0])[:512]
        for key, _ in oldest:
            _PLATFORM_HIT_CACHE.pop(key, None)


def _get_platform_hit(fn_name: str, username: str, variants: dict) -> dict | None:
    now = time.time()
    for value in _platform_cache_values(username, variants):
        entry = _PLATFORM_HIT_CACHE.get((fn_name, value))
        if not entry:
            continue
        created, result = entry
        if now - created > PLATFORM_HIT_CACHE_SECONDS:
            _PLATFORM_HIT_CACHE.pop((fn_name, value), None)
            continue
        cached = copy.deepcopy(result)
        cached["platform_cache_hit"] = True
        return cached
    return None


async def _run_platform(fn, client, username: str, variants: dict, platform_timeout: int | None = None) -> dict:
    started = time.perf_counter()
    timeout = platform_timeout or PLATFORM_TIMEOUT_SECONDS
    tracked = _TrackedClient(client)
    try:
        result = await asyncio.wait_for(
            fn(tracked, username, variants),
            timeout=timeout,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        cleaned = _clean_result(result, username, elapsed_ms)
        if not cleaned.get("found") and tracked.unavailable:
            cleaned["error"] = "source_unavailable"
        if not cleaned.get("found"):
            cached_hit = _get_platform_hit(fn.__name__, username, variants)
            if cached_hit:
                cached_hit["elapsed_ms"] = elapsed_ms
                return cached_hit
        if cleaned.get("found"):
            _remember_platform_hit(fn.__name__, username, variants, cleaned)
            log.info(
                "search hit platform=%s matched=%s wallets=%s elapsed_ms=%s",
                cleaned.get("platform"),
                cleaned.get("matched"),
                len(cleaned.get("wallets") or []),
                elapsed_ms,
            )
        return cleaned
    except asyncio.TimeoutError:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.warning("search timeout platform=%s username=%s", fn.__name__, username)
        return _clean_result(
            {"found": False, "platform": fn.__name__.replace("check_", ""), "emoji": "⏱"},
            username,
            elapsed_ms,
            "timeout",
        )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.exception("search error platform=%s username=%s", fn.__name__, username)
        return _clean_result(
            {"found": False, "platform": fn.__name__.replace("check_", ""), "emoji": "⚠"},
            username,
            elapsed_ms,
            str(exc)[:120],
        )


async def _run_bulk_platform(fn, client, username: str, variants: dict, platform_timeout: int | None = None) -> dict:
    semaphore = _BULK_PLATFORM_SEMAPHORES[fn.__name__]
    async with semaphore:
        return await _run_platform(
            fn,
            client,
            username,
            variants,
            platform_timeout=platform_timeout or BULK_PLATFORM_TIMEOUT_SECONDS,
        )


async def run_search(username: str) -> dict:
    # Позитивный кэш
    cached = _get_cached(username)
    if cached:
        return cached

    variants = get_variants(username)
    started  = time.perf_counter()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
        results = await asyncio.gather(
            *[_run_platform(fn, client, username, variants) for fn in PLATFORMS],
        )

    found = sorted(
        [r for r in results if r.get("found")],
        key=lambda r: len(r.get("wallets") or []),
        reverse=True,
    )
    not_found = [r for r in results if not r.get("found")]
    ordered_results = found + not_found
    wallets = list(dict.fromkeys(
        w for r in found for w in (r.get("wallets") or []) if w
    ))
    diagnostics = {
        "elapsed_ms":       int((time.perf_counter() - started) * 1000),
        "platforms_checked": len(PLATFORMS),
        "errors": [
            {"platform": r.get("platform"), "error": r.get("error")}
            for r in ordered_results if r.get("error")
        ],
    }

    data = {
        "username":        username,
        "variants":        variants["base"],
        "domains_checked": variants["domains"],
        "results":         ordered_results,
        "found_count":     len(found),
        "all_wallets":     wallets,
        "diagnostics":     diagnostics,
        "cache_hit":       False,
    }

    if len(found) > 0:
        _set_cached(username, data)

    return data


def _build_search_response(username: str, variants: dict, results: list[dict], started: float, cache_hit: bool = False) -> dict:
    found = sorted(
        [r for r in results if r.get("found")],
        key=lambda r: len(r.get("wallets") or []),
        reverse=True,
    )
    not_found = [r for r in results if not r.get("found")]
    ordered_results = found + not_found
    wallets = list(dict.fromkeys(
        w for r in found for w in (r.get("wallets") or []) if w
    ))
    return {
        "username":        username,
        "variants":        variants["base"],
        "domains_checked": variants["domains"],
        "results":         ordered_results,
        "found_count":     len(found),
        "all_wallets":     wallets,
        "diagnostics": {
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "platforms_checked": len(results),
            "errors": [
                {"platform": r.get("platform"), "error": r.get("error")}
                for r in ordered_results if r.get("error")
            ],
        },
        "cache_hit":       cache_hit,
    }


async def run_bulk_search(username: str) -> dict:
    """
    Полный режим для TXT/CSV bulk.
    Надёжные источники проверяются параллельно; временный сбой ENS повторяется,
    а неполные ответы с ошибками источников не закрепляются в bulk-кэше.
    """
    cached = _get_cached(username)
    if cached:
        cached["bulk_mode"] = True
        return cached
    cached = _get_bulk_cached(username)
    if cached:
        return cached

    variants = get_variants(username)
    started = time.perf_counter()
    results = []

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=BULK_USERNAME_TIMEOUT_SECONDS) as client:
        results = await asyncio.gather(*[
            _run_bulk_platform(fn, client, username, variants)
            for fn in BULK_PLATFORMS
        ])

        ens_index = BULK_PLATFORMS.index(check_ens)
        ens_result = results[ens_index]
        if not ens_result.get("wallets") and ens_result.get("error"):
            if BULK_ENS_RETRY_DELAY_SECONDS:
                await asyncio.sleep(BULK_ENS_RETRY_DELAY_SECONDS)
            ens_retry = await _run_bulk_platform(
                check_ens,
                client,
                username,
                variants,
                platform_timeout=BULK_ENS_RETRY_TIMEOUT_SECONDS,
            )
            if ens_retry.get("wallets") or not ens_retry.get("error"):
                results[ens_index] = ens_retry

    data = _build_search_response(username, variants, results, started)
    data["bulk_mode"] = True
    data["bulk_complete"] = not any(result.get("error") for result in results)
    data["diagnostics"]["ens_retried"] = bool(
        ens_result.get("error") and not ens_result.get("wallets")
    )
    if data["all_wallets"] and data["bulk_complete"]:
        _set_bulk_cached(username, data)
    return data


# ─── GoldRush: баланс кошелька в USD ──────────────────────────────────────────

def _is_evm(addr: str) -> bool:
    return isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42


def _is_solana(addr: str) -> bool:
    return bool(isinstance(addr, str) and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr.strip()))


def _balance_cached(addr: str):
    item = _BALANCE_CACHE.get(addr.lower())
    if item and time.time() - item[0] < BALANCE_CACHE_SECONDS:
        return item[1]
    return None


def _balance_set_cache(addr: str, data: dict):
    _BALANCE_CACHE[addr.lower()] = (time.time(), data)


def _blank_balance(addr: str, note: str = "") -> dict:
    return {"address": addr, "balance_usd": None, "top_tokens": [], "chains": [], "note": note}


def _safe_usd_quote(item: dict) -> float | None:
    try:
        quote = float(item.get("quote"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(quote) or quote < 0 or quote > BALANCE_MAX_TOKEN_USD:
        return None
    return quote


async def _get_free_prices(client: httpx.AsyncClient) -> dict[str, float]:
    global _FREE_PRICE_CACHE, _FREE_PRICE_INFLIGHT
    now = time.time()
    if _FREE_PRICE_CACHE and now - _FREE_PRICE_CACHE[0] < FREE_PRICE_CACHE_SECONDS:
        return dict(_FREE_PRICE_CACHE[1])

    if _FREE_PRICE_INFLIGHT is None:
        async def fetch() -> dict[str, float]:
            ids = list(dict.fromkeys(FREE_PRICE_IDS.values()))
            url = "https://coins.llama.fi/prices/current/" + ",".join(ids)
            try:
                response = await client.get(url, timeout=FREE_RPC_TIMEOUT)
                response.raise_for_status()
                coins = (response.json() or {}).get("coins") or {}
            except Exception as exc:
                log.warning("free price fetch failed: %s", exc)
                return {}

            prices: dict[str, float] = {}
            for symbol, coin_id in FREE_PRICE_IDS.items():
                try:
                    price = float((coins.get(coin_id) or {}).get("price"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(price) and price > 0:
                    prices[symbol] = price
            return prices

        _FREE_PRICE_INFLIGHT = asyncio.create_task(fetch())

    task = _FREE_PRICE_INFLIGHT
    try:
        prices = await asyncio.shield(task)
        if prices:
            _FREE_PRICE_CACHE = (time.time(), dict(prices))
        return dict(prices)
    finally:
        if task.done() and _FREE_PRICE_INFLIGHT is task:
            _FREE_PRICE_INFLIGHT = None


async def _get_solana_token_prices(
    client: httpx.AsyncClient,
    mints: list[str],
) -> dict[str, tuple[float, str]]:
    now = time.time()
    result: dict[str, tuple[float, str]] = {}
    missing: list[str] = []
    for mint in dict.fromkeys(mints):
        cached = _FREE_TOKEN_PRICE_CACHE.get(mint)
        if cached and now - cached[0] < FREE_PRICE_CACHE_SECONDS:
            if cached[1] is not None:
                result[mint] = (cached[1], cached[2])
            continue
        missing.append(mint)

    for start in range(0, len(missing), 30):
        chunk = missing[start:start + 30]
        ids = [f"solana:{mint}" for mint in chunk]
        url = "https://coins.llama.fi/prices/current/" + ",".join(ids)
        try:
            async with _FREE_PRICE_SEMAPHORE:
                response = await client.get(url, timeout=FREE_RPC_TIMEOUT)
            response.raise_for_status()
            coins = (response.json() or {}).get("coins") or {}
        except Exception as exc:
            log.debug("Solana token price fetch failed: %s", exc)
            continue

        checked_at = time.time()
        for mint, coin_id in zip(chunk, ids):
            coin = coins.get(coin_id) or {}
            try:
                price = float(coin.get("price"))
            except (TypeError, ValueError):
                price = None
            if price is not None and (not math.isfinite(price) or price <= 0):
                price = None
            symbol = str(coin.get("symbol") or mint[:4])
            _FREE_TOKEN_PRICE_CACHE[mint] = (checked_at, price, symbol)
            if price is not None:
                result[mint] = (price, symbol)
    return result


def _balance_of_data(address: str) -> str:
    return "0x70a08231" + address.lower().removeprefix("0x").rjust(64, "0")


def _hex_amount(value) -> int:
    if not isinstance(value, str) or value in ("", "0x"):
        return 0
    try:
        return int(value, 16)
    except ValueError:
        return 0


def _format_usd_value(value: float) -> str:
    if value >= 1000:
        return f"${value:,.0f}"
    if value >= 0.01:
        return f"${value:,.2f}"
    return f"${value:,.4f}"


async def _rpc_post(client: httpx.AsyncClient, url: str, payload):
    async with _FREE_RPC_SEMAPHORE:
        response = await client.post(url, json=payload, timeout=FREE_RPC_TIMEOUT)
    response.raise_for_status()
    return response.json()


async def _rpc_results(
    client: httpx.AsyncClient,
    url: str,
    requests: list[dict],
) -> dict[int, str]:
    try:
        payload = await _rpc_post(client, url, requests)
    except Exception:
        payload = None

    if isinstance(payload, list):
        results = {
            int(item.get("id")): item.get("result")
            for item in payload
            if isinstance(item, dict) and item.get("result") is not None
        }
        if 1 in results:
            return results

    async def one(request: dict):
        try:
            item = await _rpc_post(client, url, request)
        except Exception:
            return request["id"], None
        return request["id"], item.get("result") if isinstance(item, dict) else None

    pairs = await asyncio.gather(*(one(request) for request in requests))
    return {int(request_id): result for request_id, result in pairs if result is not None}


async def _fetch_free_chain(
    client: httpx.AsyncClient,
    chain: str,
    config: dict,
    address: str,
) -> dict | None:
    requests = [{
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"],
    }]
    call_data = _balance_of_data(address)
    for index, (_, contract, _, _) in enumerate(config["tokens"], start=2):
        requests.append({
            "jsonrpc": "2.0",
            "id": index,
            "method": "eth_call",
            "params": [{"to": contract, "data": call_data}, "latest"],
        })

    for url in config["rpcs"]:
        results = await _rpc_results(client, url, requests)
        if 1 not in results:
            continue
        tokens = []
        for index, (symbol, _, decimals, price_key) in enumerate(config["tokens"], start=2):
            amount = _hex_amount(results.get(index)) / (10 ** decimals)
            if amount > 0:
                tokens.append({"symbol": symbol, "amount": amount, "price": price_key})
        return {
            "chain": chain,
            "native_symbol": config["native"],
            "native_price": config["price"],
            "native_amount": _hex_amount(results[1]) / 1_000_000_000_000_000_000,
            "tokens": tokens,
        }
    return None


async def fetch_free_evm_balance(client: httpx.AsyncClient, address: str) -> dict:
    cached = _balance_cached(address)
    if cached is not None:
        return cached

    prices_task = asyncio.create_task(_get_free_prices(client))
    chain_results = await asyncio.gather(*(
        _fetch_free_chain(client, chain, config, address)
        for chain, config in FREE_EVM_CHAINS.items()
    ))
    prices = await prices_task
    available = [result for result in chain_results if result is not None]
    if not available:
        return _blank_balance(address, "free_rpc_unavailable")

    total_usd = 0.0
    has_unpriced = False
    positive_chains: list[str] = []
    positions: dict[str, float] = {}
    unpriced_positions: list[str] = []

    for result in available:
        chain_positive = False
        native_amount = float(result["native_amount"] or 0)
        if native_amount > 0:
            chain_positive = True
            symbol = result["native_symbol"]
            price = prices.get(result["native_price"])
            if price:
                usd = native_amount * price
                total_usd += usd
                positions[symbol] = positions.get(symbol, 0.0) + usd
            else:
                has_unpriced = True
                unpriced_positions.append(f"{symbol} {native_amount:,.4f}")

        for token in result["tokens"]:
            chain_positive = True
            price = 1.0 if token["price"] == "USD" else prices.get(token["price"])
            if price:
                usd = token["amount"] * price
                total_usd += usd
                positions[token["symbol"]] = positions.get(token["symbol"], 0.0) + usd
            else:
                has_unpriced = True
                unpriced_positions.append(f"{token['symbol']} {token['amount']:,.4f}")

        if chain_positive:
            positive_chains.append(result["chain"])

    top = sorted(
        (item for item in positions.items() if item[1] >= 0.01),
        key=lambda item: item[1],
        reverse=True,
    )
    top_tokens = [f"{symbol} {_format_usd_value(usd)}" for symbol, usd in top[:5]]
    if len(top_tokens) < 5:
        top_tokens.extend(unpriced_positions[:5 - len(top_tokens)])

    any_position = bool(positions or unpriced_positions)
    result = {
        "address": address,
        "balance_usd": round(total_usd, 2) if (positions or not any_position) else None,
        "top_tokens": top_tokens,
        "chains": positive_chains,
        "note": "partial_price_data" if has_unpriced else "free_rpc",
    }
    _balance_set_cache(address, result)
    return result


async def _goldrush_json(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    params: dict | None = None,
) -> tuple[dict | None, str]:
    """Request GoldRush with retry and reject API error envelopes."""
    global _GOLDRUSH_NEXT_REQUEST
    last_note = "goldrush_error"
    for attempt in range(GOLDRUSH_RETRIES):
        async with _GOLDRUSH_RATE_LOCK:
            now = time.monotonic()
            delay = max(0.0, _GOLDRUSH_NEXT_REQUEST - now)
            if delay:
                await asyncio.sleep(delay)
            _GOLDRUSH_NEXT_REQUEST = max(time.monotonic(), _GOLDRUSH_NEXT_REQUEST) + (1 / GOLDRUSH_RPS)
        try:
            response = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=GOLDRUSH_TIMEOUT,
            )
        except httpx.TimeoutException:
            last_note = "goldrush_timeout"
        except Exception as exc:
            last_note = f"goldrush_error:{str(exc)[:60]}"
        else:
            if response.status_code == 429:
                last_note = "goldrush_rate_limited"
            elif response.status_code >= 500:
                last_note = f"goldrush_http_{response.status_code}"
            elif response.status_code >= 400:
                return None, f"goldrush_http_{response.status_code}"
            else:
                try:
                    payload = response.json()
                except ValueError:
                    last_note = "goldrush_bad_json"
                else:
                    if not isinstance(payload, dict):
                        last_note = "goldrush_bad_payload"
                    elif payload.get("error"):
                        message = payload.get("error_message") or payload.get("error_code") or "api_error"
                        last_note = f"goldrush_api_error:{str(message)[:60]}"
                    elif not isinstance(payload.get("data"), dict):
                        last_note = "goldrush_missing_data"
                    else:
                        return payload, ""

        if attempt < GOLDRUSH_RETRIES - 1:
            await asyncio.sleep(0.5 * (2 ** attempt))

    return None, last_note


async def _fetch_sol_price_usd(client: httpx.AsyncClient) -> float | None:
    return (await _get_free_prices(client)).get("SOL")


async def _solana_rpc(client: httpx.AsyncClient, method: str, params: list) -> dict:
    resp = await client.post(
        SOLANA_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=SOLANA_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:160])
    return payload.get("result") or {}


async def fetch_solana_goldrush_balance(client: httpx.AsyncClient, address: str) -> dict | None:
    """Solana wallet balance through GoldRush, enabled by GOLDRUSH_SOLANA=1."""
    if not GOLDRUSH_SOLANA_ENABLED or not GOLDRUSH_API_KEY:
        return None

    url = f"https://api.covalenthq.com/v1/solana-mainnet/address/{address}/balances_v2/"
    headers = {**HEADERS, "Authorization": f"Bearer {GOLDRUSH_API_KEY}"}
    payload, error = await _goldrush_json(client, url, headers)
    if payload is None:
        log.warning("GoldRush Solana failed address=%s error=%s", address, error)
        return None

    items = ((payload or {}).get("data") or {}).get("items") or []
    total = 0.0
    valid_quotes = 0
    tokens: list[tuple[str, float, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = _safe_usd_quote(item)
        sym = item.get("contract_ticker_symbol") or item.get("contract_name") or "?"
        pretty = item.get("pretty_quote")
        if quote is not None:
            valid_quotes += 1
            total += quote
            if quote >= 1:
                tokens.append((str(sym), quote, str(pretty or "")))

    if items and valid_quotes == 0:
        return _blank_balance(address, "no_valid_quotes")
    tokens.sort(key=lambda token: token[1], reverse=True)
    result = {
        "address": address,
        "balance_usd": round(total, 2),
        "top_tokens": [
            f"{symbol} ${quote:,.0f}" if not pretty else f"{symbol} {pretty}"
            for symbol, quote, pretty in tokens[:5]
        ],
        "chains": ["solana"],
        "note": "goldrush_solana",
    }
    _balance_set_cache(address, result)
    return result


async def fetch_solana_balance(client: httpx.AsyncClient, address: str) -> dict:
    """Native SOL + USDC SPL balance for a Solana wallet."""
    cached = _balance_cached(address)
    if cached is not None:
        return cached
    if not _is_solana(address):
        return _blank_balance(address, "non_solana")

    if BALANCE_PROVIDER in ("goldrush", "auto"):
        goldrush_result = await fetch_solana_goldrush_balance(client, address)
        if goldrush_result is not None and goldrush_result.get("note") not in {
            "goldrush_solana_rate_limited",
            "goldrush_solana_timeout",
        }:
            return goldrush_result

    raw = await asyncio.gather(
        _solana_rpc(client, "getBalance", [address, {"commitment": "confirmed"}]),
        _solana_rpc(client, "getTokenAccountsByOwner", [
            address,
            {"programId": SOLANA_TOKEN_PROGRAM},
            {"encoding": "jsonParsed"},
        ]),
        _solana_rpc(client, "getTokenAccountsByOwner", [
            address,
            {"programId": SOLANA_TOKEN_2022_PROGRAM},
            {"encoding": "jsonParsed"},
        ]),
        _fetch_sol_price_usd(client),
        return_exceptions=True,
    )
    balance_result, token_result, token_2022_result, sol_price = raw
    if isinstance(balance_result, Exception):
        return _blank_balance(address, f"solana_error:{str(balance_result)[:60]}")
    if isinstance(sol_price, Exception):
        sol_price = None

    lamports = int(balance_result.get("value") or 0)
    sol_amount = lamports / 1_000_000_000
    token_amounts: dict[str, float] = {}
    for source in (token_result, token_2022_result):
        if not isinstance(source, dict):
            continue
        for account in source.get("value") or []:
            parsed = (((account.get("account") or {}).get("data") or {}).get("parsed") or {})
            info = parsed.get("info") or {}
            mint = info.get("mint")
            token_amount = info.get("tokenAmount") or {}
            try:
                amount = float(token_amount.get("uiAmountString") or token_amount.get("uiAmount") or 0)
            except (TypeError, ValueError):
                continue
            if mint and amount > 0:
                token_amounts[mint] = token_amounts.get(mint, 0.0) + amount

    selected_mints = sorted(token_amounts, key=token_amounts.get, reverse=True)[:FREE_SOLANA_MAX_TOKENS]
    price_data = await _get_solana_token_prices(
        client,
        [mint for mint in selected_mints if mint != SOLANA_USDC_MINT],
    )

    positions: list[tuple[str, float]] = []
    total_usd = 0.0
    if sol_amount > 0 and sol_price:
        sol_usd = sol_amount * sol_price
        total_usd += sol_usd
        positions.append(("SOL", sol_usd))
    for mint in selected_mints:
        amount = token_amounts[mint]
        if mint == SOLANA_USDC_MINT:
            price, symbol = 1.0, "USDC"
        else:
            priced = price_data.get(mint)
            if not priced:
                continue
            price, symbol = priced
        usd = amount * price
        if not math.isfinite(usd) or usd <= 0 or usd > BALANCE_MAX_TOKEN_USD:
            continue
        total_usd += usd
        positions.append((symbol, usd))

    positions.sort(key=lambda item: item[1], reverse=True)
    top_tokens = [
        f"{symbol} {_format_usd_value(usd)}"
        for symbol, usd in positions[:5] if usd >= 0.01
    ]
    has_assets = sol_amount > 0 or bool(token_amounts)
    balance_usd = round(total_usd, 2) if positions or not has_assets else None

    result = {
        "address": address,
        "balance_usd": balance_usd,
        "top_tokens": top_tokens,
        "chains": ["solana"],
        "note": "free_rpc" if has_assets else "empty_solana",
    }
    _balance_set_cache(address, result)
    return result


async def fetch_wallet_balance(client: httpx.AsyncClient, address: str) -> dict:
    """Суммарный баланс EVM-адреса в USD по всем сетям GOLDRUSH_CHAINS (один запрос)."""
    if not _is_evm(address):
        if _is_solana(address):
            return await fetch_solana_balance(client, address)
        return _blank_balance(address, "unsupported_chain")
    if BALANCE_PROVIDER == "free" or not GOLDRUSH_API_KEY:
        return await fetch_free_evm_balance(client, address)

    cached = _balance_cached(address)
    if cached is not None:
        return cached

    url = f"https://api.covalenthq.com/v1/allchains/address/{address}/balances/"
    params = {"chains": GOLDRUSH_CHAINS, "limit": 100}
    headers = {**HEADERS, "Authorization": f"Bearer {GOLDRUSH_API_KEY}"}
    payload, error = await _goldrush_json(client, url, headers, params=params)
    if payload is None:
        log.warning("GoldRush EVM failed address=%s error=%s", address, error)
        return _blank_balance(address, error)

    items = ((payload or {}).get("data") or {}).get("items") or []
    total = 0.0
    valid_quotes = 0
    chains: list[str] = []
    tokens: list[tuple[str, float]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        quote = _safe_usd_quote(it)
        if quote is not None:
            valid_quotes += 1
            total += quote
            sym = it.get("contract_ticker_symbol") or "?"
            if quote >= 1:
                tokens.append((sym, quote))
        chain = it.get("chain_name")
        if chain and chain not in chains:
            chains.append(chain)

    if items and valid_quotes == 0:
        return _blank_balance(address, "no_valid_quotes")
    tokens.sort(key=lambda t: t[1], reverse=True)
    result = {
        "address": address,
        "balance_usd": round(total, 2),
        "top_tokens": [f"{s} ${v:,.0f}" for s, v in tokens[:5]],
        "chains": [c.replace("-mainnet", "") for c in chains],
        "note": "",
    }
    _balance_set_cache(address, result)
    return result


async def _fetch_balance_shared(client: httpx.AsyncClient, address: str) -> dict:
    cached = _balance_cached(address)
    if cached is not None:
        return cached

    key = address.lower()
    task = _BALANCE_INFLIGHT.get(key)
    if task is None:
        async def fetch():
            async with _BALANCE_SEMAPHORE:
                return await fetch_wallet_balance(client, address)

        task = asyncio.create_task(fetch())
        _BALANCE_INFLIGHT[key] = task

    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _BALANCE_INFLIGHT.get(key) is task:
            _BALANCE_INFLIGHT.pop(key, None)


async def enrich_balances(
    addresses: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict[str, dict]:
    """Return wallet balances, optionally reusing a caller-owned HTTP client."""
    uniq = list(dict.fromkeys(a for a in addresses if a))
    out: dict[str, dict] = {}
    if not uniq:
        return out

    async def run(active_client: httpx.AsyncClient):
        async def one(addr: str):
            out[addr] = await _fetch_balance_shared(active_client, addr)

        await asyncio.gather(*(one(a) for a in uniq))

    if client is not None:
        await run(client)
    else:
        timeout = max(GOLDRUSH_TIMEOUT, SOLANA_TIMEOUT)
        limits = httpx.Limits(
            max_connections=max(GOLDRUSH_CONCURRENCY, 5),
            max_keepalive_connections=max(GOLDRUSH_CONCURRENCY, 5),
        )
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            limits=limits,
        ) as owned_client:
            await run(owned_client)
    return out
