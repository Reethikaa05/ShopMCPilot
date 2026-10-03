#!/usr/bin/env python3
"""WooCommerce -> Agent Studio Connector
All-in-one launcher for Mission Control and the Mock Store.
Run:
    python run.py
"""
import argparse, os, sys, threading, time, webbrowser

BANNER = r"""
 __      __                 _________                                   __                
/  \    /  \____   ____    /   _____/___________________  __ ____ _____/  |_  ___________ 
\   \/\/   /  _ \ /  _ \   \_____  \/ __ \_  __ \  \/ // // __ \\__  \\   __\/  _ \_  __ \
 \        (  <_> |  <_> )  /        \  ___/|  | \/\   / \   ___/ / __ \|  | (  <_> )  | \/
  \__/\  / \____/ \____/  /_______  /\___  >__|    \_/   \___  >(____  /__|  \____/|__|   
       \/                         \/     \/                  \/      \/                   
           >> WooCommerce -> Agent Studio MCP Mission Control <<
"""


def main():
    parser = argparse.ArgumentParser(description="WooCommerce Agent Studio Mission Control")
    parser.add_argument("--port", type=int, default=8090, help="Mission Control UI port (default: 8090)")
    parser.add_argument("--store-port", type=int, default=8089, help="Mock WooCommerce store port (default: 8089)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(BANNER)
    print(f"  [*] Mission Control Dashboard  ->  http://127.0.0.1:{args.port}")
    print(f"  [*] Mock WooCommerce Store     ->  http://127.0.0.1:{args.store_port}")
    print(f"  [+] Read-Only Guardrails       ->  Active (GET-only, PII masked, injection defenses)")
    print(f"  [+] Agent Studio Copilot       ->  Ready (0 API keys needed, 100% offline)\n")

    if not args.no_browser:
        def open_browser():
            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        threading.Thread(target=open_browser, daemon=True).start()

    from demo.app import main as run_demo
    try:
        run_demo(args.port, args.store_port)
    except KeyboardInterrupt:
        print("\nShutdown complete.")


if __name__ == "__main__":
    main()
