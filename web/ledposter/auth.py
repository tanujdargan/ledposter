"""Passwords, sessions, API tokens and login rate limiting; stdlib only.

Passwords: scrypt with a random salt, stored as "scrypt$N$r$p$salt$hash".
Sessions: HMAC-SHA256 signed cookie "expiry.fingerprint.signature". The fingerprint
is derived from the password hash, so changing the password signs everyone out.
Tokens: random, shown once, stored as SHA-256 (a random 256-bit token needs no slow hash).
"""
import base64
import collections
import hashlib
import hmac
import os
import secrets
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_TTL = 30 * 86400
TOKEN_PREFIX = "lp_"


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${h.hex()}"


def verify_password(password, stored):
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(h) // 2)
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(got, bytes.fromhex(h))


def load_secret(data_dir):
    """Per-install signing key in $LEDPOSTER_DATA/secret, created on first start."""
    p = os.path.join(data_dir, "secret")
    try:
        with open(p) as f:
            s = bytes.fromhex(f.read().strip())
        if len(s) >= 32:
            return s
    except (OSError, ValueError):
        pass
    s = secrets.token_bytes(32)
    os.makedirs(data_dir, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(s.hex())
    return s


def _fingerprint(password_hash):
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


class Sessions:
    def __init__(self, secret, ttl=SESSION_TTL):
        self.secret, self.ttl = secret, ttl

    def _sign(self, payload):
        mac = hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def issue(self, password_hash):
        payload = f"{int(time.time()) + self.ttl}.{_fingerprint(password_hash)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, cookie, password_hash):
        try:
            expiry, fp, sig = cookie.split(".")
            payload = f"{expiry}.{fp}"
            if not hmac.compare_digest(self._sign(payload), sig):
                return False
            return int(expiry) > time.time() and hmac.compare_digest(fp, _fingerprint(password_hash))
        except (AttributeError, ValueError):
            return False


def new_token():
    """(plaintext shown once, stored hash)."""
    plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_token(plain)


def hash_token(plain):
    return hashlib.sha256(plain.encode()).hexdigest()


def find_token(tokens, plain):
    """The token record whose hash matches, or None. Constant-time per record."""
    h = hash_token(plain)
    found = None
    for t in tokens:
        if hmac.compare_digest(t.get("hash", ""), h):
            found = t
    return found


class RateLimiter:
    """Blocks a client after `limit` failed logins within `window` seconds."""

    def __init__(self, limit=5, window=60):
        self.limit, self.window = limit, window
        self.failures = collections.defaultdict(collections.deque)

    def _prune(self, key, now):
        q = self.failures[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def blocked(self, key, now=None):
        return len(self._prune(key, time.time() if now is None else now)) >= self.limit

    def fail(self, key, now=None):
        now = time.time() if now is None else now
        self._prune(key, now).append(now)

    def reset(self, key):
        self.failures.pop(key, None)
