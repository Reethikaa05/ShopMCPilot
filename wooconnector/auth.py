"""Authentication for WooCommerce.

Two supported paths (both produce the same consumer_key / consumer_secret pair):

1. API-key  : merchant creates a *Read-only* key in WooCommerce > Settings > Advanced > REST API
              and supplies it via env vars or `python -m wooconnector login --key ... --secret ...`.
2. wc-auth  : WooCommerce's built-in OAuth-style consent flow (/wc-auth/v1/authorize). The merchant
              clicks "Approve", WooCommerce POSTs freshly minted *read-scoped* keys to our local
              callback. We use a CSRF `state` (passed as user_id) and bind to 127.0.0.1 only.

Secrets are stored in a 0600 file (or env vars) and are never logged or returned to the agent.
"""
from __future__ import annotations
import json, os, pathlib, secrets, threading, time, urllib.parse, urllib.request
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer

CRED_PATH = pathlib.Path(os.environ.get("WOO_CRED_FILE", pathlib.Path.home() / ".woo-connector" / "credentials.json"))
APP_NAME = "Razorpay Agent Studio Connector"


class AuthError(Exception):
    pass


@dataclass
class Credentials:
    base_url: str
    consumer_key: str
    consumer_secret: str
    scope: str = "read"

    def redacted(self) -> dict:
        k = self.consumer_key
        return {"base_url": self.base_url, "consumer_key": k[:6] + "…" + k[-3:] if len(k) > 9 else "***",
                "scope": self.scope}


def from_env() -> Credentials | None:
    u, k, s = (os.environ.get(x) for x in ("WOO_BASE_URL", "WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET"))
    return Credentials(u.rstrip("/"), k, s) if u and k and s else None


def save(creds: Credentials, path: pathlib.Path | None = None) -> pathlib.Path:
    path = pathlib.Path(path or CRED_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(creds), f)
    os.chmod(path, 0o600)
    return path


def load(path: pathlib.Path | None = None) -> Credentials:
    env = from_env()
    if env:
        return env
    path = pathlib.Path(path or CRED_PATH)
    if not path.exists():
        raise AuthError("No credentials. Set WOO_BASE_URL/WOO_CONSUMER_KEY/WOO_CONSUMER_SECRET or run `python -m wooconnector login`.")
    return Credentials(**json.loads(path.read_text()))


def build_authorize_url(base_url: str, callback_url: str, return_url: str, state: str, scope: str = "read") -> str:
    q = urllib.parse.urlencode({"app_name": APP_NAME, "scope": scope, "user_id": state,
                                "return_url": return_url, "callback_url": callback_url})
    return f"{base_url.rstrip('/')}/wc-auth/v1/authorize?{q}"


def run_authorize_flow(base_url: str, port: int = 8765, auto_open: bool = False,
                       timeout: float = 120, public_callback: str | None = None) -> Credentials:
    """Run the consent flow. `public_callback` must be HTTPS reachable by the store for real shops
    (e.g. an ngrok URL forwarding to this port); localhost works for the bundled mock store."""
    state = secrets.token_urlsafe(16)
    box: dict = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            ctype = self.headers.get("Content-Type", "")
            data = json.loads(raw) if "json" in ctype else {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            if not secrets.compare_digest(str(data.get("user_id", "")), state):
                self.send_response(403); self.end_headers(); box["error"] = "state mismatch (possible CSRF)"; done.set(); return
            box.update(data)
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); done.set()

        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<h2>Connected to Agent Studio. You can close this tab.</h2>")

    srv = HTTPServer(("127.0.0.1", port), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cb = public_callback or f"http://127.0.0.1:{port}/callback"
    url = build_authorize_url(base_url, cb, f"http://127.0.0.1:{port}/done", state)
    try:
        if auto_open:  # non-interactive (demo/CI): act as the merchant clicking "Approve"
            def _approve():
                try:
                    urllib.request.urlopen(url, timeout=10).read()
                except Exception:
                    pass  # return_url hit after our callback server closed: harmless
            threading.Thread(target=_approve, daemon=True).start()
        else:
            print(f"Open this URL as the store admin and click Approve:\n\n  {url}\n")
            try:
                import webbrowser; webbrowser.open(url)
            except Exception:
                pass
        if not done.wait(timeout):
            raise AuthError("Timed out waiting for merchant approval")
        time.sleep(0.3)  # let the browser finish loading return_url before we close the callback server
    finally:
        srv.shutdown()
    if "error" in box:
        raise AuthError(box["error"])
    if not box.get("consumer_key"):
        raise AuthError("Callback did not include keys")
    perms = box.get("key_permissions", "read")
    if perms != "read":
        raise AuthError(f"Refusing key with permissions '{perms}': this connector is read-only by design")
    return Credentials(base_url.rstrip("/"), box["consumer_key"], box["consumer_secret"], perms)
