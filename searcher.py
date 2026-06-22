"""
Crypto OSINT — searcher.py
Только надёжные API-чекеры (без HTML парсинга — он давал одинаковые кошельки для всех)
"""

import asyncio
import copy
import logging
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
OPENSEA_API_KEY               = os.getenv("OPENSEA_KEY", "")
WEB3BIO_API_KEY               = os.getenv("WEB3BIO_API_KEY", "")

_SEARCH_CACHE:    dict[str, tuple[float, dict]] = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


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


async def check_ens(client, username, variants):
    """ENS: username.eth → Ethereum адрес через The Graph API"""
    eth_names = [d for d in variants["domains"] if d.endswith(".eth")]
    for name in eth_names[:3]:
        try:
            r = await client.post(
                "https://api.thegraph.com/subgraphs/name/ensdomains/ens",
                json={"query": f'{{domains(where:{{name:"{name}"}}){{name owner{{id}} resolvedAddress{{id}}}}}}'},
                timeout=10)
            if r.status_code == 200:
                doms = r.json().get("data", {}).get("domains", [])
                if doms:
                    d      = doms[0]
                    wallet = (d.get("resolvedAddress") or {}).get("id") or (d.get("owner") or {}).get("id")
                    if wallet and wallet != "0x0000000000000000000000000000000000000000":
                        return {"found": True, "platform": "ENS (.eth)", "emoji": "🔷",
                                "url": f"https://app.ens.domains/{name}", "matched": name,
                                "wallets": [wallet], "extra": {"домен": name}}
        except Exception:
            pass
    return {"found": False, "platform": "ENS (.eth)", "emoji": "🔷"}


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
                addr = r.json().get("result")
                if addr:
                    return {"found": True, "platform": "Solana NS (.sol)", "emoji": "🟣",
                            "url": f"https://naming.bonfida.org/#/domain/{domain}", "matched": name,
                            "wallets": [addr], "extra": {"домен": name}}
        except Exception:
            pass
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
            _rev_ens(client, address),
            _rev_farcaster(client, address),
            _rev_lens(client, address),
            _rev_web3bio(client, address),
            return_exceptions=True
        )
    results = [r for r in raw if r and not isinstance(r, Exception) and r.get("found")]
    return {"address": address, "found_count": len(results), "results": results}


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
    check_basename,
    check_opensea_v2,
]

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
    _SEARCH_CACHE[_cache_key(username)] = (time.time(), copy.deepcopy(data))
    if len(_SEARCH_CACHE) > 512:
        oldest = sorted(_SEARCH_CACHE.items(), key=lambda item: item[1][0])[:128]
        for key, _ in oldest:
            _SEARCH_CACHE.pop(key, None)


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
        if wallet.startswith("0x") and not is_real_wallet(wallet):
            continue
        wallets.append(wallet)
    result["wallets"] = list(dict.fromkeys(wallets))
    return result


async def _run_platform(fn, client, username: str, variants: dict, platform_timeout: int | None = None) -> dict:
    started = time.perf_counter()
    timeout = platform_timeout or PLATFORM_TIMEOUT_SECONDS
    try:
        result = await asyncio.wait_for(
            fn(client, username, variants),
            timeout=timeout,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        cleaned = _clean_result(result, username, elapsed_ms)
        if cleaned.get("found"):
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


async def run_bulk_search(username: str, stop_after_first_wallet: bool = True) -> dict:
    """
    Быстрый режим для TXT/CSV bulk.
    Проверяет только стабильные API, режет таймауты и по умолчанию останавливается
    после первого найденного кошелька.
    """
    cached = _get_cached(username)
    if cached:
        cached["bulk_mode"] = True
        return cached

    variants = get_variants(username)
    started = time.perf_counter()
    results = []

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=BULK_USERNAME_TIMEOUT_SECONDS) as client:
        for fn in BULK_PLATFORMS:
            result = await _run_platform(
                fn, client, username, variants,
                platform_timeout=BULK_PLATFORM_TIMEOUT_SECONDS,
            )
            results.append(result)
            if stop_after_first_wallet and result.get("wallets"):
                break

    data = _build_search_response(username, variants, results, started)
    data["bulk_mode"] = True
    if data["found_count"] > 0:
        _set_cached(username, data)
    return data
