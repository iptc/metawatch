"""Metawatch crawler."""

__version__ = "2.0.0a1"
USER_AGENT = "Metawatch/2.0"
# RFC 9110 §10.1.2 — contact for site admins who see our hits in their logs.
# Kept out of the User-Agent string because some WAFs (e.g. CBC) reject any UA
# with a parenthesised comment block, even a polite one.
CONTACT_EMAIL = "metadata-crawler@iptc.org"
CONTACT_URL = "https://metawatch.iptc.org"

# Two WAF lessons folded into one minimal header set:
#   * CBC blocks User-Agent strings with parenthesised RFC-9110 comments,
#     so the contact info can't go inside `User-Agent: Metawatch/2.0
#     (+https://metawatch.iptc.org)` — it would 403.
#   * TASS blocks any request carrying an unfamiliar `X-*` header whose
#     value looks like a URL (presumably scored as a bot/spam signal).
#     We previously sent `X-Contact: https://metawatch.iptc.org` and got
#     403'd. Removed.
# What survives is RFC-standard only: User-Agent for identity, From for
# admin contact. Admins receiving a hit from "metadata-crawler@iptc.org"
# can find the project by searching the email.
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "From": CONTACT_EMAIL,
}
