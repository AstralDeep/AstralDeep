"""Development-only OIDC issuer: serves an RS256 JWKS and mints short-lived access tokens for the node under test."""
import base64, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

KEYDIR = os.environ.get("KEYDIR", "/keys")
ISSUER = os.environ["ISSUER"]
KID = "dev-issuer-1"
PEM = os.path.join(KEYDIR, "issuer.pem")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def load():
    if not os.path.exists(PEM):
        os.makedirs(KEYDIR, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with open(PEM, "wb") as fh:
            fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with open(PEM, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), None)


def jwks(key):
    n = key.public_key().public_numbers()
    size = lambda v: v.to_bytes((v.bit_length() + 7) // 8, "big")
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": b64(size(n.n)), "e": b64(size(n.e))}]}


def mint(key, subject, client, ttl=900):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": subject, "azp": client, "aud": ["account", client], "iat": now, "exp": now + ttl,
              "preferred_username": "node-owner", "email": "owner@node.test",
              "realm_access": {"roles": ["user"]}}
    head = {"alg": "RS256", "typ": "JWT", "kid": KID}
    signing = b64(json.dumps(head).encode()) + "." + b64(json.dumps(claims).encode())
    sig = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing + "." + b64(sig)


if sys.argv[1] == "serve":
    key = load()
    body = json.dumps(jwks(key)).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/protocol/openid-connect/certs"):
                self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()

        def log_message(self, *args):
            sys.stderr.write("issuer %s\n" % (args[0] % args[1:] if args else ""))

    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
elif sys.argv[1] == "mint":
    print(mint(load(), sys.argv[2], sys.argv[3]))
