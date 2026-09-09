"""High-cost / revenue-share destination prefixes for toll-fraud defense.

SEC-060: an agent-initiated call to a premium-rate, audiotext, or satellite
number is the classic vehicle for International Revenue Share Fraud (IRSF) — the
fraudster owns the high-cost number and pockets the per-minute revenue. The old
defense was a 6-entry premium-rate list (US 900/976, a handful of national
premium ranges) with **zero satellite coverage**, so the most common IRSF
targets (satellite networks and the wider national premium ranges) passed.

This module replaces the hand-picked list with a structured, sourced prefix
dataset organized by category. Matching is longest-prefix over E.164 digits, so
adding a country or range is a data edit, not new code.

Sources:
- ITU-T E.164 assigned country/network codes (satellite & global networks block
  +870..+888): https://www.itu.int/rec/T-REC-E.164
- ITU Operational Bulletin satellite network assignments (Inmarsat +870,
  Global Mobile Satellite +881, Universal Personal Telecom +878,
  International Networks +882/+883, Iridium +881 6/7, Thuraya +882 16,
  Globalstar +881 8/9, Ellipso/ICO ranges).
- National regulators' premium-rate (PRS) / audiotext number ranges (Ofcom UK
  09/070/0871-3; ARCEP FR 089x; ComReg IE 15xx; ACMA AU 190x; BNetzA DE 0900;
  US NANPA 900/976 audiotext).

The list is intentionally conservative for agent-initiated outbound calls:
Viola never has a legitimate reason to dial a premium-rate or satellite number
on a user's behalf, and the confirmation/cost-cap gates remain the primary
backstop — this is defense-in-depth that makes the common IRSF destinations
fail closed by default.
"""

from __future__ import annotations

# Each entry: E.164 prefix (with leading +) → human-readable category/reason.
# Longest-matching prefix wins (see match_toll_fraud_prefix).
TOLL_FRAUD_PREFIXES: dict[str, str] = {
    # ----- International satellite & global-network ranges (top IRSF targets) --
    "+870": "satellite_inmarsat",
    "+871": "satellite_inmarsat",  # retired Inmarsat ocean-region codes
    "+872": "satellite_inmarsat",
    "+873": "satellite_inmarsat",
    "+874": "satellite_inmarsat",
    "+875": "satellite_maritime_mobile",
    "+876": "satellite_maritime_mobile",
    "+877": "satellite_maritime_mobile",
    "+878": "universal_personal_telecom",  # +878 10 UPT
    "+881": "satellite_global_mobile",  # Iridium/Globalstar/ICO (+881 x)
    "+882": "international_networks",  # +882 incl. Thuraya 16, MCP, iNum
    "+883": "international_networks",  # +883 incl. iNum, MNP, Telna
    "+888": "telecom_disaster_relief",  # OCHA TDRS — not a normal outbound dest
    "+979": "international_premium_rate",  # ITU international premium-rate service
    # ----- United States / NANP audiotext & premium --------------------------
    "+1900": "us_premium_audiotext",
    "+1976": "us_premium_audiotext",
    # ----- United Kingdom (Ofcom) --------------------------------------------
    "+449": "uk_premium_rate",  # 09x premium (broad national 9-prefix)
    "+4470": "uk_personal_numbering",  # 070 — high-cost "follow-me" / spoof vector
    "+44871": "uk_revenue_share",  # 0871/0872/0873
    "+44872": "uk_revenue_share",
    "+44873": "uk_revenue_share",
    # ----- France (ARCEP) -----------------------------------------------------
    "+33089": "fr_premium_rate",  # 089x
    "+33081": "fr_special_rate",  # some 081x value-added ranges
    # ----- Germany (BNetzA) ---------------------------------------------------
    "+49900": "de_premium_rate",  # 0900
    "+49137": "de_mass_traffic",  # 0137 televoting/mass-traffic
    # ----- Ireland (ComReg) ---------------------------------------------------
    "+35315": "ie_premium_rate",  # 15xx premium service
    "+3535": "ie_premium_rate",  # legacy 15xx-style ranges
    # ----- Australia (ACMA) ---------------------------------------------------
    "+61190": "au_premium_rate",  # 190x
    # ----- Spain --------------------------------------------------------------
    "+34803": "es_premium_rate",
    "+34806": "es_premium_rate",
    "+34807": "es_premium_rate",
    "+34905": "es_premium_rate",
    # ----- Italy --------------------------------------------------------------
    "+39144": "it_premium_rate",
    "+39166": "it_premium_rate",
    "+39899": "it_premium_rate",
    "+39709": "it_premium_rate",
    # ----- Netherlands --------------------------------------------------------
    "+31900": "nl_premium_rate",
    "+31906": "nl_premium_rate",
    "+31909": "nl_premium_rate",
    # ----- High-cost small-territory / known IRSF transit destinations --------
    # These national codes are repeatedly abused as IRSF terminating ranges
    # because their settlement rates are very high and they are almost never a
    # legitimate consumer-assistant call destination.
    "+239": "high_cost_sao_tome",
    "+247": "high_cost_ascension",
    "+248": "high_cost_seychelles",
    "+252": "high_cost_somalia",
    "+267": "high_cost_botswana_share",
    "+371": "high_cost_latvia_share",
    "+375": "high_cost_belarus_share",
    "+509": "high_cost_haiti_share",
    "+675": "high_cost_png",
    "+676": "high_cost_tonga",
    "+677": "high_cost_solomon",
    "+678": "high_cost_vanuatu",
    "+679": "high_cost_fiji_share",
    "+685": "high_cost_samoa",
    "+688": "high_cost_tuvalu",
    "+690": "high_cost_tokelau",
    "+767": "high_cost_dominica_share",
}

# Reasons that are satellite/IRSF-class (used for messaging / metrics).
_PREFIXES_SORTED: tuple[str, ...] = tuple(sorted(TOLL_FRAUD_PREFIXES, key=len, reverse=True))


def normalize_e164_digits(number: str) -> str:
    """Return a '+<digits>' E.164-ish string for prefix matching.

    Strips spaces, dashes, parens, and dots. Preserves a leading '+'. Numbers
    without a leading '+' are matched as-is (callers that pass national-format
    numbers still get emergency/short-code handling elsewhere).
    """
    cleaned: list[str] = []
    for ch in str(number or ""):
        if ch.isdigit() or (ch == "+" and not cleaned):
            cleaned.append(ch)
    return "".join(cleaned)


def match_toll_fraud_prefix(number: str) -> str | None:
    """Return the toll-fraud category for a destination number, or None.

    Longest-prefix match over the structured dataset. Only matches when the
    number is in international (+) form, because a bare national number can't be
    safely attributed to a country's premium range without the country code.
    """
    normalized = normalize_e164_digits(number)
    if not normalized.startswith("+"):
        return None
    for prefix in _PREFIXES_SORTED:
        if normalized.startswith(prefix):
            return TOLL_FRAUD_PREFIXES[prefix]
    return None


def is_toll_fraud_number(number: str) -> bool:
    """True when a number falls in any premium-rate/satellite/IRSF range."""
    return match_toll_fraud_prefix(number) is not None
