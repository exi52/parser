"""
Балансы кошельков через публичные RPC — без API ключей
"""

import asyncio
import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Публичные RPC эндпоинты (без ключей)
ETH_RPC  = "https://eth.llamarpc.com"
BSC_RPC  = "https://bsc-dataseed.binance.org"
ARB_RPC  = "https://arb1.arbitrum.io/rpc"
POL_RPC  = "https://polygon-rpc.com"
SOL_RPC  = "https://api.mainnet-beta.solana.com"


async def rpc_call(client, url, method, params):
    """Универсальный JSON-RPC вызов"""
    try:
        r = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=8
        )
        if r.status_code == 200:
            return r.json().get("result")
    except Exception:
        pass
    return None


def wei_to_coin(wei_hex, decimals=18) -> float:
    try:
        return int(wei_hex, 16) / (10 ** decimals)
    except Exception:
        return 0.0


async def get_evm_balance(client, address: str, rpc_url: str, symbol: str) -> str:
    """Баланс на любой EVM-сети через публичный RPC"""
    result = await rpc_call(client, rpc_url, "eth_getBalance", [address, "latest"])
    if result:
        val = wei_to_coin(result)
        if val > 0.00001:
            return f"{val:.4f} {symbol}"
        return f"0 {symbol}"
    return None


async def get_sol_balance(client, address: str) -> str:
    """SOL баланс через публичный Solana RPC"""
    try:
        r = await client.post(
            SOL_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]},
            timeout=8
        )
        if r.status_code == 200:
            lamports = r.json().get("result", {}).get("value", 0)
            sol = lamports / 10**9
            if sol > 0.00001:
                return f"{sol:.4f} SOL"
            return "0 SOL"
    except Exception:
        pass
    return None


async def get_token_list(client, address: str) -> str:
    """Топ токены через DeBank публичный API"""
    try:
        r = await client.get(
            f"https://api.debank.com/token/balance_list?user_addr={address.lower()}&chain=eth",
            headers={**HEADERS, "source": "web"},
            timeout=8
        )
        if r.status_code == 200:
            tokens = r.json().get("data", []) or []
            # Фильтруем токены с ненулевым балансом
            names = [
                t.get("optimized_symbol") or t.get("symbol", "")
                for t in tokens
                if t.get("amount", 0) > 0 and t.get("optimized_symbol")
            ]
            if names:
                return ", ".join(names[:5])
    except Exception:
        pass
    return ""


async def get_wallet_info(address: str) -> dict:
    """
    Получает балансы кошелька.
    ETH-адрес (0x...): проверяет ETH + BNB + MATIC + ARB
    SOL-адрес: проверяет SOL
    """
    address = address.strip()
    info = {}

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=10) as client:

        if address.startswith("0x") and len(address) == 42:
            # EVM адрес — проверяем несколько сетей параллельно
            eth, bnb, matic, arb, tokens = await asyncio.gather(
                get_evm_balance(client, address, ETH_RPC,  "ETH"),
                get_evm_balance(client, address, BSC_RPC,  "BNB"),
                get_evm_balance(client, address, POL_RPC,  "MATIC"),
                get_evm_balance(client, address, ARB_RPC,  "ETH(ARB)"),
                get_token_list(client, address),
                return_exceptions=True
            )
            info["type"] = "EVM"
            if eth   and not isinstance(eth, Exception):   info["ETH"]      = eth
            if bnb   and not isinstance(bnb, Exception):   info["BNB"]      = bnb
            if matic and not isinstance(matic, Exception): info["MATIC"]    = matic
            if arb   and not isinstance(arb, Exception):   info["ARB"]      = arb
            if tokens and not isinstance(tokens, Exception): info["tokens"]  = tokens

        elif 32 <= len(address) <= 44 and not address.startswith("0x"):
            # Solana адрес
            sol = await get_sol_balance(client, address)
            info["type"] = "SOL"
            if sol: info["SOL"] = sol

    return info


def fmt_balance(address: str, info: dict) -> str:
    """Форматирует баланс в строку для Telegram"""
    if not info:
        return ""

    parts = []
    for key in ["ETH", "BNB", "MATIC", "ARB", "SOL"]:
        val = info.get(key)
        # Показываем только ненулевые
        if val and not val.startswith("0 "):
            parts.append(val)

    if not parts and not info.get("tokens"):
        return "  💰 пустой кошелёк"

    line = "  💰 " + " | ".join(parts) if parts else "  💰"
    if info.get("tokens"):
        line += f"  🪙 {info['tokens']}"
    return line
