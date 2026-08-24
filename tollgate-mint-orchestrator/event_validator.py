import hashlib
import json
import time
from dataclasses import dataclass
from typing import Optional

SUPPORTED_UNITS = {"sat", "usd", "eur", "B", "KB", "MB", "GB", "sec", "min", "hr", "day", "wk", "mo"}


@dataclass
class ValidationResult:
    valid: bool
    error: Optional[str] = None
    mint: Optional[dict] = None
    quote_id: Optional[str] = None
    amount: Optional[int] = None
    unit: Optional[str] = None


def _get_tag(tags: list, tag_name: str) -> Optional[str]:
    for t in tags:
        if len(t) >= 2 and t[0] == tag_name:
            return t[1]
    return None


def _compute_event_id(event: dict) -> str:
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _verify_signature(event: dict) -> bool:
    try:
        event_id = _compute_event_id(event)
        if event.get("id") != event_id:
            return False
        try:
            from pynostr.event import Event
            evt = Event(
                pubkey=event["pubkey"],
                created_at=event["created_at"],
                kind=event["kind"],
                tags=event["tags"],
                content=event["content"],
                sig=event.get("sig", ""),
            )
            evt.id = event_id
            return evt.verify()
        except ImportError:
            return _verify_schnorr_fallback(event_id, event["pubkey"], event.get("sig", ""))
    except Exception:
        return False


def _verify_schnorr_fallback(msg_hex: str, pubkey_hex: str, sig_hex: str) -> bool:
    try:
        import secp256k1
        pk = secp256k1.PublicKey(bytes.fromhex("02" + pubkey_hex), raw=True)
        return pk.schnorr_verify(
            bytes.fromhex(msg_hex),
            bytes.fromhex(sig_hex),
            None,
            raw=True,
        )
    except Exception:
        return False


class EventValidator:
    def __init__(self, registry, approval_ttl_secs: int = 300, authorized_approvers: Optional[list[str]] = None):
        self.registry = registry
        self.approval_ttl_secs = approval_ttl_secs
        self.authorized_approvers = authorized_approvers or []

    def validate(self, event: dict) -> ValidationResult:
        if event.get("kind") != 38010:
            return ValidationResult(valid=False, error="invalid kind, expected 38010")

        tags = event.get("tags", [])

        tag_t = _get_tag(tags, "t")
        if tag_t != "mint-approval":
            return ValidationResult(valid=False, error="missing or invalid t tag")

        mint_url = _get_tag(tags, "mint")
        if not mint_url:
            return ValidationResult(valid=False, error="missing mint tag")

        mint_entry = self.registry.get_mint_by_url(mint_url)
        if not mint_entry:
            return ValidationResult(valid=False, error=f"unknown mint: {mint_url}")

        quote_id = _get_tag(tags, "quote")
        if not quote_id:
            return ValidationResult(valid=False, error="missing quote tag")

        amount_str = _get_tag(tags, "amount")
        if not amount_str:
            return ValidationResult(valid=False, error="missing amount tag")
        try:
            amount = int(amount_str)
        except ValueError:
            return ValidationResult(valid=False, error="amount must be integer")
        if amount <= 0:
            return ValidationResult(valid=False, error="amount must be positive")

        unit = _get_tag(tags, "unit")
        if not unit:
            return ValidationResult(valid=False, error="missing unit tag")
        if unit not in SUPPORTED_UNITS:
            return ValidationResult(valid=False, error=f"unsupported unit: {unit}")

        pubkey = event.get("pubkey", "")
        if self.authorized_approvers:
            if pubkey not in self.authorized_approvers:
                return ValidationResult(valid=False, error="pubkey not in authorized approvers list")
        else:
            if pubkey != mint_entry.hex_pubkey:
                return ValidationResult(valid=False, error="pubkey does not match mint owner")

        created_at = event.get("created_at", 0)
        now = int(time.time())
        if abs(now - created_at) > self.approval_ttl_secs:
            return ValidationResult(valid=False, error="event too old (TTL exceeded)")

        if not _verify_signature(event):
            return ValidationResult(valid=False, error="invalid event signature")

        from dataclasses import asdict
        return ValidationResult(
            valid=True,
            mint=asdict(mint_entry),
            quote_id=quote_id,
            amount=amount,
            unit=unit,
        )