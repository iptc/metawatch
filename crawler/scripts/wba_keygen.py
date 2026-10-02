"""Generate a Web Bot Auth signing key and print what each side needs.

    .venv/bin/python crawler/scripts/wba_keygen.py --out wba-private.pem

Writes the Ed25519 private key (PEM, mode 0600) and prints:
  * the public JWK — what the iptc.org key directory serves;
  * the keyid (RFC 7638 thumbprint) — what our signatures name;
  * where the private key has to go: the METAWATCH_WBA_PRIVATE_KEY GitHub
    Actions secret (crawler) and the iptc.org server (directory signing).

Never commit the PEM. Rotating means replacing it in both places at once.
"""

import argparse
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pmd_crawler.webbotauth import DIRECTORY_PATH, KEY_ENV, jwk_thumbprint, public_jwk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True, help="Where to write the private key PEM.")
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; refusing to overwrite a key.")

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)

    jwk = public_jwk(key)
    print(f"Private key written to {args.out} (0600). Do not commit it.\n")
    print(f"keyid: {jwk_thumbprint(jwk)}\n")
    print(f"Key directory body for https://iptc.org{DIRECTORY_PATH}:")
    print(json.dumps({"keys": [jwk]}, indent=2))
    print("\nNext:")
    print(f"  gh secret set {KEY_ENV} < {args.out}")
    print("  copy the PEM to the iptc.org server for the directory script, then delete local copies you don't need.")


if __name__ == "__main__":
    main()
