"""Metawatch crawler."""

__version__ = "2.0.0a1"
USER_AGENT = "Metawatch/2.0"
# RFC 9110 §10.1.2 — contact for site admins who see our hits in their logs.
# Kept out of the User-Agent string because some WAFs (e.g. CBC) reject any UA
# with a parenthesised comment block, even a polite one.
CONTACT_EMAIL = "metadata-crawler@iptc.org"
CONTACT_URL = "https://metawatch.iptc.org"

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "From": CONTACT_EMAIL,
    "X-Contact": CONTACT_URL,
}
