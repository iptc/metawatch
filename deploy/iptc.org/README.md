# Web Bot Auth key directory on iptc.org

The Metawatch crawler signs its requests with `Signature-Agent: "https://iptc.org"`
(see `crawler/src/pmd_crawler/webbotauth.py`). Cloudflare verifies those
signatures by fetching our public key from

    https://iptc.org/.well-known/http-message-signatures-directory

`http-message-signatures-directory.php` serves it. Cloudflare requires that
response to be signed by the key it publishes, with a short-lived signature,
so it is a script rather than a static file, and it needs the private key.

## Requirements

- PHP 7.2+ with `sodium` (on by default in Ubuntu's PHP packages; check with
  `php -m | grep sodium`).
- Apache with `mod_alias` (standard).

Tested on `php:8.3-apache` behind WordPress's stock `.htaccess` rewrite rules,
both ways of wiring it up below (vhost `Alias`, or `.htaccess` only).

## Install

1. **Key.** Generate it on a trusted machine (once — the same key goes to the
   crawler):

   ```bash
   .venv/bin/python crawler/scripts/wba_keygen.py --out wba-private.pem
   gh secret set METAWATCH_WBA_PRIVATE_KEY < wba-private.pem
   ```

   Copy it to the server, readable by the web server user only:

   ```bash
   sudo install -d -m 0750 -o root -g www-data /etc/metawatch
   sudo install -m 0640 -o root -g www-data wba-private.pem /etc/metawatch/wba-private.pem
   ```

   Then delete any local copies you don't need. If PHP runs as a user other
   than `www-data` (PHP-FPM pools can), use that group instead.

2. **Script.** Put it outside the WordPress document root:

   ```bash
   sudo install -d -m 0755 /opt/metawatch-wba
   sudo install -m 0644 deploy/iptc.org/http-message-signatures-directory.php /opt/metawatch-wba/
   ```

3. **Apache.** In the iptc.org HTTPS `<VirtualHost>`:

   ```apache
   Alias /.well-known/http-message-signatures-directory /opt/metawatch-wba/http-message-signatures-directory.php
   <Directory /opt/metawatch-wba>
       Require all granted
   </Directory>
   ```

   The alias resolves to a real `.php` file, so WordPress's
   `RewriteCond %{REQUEST_FILENAME} !-f` lets it through and PHP handles it
   with whatever handler the vhost already uses (mod_php or PHP-FPM).

   If WordPress's rewrite rules live in the vhost itself rather than in
   `.htaccess`, add this exemption above them:

   ```apache
   RewriteRule ^/\.well-known/http-message-signatures-directory$ - [L]
   ```

   To keep the key somewhere other than `/etc/metawatch/wba-private.pem`, add
   `SetEnv METAWATCH_WBA_KEY_FILE /path/to/key.pem` in the same vhost.

   Then `sudo apachectl configtest && sudo systemctl reload apache2`.

   **Or, without touching the vhost: `.htaccess` only.** `Alias` isn't
   allowed in `.htaccess`, and a rewrite there can only reach files inside
   the document root, so the script moves into the WordPress directory
   instead of `/opt` (the key stays in `/etc/metawatch`):

   ```bash
   sudo install -d -m 0755 <docroot>/metawatch-wba
   sudo install -m 0644 deploy/iptc.org/http-message-signatures-directory.php <docroot>/metawatch-wba/
   ```

   and add this to the top-level `.htaccess`, **above** `# BEGIN WordPress`
   (WordPress rewrites everything between its own markers when permalinks
   are saved, but leaves the rest of the file alone):

   ```apache
   # BEGIN Metawatch Web Bot Auth
   RewriteEngine On
   RewriteRule ^\.well-known/http-message-signatures-directory$ metawatch-wba/http-message-signatures-directory.php [L]
   # END Metawatch Web Bot Auth
   ```

   No reload needed. The script is then also reachable at
   `/metawatch-wba/http-message-signatures-directory.php`, which is harmless:
   it serves the same public key. If PHP has `open_basedir` set, it must
   include `/etc/metawatch`, or set `METAWATCH_WBA_KEY_FILE` (via `SetEnv`
   in the same `.htaccess`) to a path PHP can read that is *not* web-served.

## Check

From the repo:

```bash
.venv/bin/python crawler/scripts/wba_check_directory.py --keyid <keyid printed by wba_keygen.py>
```

It fetches the directory and verifies it independently of this PHP: content
type, key, keyid, the signature over `@authority`, and the time window. Then
check the crawler and the directory together against Cloudflare's test
endpoint, which should answer 200:

```bash
METAWATCH_WBA_PRIVATE_KEY="$(cat wba-private.pem)" .venv/bin/python - <<'EOF'
import asyncio, httpx
from pmd_crawler import DEFAULT_HEADERS, webbotauth
async def main():
    s = webbotauth.load_signer()
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, event_hooks=webbotauth.event_hooks(s)) as c:
        r = await c.get("https://crawltest.com/cdn-cgi/web-bot-auth")
        print(r.status_code, r.text)
asyncio.run(main())
EOF
```

## Behaviour

| Request | Response |
|---|---|
| `GET`/`HEAD`, Host `iptc.org` or `www.iptc.org` | 200, JWKS, signed for that host |
| Any other Host | 404 (it won't sign for a host it doesn't serve) |
| Other methods | 405 |
| Key file missing or unreadable | 503, reason in Apache's error log |

Responses are `Cache-Control: no-store`: each signature expires after 60
seconds, so a cached copy would fail verification.

## Rotating the key

Generate a new key, then replace it in both places together: the
`METAWATCH_WBA_PRIVATE_KEY` GitHub secret and `/etc/metawatch/wba-private.pem`.
Then rerun both checks above. Cloudflare reads the key from this directory, so
the crawltest endpoint answering 200 confirms the new key is picked up.
