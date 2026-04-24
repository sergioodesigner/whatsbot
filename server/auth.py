"""Authentication utilities for WhatsBot web panel."""

import hashlib
import hmac
import secrets
import time


def generate_salt() -> str:
    """Generate a random hex salt."""
    return secrets.token_hex(32)


def hash_password(password: str, salt: str) -> str:
    """Hash a password with the given salt using SHA-256."""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def generate_token(password_hash: str, salt: str) -> str:
    """Generate a deterministic session token from the password hash.

    Changes automatically when the password changes.
    """
    return hashlib.sha256(
        (password_hash + salt + "session").encode("utf-8")
    ).hexdigest()


def verify_token(token: str, settings) -> bool:
    """Verify a session token against the stored password hash."""
    password_hash = settings.get("web_password_hash", "")
    salt = settings.get("web_password_salt", "")
    if not password_hash or not salt:
        return False
    expected = generate_token(password_hash, salt)
    return hmac.compare_digest(token, expected)


def auth_required(settings) -> bool:
    """Check if authentication is enabled (password is set)."""
    return bool(settings.get("web_password_hash", ""))


def generate_superadmin_delegate_token(
    password_hash: str,
    salt: str,
    tenant_slug: str,
    ttl_seconds: int = 600,
) -> str:
    """Generate a short-lived superadmin delegation token bound to one tenant."""
    expires_at = int(time.time()) + max(1, int(ttl_seconds))
    payload = f"{tenant_slug}:{expires_at}"
    key = (password_hash + salt + "delegate").encode("utf-8")
    signature = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sa1.{expires_at}.{signature}"


def verify_superadmin_delegate_token(
    token: str,
    *,
    password_hash: str,
    salt: str,
    tenant_slug: str,
) -> bool:
    """Verify delegated token signature, expiry and tenant binding."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "sa1":
        return False
    expires_at_str, sent_signature = parts[1], parts[2]
    if not expires_at_str.isdigit():
        return False
    expires_at = int(expires_at_str)
    if expires_at <= int(time.time()):
        return False
    payload = f"{tenant_slug}:{expires_at}"
    key = (password_hash + salt + "delegate").encode("utf-8")
    expected_signature = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent_signature, expected_signature)
