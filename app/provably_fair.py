"""
Road Worx Provably Fair Verification Module

The Road Worx game by Spribe uses a provably fair system:
  1. Server generates a server_seed (committed via SHA-256 hash before the round)
  2. First 3 players' browsers provide client_seeds
  3. Combined string:  server_seed:client_seed_1:client_seed_2:client_seed_3:nonce
  4. SHA-512 hash is computed from the combined string
  5. Hash is converted to the crash multiplier

This module independently verifies that a recorded crash point
matches the cryptographic inputs.
"""

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def compute_crash_point(server_seed: str, combined_client_seed: str, nonce: int | None = None) -> float:
    """
    Reproduce the Road Worx crash point from seeds.

    Args:
        server_seed: The server-generated seed (revealed after round ends)
        combined_client_seed: The combined client seed string
            (could be a single concatenated string or individual seeds joined with ':')
        nonce: Optional round nonce

    Returns:
        The crash multiplier (>= 1.00)
    """
    if nonce is not None:
        seed_str = f"{server_seed}:{combined_client_seed}:{nonce}"
    else:
        seed_str = f"{server_seed}:{combined_client_seed}"

    hash_hex = hashlib.sha512(seed_str.encode('utf-8')).hexdigest()

    # Take first 13 hex characters and convert to integer
    h = int(hash_hex[:13], 16)
    e = 2 ** 52  # 4503599627370496

    # House edge: ~3% of rounds instant-crash at 1.00x
    # If h % 33 == 0, it's a house-edge round
    if h % 33 == 0:
        return 1.00

    # Calculate crash point
    result = (100 * e - h) / (e - h)
    crash_point = max(1.00, result / 100)
    return round(crash_point, 2)


def compute_sha512(server_seed: str, client_seeds: list[str], nonce: int | None = None) -> str:
    """
    Compute the SHA-512 hash from the seed components.

    Args:
        server_seed: Server seed string
        client_seeds: List of client seed strings (up to 3)
        nonce: Optional nonce

    Returns:
        The full SHA-512 hex digest
    """
    combined = ":".join([server_seed] + client_seeds)
    if nonce is not None:
        combined += f":{nonce}"
    return hashlib.sha512(combined.encode('utf-8')).hexdigest()


def verify_round(
    recorded_multiplier: float,
    server_seed: str,
    client_seeds: list[str],
    nonce: int | None = None,
    tolerance: float = 0.02,
) -> dict:
    """
    Verify a round's recorded multiplier against the cryptographic inputs.

    Args:
        recorded_multiplier: The multiplier that was recorded
        server_seed: Server seed (revealed after round)
        client_seeds: List of client seeds
        nonce: Optional nonce
        tolerance: Acceptable difference between recorded and computed

    Returns:
        dict with verification results
    """
    combined_client = ":".join(client_seeds)
    computed = compute_crash_point(server_seed, combined_client, nonce)
    sha512_hash = compute_sha512(server_seed, client_seeds, nonce)
    diff = abs(recorded_multiplier - computed)
    verified = diff <= tolerance

    result = {
        "verified": verified,
        "recorded_multiplier": recorded_multiplier,
        "computed_multiplier": computed,
        "difference": round(diff, 4),
        "sha512_hash": sha512_hash,
        "inputs": {
            "server_seed": server_seed,
            "client_seeds": client_seeds,
            "nonce": nonce,
        },
    }

    if verified:
        logger.info(f"Round verified: {recorded_multiplier}x == {computed}x (diff={diff:.4f})")
    else:
        logger.warning(
            f"Round verification FAILED: recorded={recorded_multiplier}x, "
            f"computed={computed}x, diff={diff:.4f}"
        )

    return result
