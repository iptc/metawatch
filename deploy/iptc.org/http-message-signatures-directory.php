<?php
/**
 * Web Bot Auth key directory for the Metawatch crawler.
 *
 * Served at https://iptc.org/.well-known/http-message-signatures-directory.
 * The crawler's requests carry `Signature-Agent: "https://iptc.org"`;
 * Cloudflare fetches this URL to get the public key that checks those
 * signatures. Cloudflare also requires this response to be signed by the
 * key it publishes, with a short-lived signature — so it can't be a static
 * file, and it reads the private key on every request.
 *
 * Format: https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/
 * Install: see README.md beside this file.
 *
 * Needs PHP >= 7.2 with the sodium extension (bundled and on by default in
 * Ubuntu's PHP packages).
 */

declare(strict_types=1);

// Outside the web root, readable by the web server user only.
const KEY_FILE_DEFAULT = '/etc/metawatch/wba-private.pem';
// Hosts this directory answers for. The signature covers the host the
// request came to, so we refuse to sign for anything else.
const ALLOWED_HOSTS = ['iptc.org', 'www.iptc.org'];
// Cloudflare's own example uses a ten-second window; a minute leaves room
// for clock skew without keeping a signed response useful for long.
const VALIDITY_SECONDS = 60;

function b64url(string $raw): string
{
    return rtrim(strtr(base64_encode($raw), '+/', '-_'), '=');
}

function fail(int $status): void
{
    http_response_code($status);
    header('Content-Type: text/plain');
    header('Cache-Control: no-store');
    echo $status === 404 ? "Not found\n" : "Unavailable\n";
    exit;
}

/**
 * Ed25519 keypair from a PKCS#8 PEM, as written by wba_keygen.py
 * (and `openssl genpkey -algorithm ed25519`). The DER is a fixed 48 bytes:
 * a 16-byte header naming Ed25519, then the 32-byte seed.
 */
function load_keypair(string $path): string
{
    $pem = @file_get_contents($path);
    if ($pem === false) {
        throw new RuntimeException("cannot read key file $path");
    }
    if (!preg_match('/-----BEGIN PRIVATE KEY-----(.+?)-----END PRIVATE KEY-----/s', $pem, $m)) {
        throw new RuntimeException('key file is not a PKCS#8 PEM private key');
    }
    $der = base64_decode(preg_replace('/\s+/', '', $m[1]), true);
    $header = hex2bin('302e020100300506032b657004220420');
    if ($der === false || strlen($der) !== 48 || substr($der, 0, 16) !== $header) {
        throw new RuntimeException('key file is not an Ed25519 private key');
    }
    return sodium_crypto_sign_seed_keypair(substr($der, 16));
}

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
if ($method !== 'GET' && $method !== 'HEAD') {
    header('Allow: GET, HEAD');
    fail(405);
}

// RFC 9421 @authority: lowercase host, default port dropped.
$host = strtolower($_SERVER['HTTP_HOST'] ?? '');
$host = preg_replace('/:(80|443)$/', '', $host);
if (!in_array($host, ALLOWED_HOSTS, true)) {
    fail(404);
}

try {
    $keyFile = getenv('METAWATCH_WBA_KEY_FILE') ?: KEY_FILE_DEFAULT;
    $keypair = load_keypair($keyFile);
} catch (Throwable $e) {
    error_log('metawatch wba directory: ' . $e->getMessage());
    fail(503);
}
$secret = sodium_crypto_sign_secretkey($keypair);
$public = sodium_crypto_sign_publickey($keypair);

$x = b64url($public);
// RFC 7638 thumbprint: required members in lexical order, no whitespace.
$keyid = b64url(hash('sha256', '{"crv":"Ed25519","kty":"OKP","x":"' . $x . '"}', true));
$body = json_encode(['keys' => [['kty' => 'OKP', 'crv' => 'Ed25519', 'x' => $x]]], JSON_UNESCAPED_SLASHES);

$created = time();
$expires = $created + VALIDITY_SECONDS;
$nonce = base64_encode(random_bytes(64));
// ;req — the component is taken from the request this response answers.
$params = '("@authority";req)'
    . ';alg="ed25519"'
    . ';keyid="' . $keyid . '"'
    . ';nonce="' . $nonce . '"'
    . ';tag="http-message-signatures-directory"'
    . ';created=' . $created
    . ';expires=' . $expires;
$base = '"@authority";req: ' . $host . "\n"
    . '"@signature-params": ' . $params;
$signature = base64_encode(sodium_crypto_sign_detached($base, $secret));
sodium_memzero($secret);

header('Content-Type: application/http-message-signatures-directory+json');
// Each response carries its own short-lived signature; a cached copy
// would fail verification within a minute.
header('Cache-Control: no-store');
header('Signature-Input: sig1=' . $params);
header('Signature: sig1=:' . $signature . ':');
header('Content-Length: ' . strlen($body));
if ($method === 'GET') {
    echo $body;
}
