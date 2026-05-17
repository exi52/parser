"""
Система доступа — ключи без лимита запросов
"""

import json
import os
import secrets
import string
from datetime import datetime

DB_FILE = "access_db.json"


def load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"keys": {}, "users": {}}


def save_db(db: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def generate_key() -> str:
    """Генерирует бессрочный ключ без лимита"""
    db = load_db()
    chars = string.ascii_uppercase + string.digits
    key = "OSINT-" + "".join(secrets.choice(chars) for _ in range(12))
    db["keys"][key] = {
        "created": datetime.now().isoformat(),
        "activated_by_id": None,
        "activated_by_name": None,
        "searches": 0,
        "active": True,
    }
    save_db(db)
    return key


def activate_key(user_id: int, username: str, key: str) -> tuple[bool, str]:
    db = load_db()
    key = key.strip().upper()

    # Уже есть доступ?
    uid = str(user_id)
    if uid in db["users"] and db["users"][uid].get("active"):
        return False, "У тебя уже есть активный доступ!"

    if key not in db["keys"]:
        return False, "Ключ не найден. Проверь правильность."

    k = db["keys"][key]

    if not k["active"]:
        return False, "Этот ключ заблокирован."

    if k["activated_by_id"] and str(k["activated_by_id"]) != uid:
        return False, "Этот ключ уже использован другим пользователем."

    k["activated_by_id"] = user_id
    k["activated_by_name"] = username
    db["users"][uid] = {
        "user_id": user_id,
        "username": username,
        "key": key,
        "activated": datetime.now().isoformat(),
        "active": True,
        "searches": 0,
    }
    save_db(db)
    return True, "Доступ активирован! Теперь отправляй @username или адрес кошелька."


def check_access(user_id: int) -> tuple[bool, str]:
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        return False, "no_access"

    user = db["users"][uid]
    if not user.get("active"):
        return False, "blocked"

    key = user.get("key", "")
    if key not in db["keys"]:
        return False, "no_key"

    if not db["keys"][key]["active"]:
        return False, "key_revoked"

    return True, "ok"


def use_search(user_id: int):
    db = load_db()
    uid = str(user_id)
    if uid in db["users"]:
        db["users"][uid]["searches"] = db["users"][uid].get("searches", 0) + 1
        key = db["users"][uid].get("key", "")
        if key in db["keys"]:
            db["keys"][key]["searches"] = db["keys"][key].get("searches", 0) + 1
    save_db(db)


def get_user_stats(user_id: int) -> dict:
    db = load_db()
    uid = str(user_id)
    if uid not in db["users"]:
        return {}
    user = db["users"][uid]
    return {
        "searches": user.get("searches", 0),
        "activated": user.get("activated", "")[:10],
        "active": user.get("active", False),
    }


def list_keys() -> list:
    db = load_db()
    result = []
    for key, k in db["keys"].items():
        name = k.get("activated_by_name") or "—"
        uid  = k.get("activated_by_id")
        result.append({
            "key": key,
            "active": k["active"],
            "searches": k.get("searches", 0),
            "user": f"@{name}" if name != "—" else "не активирован",
            "user_id": uid,
            "created": k.get("created", "")[:10],
        })
    return result


def revoke_key(key: str) -> bool:
    db = load_db()
    key = key.strip().upper()
    if key not in db["keys"]:
        return False
    db["keys"][key]["active"] = False
    # Блокируем пользователя который использовал этот ключ
    for uid, user in db["users"].items():
        if user.get("key") == key:
            user["active"] = False
    save_db(db)
    return True


def block_user(user_id: int) -> bool:
    db = load_db()
    uid = str(user_id)
    if uid not in db["users"]:
        return False
    db["users"][uid]["active"] = False
    save_db(db)
    return True


def unblock_user(user_id: int) -> bool:
    db = load_db()
    uid = str(user_id)
    if uid not in db["users"]:
        return False
    user = db["users"][uid]
    key = user.get("key", "")
    # Разблокируем только если ключ ещё активен
    if key in db["keys"] and db["keys"][key]["active"]:
        user["active"] = True
        save_db(db)
        return True
    return False
