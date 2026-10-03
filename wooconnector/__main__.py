"""CLI:  python -m wooconnector <login|serve|spec|call|whoami> ..."""
from __future__ import annotations
import argparse, asyncio, json, os, sys
from . import auth
from .client import WooClient
from .tools import build_tools, call_tool


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wooconnector", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="store credentials (API key or wc-auth consent flow)")
    lg.add_argument("--url", required=True)
    lg.add_argument("--key"); lg.add_argument("--secret")
    lg.add_argument("--oauth", action="store_true", help="use WooCommerce consent flow instead of pasting a key")
    lg.add_argument("--auto-approve", action="store_true", help="(mock store only) simulate the merchant click")
    lg.add_argument("--callback", help="public https callback URL (e.g. ngrok) for real stores")
    lg.add_argument("--port", type=int, default=8765)
    sub.add_parser("serve", help="run MCP server over stdio")
    sub.add_parser("spec", help="print MCP tool specs as JSON")
    sub.add_parser("whoami", help="show redacted credentials")
    c = sub.add_parser("call", help="call a tool directly"); c.add_argument("tool"); c.add_argument("args", nargs="?", default="{}")
    a = ap.parse_args(argv)

    if a.cmd == "login":
        if a.oauth:
            creds = auth.run_authorize_flow(a.url, a.port, auto_open=a.auto_approve, public_callback=a.callback)
        elif a.key and a.secret:
            creds = auth.Credentials(a.url.rstrip("/"), a.key, a.secret)
        else:
            sys.exit("Provide --key/--secret or --oauth")
        client = WooClient(creds, allow_insecure=os.environ.get("WOO_ALLOW_INSECURE") == "1")
        build = build_tools(client)["woo_connection_status"].handler({})  # validates creds before saving
        print("Saved to", auth.save(creds), "->", json.dumps(creds.redacted()))
        return 0
    if a.cmd == "serve":
        from .mcp_server import serve
        asyncio.run(serve()); return 0
    client = None
    if a.cmd == "spec":
        from .client import WooClient as _W
        dummy = auth.Credentials("http://127.0.0.1:1", "x", "y")
        print(json.dumps([t.spec() for t in build_tools(_W(dummy)).values()], indent=2)); return 0
    creds = auth.load()
    if a.cmd == "whoami":
        print(json.dumps(creds.redacted(), indent=2)); return 0
    client = WooClient(creds, allow_insecure=os.environ.get("WOO_ALLOW_INSECURE") == "1")
    print(json.dumps(call_tool(build_tools(client), client, a.tool, json.loads(a.args)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
