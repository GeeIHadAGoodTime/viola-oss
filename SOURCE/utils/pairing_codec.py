"""IP address encoding/decoding for Viola spoke pairing."""

from __future__ import annotations

from utils.pairing_words import PAIRING_WORDS


def encode_ip(ip: str) -> str:
    """Encode a LAN IP to a pairing word or base36 code.

    For 192.168.0.x and 192.168.1.x addresses, returns a friendly word
    from the 512-word pairing list. For all other IPs, returns a 6-char
    uppercase base36 code.

    Args:
        ip: IPv4 address string (e.g. "192.168.0.42").

    Returns:
        A pairing word or base36 code, or empty string on invalid input.
    """
    parts = ip.split(".")
    if len(parts) != 4:
        return ""
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return ""
    if any(o < 0 or o > 255 for o in octets):
        return ""

    if octets[0] == 192 and octets[1] == 168 and octets[2] <= 1:
        index = octets[2] * 256 + octets[3]
        if index < len(PAIRING_WORDS):
            return PAIRING_WORDS[index]

    # Base36 fallback for non-192.168.0-1.x addresses
    num = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    if num < 0:
        num += 2**32
    code = ""
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    while num > 0:
        code = chars[num % 36] + code
        num //= 36
    return (code or "0").upper().rjust(6, "0")


def decode_input(text: str) -> str | None:
    """Decode a pairing word or base36 code to an IP address.

    Args:
        text: A pairing word (e.g. "maple") or base36 code (e.g. "1A2B3C").

    Returns:
        IPv4 address string or None if input is invalid.
    """
    clean = (text or "").strip().lower()
    if not clean:
        return None

    # Word lookup
    try:
        index = PAIRING_WORDS.index(clean)
        o2 = index // 256
        o3 = index % 256
        return f"192.168.{o2}.{o3}"
    except ValueError:
        pass

    # Base36 decode
    try:
        num = int(clean, 36)
        if num < 0 or num > 0xFFFFFFFF:
            return None
        o1 = (num >> 24) & 0xFF
        o2 = (num >> 16) & 0xFF
        o3 = (num >> 8) & 0xFF
        o4 = num & 0xFF
        return f"{o1}.{o2}.{o3}.{o4}"
    except (ValueError, OverflowError):
        return None
