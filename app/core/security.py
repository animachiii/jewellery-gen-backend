"""API key hashing and verification.

`settings.api_keys` maps sha256(plaintext_key) -> api_key_name (see app/config.py).
Verification hashes the caller-supplied plaintext once and does a single dict lookup
against those hashes.

Timing-safety note: the thing an attacker could try to learn via a timing side
channel is the *plaintext* key. Because we never compare plaintext strings directly
(we hash first), a dict lookup on the resulting digest leaks no information about the
plaintext beyond what SHA-256 itself already reveals (i.e. nothing feasible to
exploit) — an attacker cannot use response-time differences to incrementally guess
the digest, and cannot use the digest to recover the plaintext. Short-circuiting
string comparisons only matter when comparing secrets against each other in plaintext
(e.g. two raw tokens); a hash-then-lookup approach sidesteps that class of attack
entirely, so `hmac.compare_digest` is unnecessary here.
"""

import hashlib


def hash_key(plaintext: str) -> str:
    """Return the sha256 hexdigest of a plaintext API key."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def verify_key(plaintext: str, valid_keys: dict[str, str]) -> str | None:
    """Look up the api_key_name for a plaintext key against a hash->name map.

    Returns None if the key is unknown.
    """
    return valid_keys.get(hash_key(plaintext))
