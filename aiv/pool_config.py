"""Credential loading without logging or persisting secret values."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import re

from dotenv import dotenv_values


@dataclass(frozen=True)
class Credential:
    alias: str
    provider: str
    group: str
    key: str = field(repr=False)
    base_url: str = field(repr=False)
    models: tuple[str, ...] = ()
    protocol: str = "chat"


@dataclass(frozen=True)
class Account:
    name: str
    provider: str
    concurrency: int = 2
    call_limit: int = 20
    token_limit: int = 200000
    currency: str = "unknown"
    budget: float | None = None
    expires_at: float | None = None
    rpm: int | None = None
    tpm: int | None = None


@dataclass
class Config:
    credentials: list[Credential]
    accounts: dict[str, Account]
    db: str = "runtime/pool.sqlite3"
    concurrency: int = 16
    max_attempts: int = 3
    timeout: float = 180
    prices: list = field(default_factory=list)


def load_config(path=".env", env=None):
    v = {**dotenv_values(path), **os.environ} if env is None else env
    if env is None:
        defaults = Path(v.get("POOL_CONFIG_FILE") or "config/pool-defaults.json")
        if defaults.exists():
            v = {**json.loads(defaults.read_text("utf-8")), **v}
    credentials, accounts, seen = [], {}, set()
    slots = []
    for prefix in ("OPENAI_NEXT", "TOKENDANCE"):
        for name in sorted(v):
            match = re.fullmatch(prefix + r"_API_KEY_(\d+)", name)
            if match and v[name]:
                slots.append((prefix, match[1], v[name]))
    if not slots and v.get("OPENAI_API_KEY"):
        slots = [("LEGACY", "1", v["OPENAI_API_KEY"])]
    for prefix, number, key in slots:
        if key in seen:
            continue
        seen.add(key)
        legacy = prefix == "LEGACY"
        provider = "next" if prefix == "OPENAI_NEXT" else prefix.lower()
        base = v.get("OPENAI_BASE_URL" if legacy else prefix + "_BASE_URL", "") or ""
        parsed = urlsplit(base)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{provider}: configure a clean API base URL")
        group = v.get(prefix + "_GROUP_" + number) or provider + "_shared"
        if not re.fullmatch(r"[a-zA-Z0-9_]+", group):
            raise ValueError("Account group must use letters, digits or underscores")
        models = (
            v.get(prefix + "_MODELS")
            or (
                v.get("LITERATURE_MODEL") or v.get("DOCUMENT_TEXT_BALANCED_MODEL")
                if legacy
                else ""
            )
            or ""
        )
        protocol = (
            v.get("LITERATURE_API_MODE" if legacy else prefix + "_PROTOCOL") or "chat"
        )
        if protocol not in ("chat", "responses"):
            raise ValueError("Supported protocols: chat, responses")
        credentials.append(
            Credential(
                f"{provider}_{number}",
                provider,
                group,
                key,
                base,
                tuple(m.strip() for m in models.split(",") if m.strip()),
                protocol,
            )
        )
        tag = "POOL_GROUP_" + group.upper() + "_"

        def number_value(name, default, cast=int):
            raw = v.get(tag + name)
            return cast(raw) if raw not in (None, "") else default

        expiry = v.get(tag + "EXPIRES_AT")
        expiry_ts = None
        if expiry:
            date = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("Expiry must have an explicit timezone")
            expiry_ts = date.timestamp()
        account = Account(
            group,
            provider,
            number_value("CONCURRENCY", 2),
            number_value("CALL_LIMIT", 20),
            number_value("TOKEN_LIMIT", 200000),
            v.get(tag + "CURRENCY") or "unknown",
            number_value("BUDGET", None, float),
            expiry_ts,
            number_value("RPM", None),
            number_value("TPM", None),
        )
        if (
            any(
                x <= 0
                for x in (account.concurrency, account.call_limit, account.token_limit)
            )
            or account.concurrency > 4
        ):
            raise ValueError("Account limits must be positive; concurrency at most 4")
        if account.budget is not None and (
            account.budget <= 0 or account.currency == "unknown"
        ):
            raise ValueError(
                "Monetary budget requires positive amount and known currency"
            )
        if group in accounts and accounts[group] != account:
            raise ValueError("Shared account has conflicting settings or providers")
        accounts[group] = account
    price_file = Path(v.get("POOL_PRICE_FILE") or "config/pool-prices.json")
    prices = json.loads(price_file.read_text("utf-8")) if price_file.exists() else []
    config = Config(
        credentials,
        accounts,
        v.get("POOL_DB") or "runtime/pool.sqlite3",
        int(v.get("POOL_GLOBAL_CONCURRENCY") or 16),
        int(v.get("POOL_MAX_ATTEMPTS") or 3),
        float(v.get("POOL_TIMEOUT_SECONDS") or 180),
        prices,
    )
    if (
        not 1 <= config.concurrency <= 16
        or not 1 <= config.max_attempts <= 3
        or not 0 < config.timeout <= 600
    ):
        raise ValueError("Invalid pool concurrency, attempt count or timeout")
    return config
