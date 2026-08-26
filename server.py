#!/usr/bin/env python3
"""Serve the QR File Transfer single-page app on the local network.

Plain `python3 server.py` is enough: it serves index.html (in the same
directory as this script) over HTTPS using a self-signed certificate that it
generates and caches locally, and prints every URL other devices on the LAN
can use to reach it.

HTTPS matters here specifically because the "Receive" tab uses the camera
(getUserMedia), and browsers only allow camera access on a secure context
(HTTPS, or http://localhost on the machine itself). A phone opening the page
over plain http://<lan-ip> will have its camera request silently blocked.

Run with --http to force plain HTTP (fine for the "Send" tab, or for
receiving via http://localhost on this same machine).
"""

import argparse
import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CERT_DIR = SCRIPT_DIR / ".certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
CERT_DAYS = 825  # browsers reject self-signed certs valid longer than ~825 days


def local_ipv4_addresses():
    """Best-effort discovery of this machine's LAN IPv4 addresses."""
    addrs = set()

    # The "connect" trick: no packet is actually sent for UDP, this just
    # asks the OS which local interface/address it would use for that route.
    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect((probe, 80))
                addrs.add(s.getsockname()[0])
        except OSError:
            pass

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass

    addrs = {a for a in addrs if not a.startswith("127.")}
    return sorted(addrs)


def _openssl_env(openssl_path):
    """Some OpenSSL builds (notably the one bundled with Anaconda on
    Windows) are compiled with a hardcoded default config path that doesn't
    exist on this machine, and fail outright unless OPENSSL_CONF points
    them at their own config file. That file normally lives next to the
    binary at <prefix>/ssl/openssl.cnf (conda's layout on both Windows and
    Linux); point OPENSSL_CONF there if we can find it and the caller
    hasn't already set one."""
    env = os.environ.copy()
    if env.get("OPENSSL_CONF"):
        return env
    candidate = Path(openssl_path).resolve().parent.parent / "ssl" / "openssl.cnf"
    if candidate.exists():
        env["OPENSSL_CONF"] = str(candidate)
    return env


def ensure_self_signed_cert(extra_ips):
    """Create (or reuse) a self-signed cert/key pair covering localhost +
    the detected LAN IPs. Returns (cert_path, key_path) or None if openssl
    is unavailable / generation fails."""
    openssl = shutil.which("openssl")
    if not openssl:
        return None
    env = _openssl_env(openssl)

    if CERT_FILE.exists() and KEY_FILE.exists():
        # Reuse the existing cert as long as it's still valid for a while
        # and covers the same set of IPs (regenerating on every run would
        # make browsers re-show the "not private" warning every time).
        try:
            out = subprocess.run(
                [openssl, "x509", "-in", str(CERT_FILE), "-noout", "-checkend", "86400", "-text"],
                capture_output=True, text=True, check=False, env=env,
            )
            still_valid = out.returncode == 0
            covers_ips = all(ip in out.stdout for ip in extra_ips)
            if still_valid and covers_ips:
                return CERT_FILE, KEY_FILE
        except OSError:
            pass

    CERT_DIR.mkdir(exist_ok=True)
    san_entries = ["DNS:localhost", "IP:127.0.0.1"]
    for ip in extra_ips:
        try:
            ipaddress.ip_address(ip)
            san_entries.append(f"IP:{ip}")
        except ValueError:
            san_entries.append(f"DNS:{ip}")
    san = ",".join(san_entries)

    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
        "-days", str(CERT_DAYS),
        "-subj", "/CN=qr-file-transfer.local",
        "-addext", f"subjectAltName={san}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0 or not CERT_FILE.exists():
        print("Warning: failed to generate a self-signed certificate:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return None
    return CERT_FILE, KEY_FILE


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self):
        # Avoid caching the app between edits/restarts during local use.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=None, help="port to listen on (default: 8443 for https, 8000 for http)")
    parser.add_argument("--http", action="store_true", help="force plain HTTP instead of HTTPS (camera scanning will likely be blocked on other devices)")
    parser.add_argument("--dir", default=str(SCRIPT_DIR), help="directory to serve (default: this script's directory)")
    args = parser.parse_args()

    serve_dir = Path(args.dir).resolve()
    if not (serve_dir / "index.html").exists():
        print(f"Warning: no index.html found in {serve_dir}", file=sys.stderr)

    ips = local_ipv4_addresses()
    use_https = not args.http
    cert_pair = ensure_self_signed_cert(ips) if use_https else None
    if use_https and not cert_pair:
        print("Could not set up HTTPS (openssl not found or cert generation failed).", file=sys.stderr)
        print("Falling back to plain HTTP — camera access on other devices may be blocked by the browser.\n", file=sys.stderr)
        use_https = False

    port = args.port if args.port is not None else (8443 if use_https else 8000)

    def handler_factory(*a, **kw):
        return Handler(*a, directory=str(serve_dir), **kw)

    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler_factory)

    if use_https:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_pair[0]), keyfile=str(cert_pair[1]))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    scheme = "https" if use_https else "http"
    print("QR File Transfer — serving", serve_dir)
    print()
    print(f"  {scheme}://localhost:{port}/")
    for ip in ips:
        print(f"  {scheme}://{ip}:{port}/")
    print()
    if use_https:
        print("Self-signed certificate: the browser will show a 'connection is not")
        print("private' warning on first visit from each device — this is expected")
        print("for a local, offline server. Proceed / accept the certificate once.")
    else:
        print("Running over plain HTTP: the camera (Receive tab) will likely be")
        print("blocked by the browser on any device except this one (localhost).")
        print("Re-run without --http to enable HTTPS for camera access on phones.")
    print("\nPress Ctrl+C to stop.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
