"""Raw HTTP client for the edge runtime fixture; runs inside a container on one source network.

    python client.py cases.json [proxy-address]   -> prints one JSON result per case, in order

A case is {"id", "port", "tls", "sni", "host", "target", "headers": [[k, v], ...]}. The request
line carries `target` byte for byte (no normalisation, like curl --path-as-is) and header lines are
sent exactly as listed, duplicates included, so the proxy sees what an attacker would send. Every
failure, including a refused connection, is reported as that case's result and never aborts the run,
so the harness can retry a proxy that is still starting.
"""
import json
import socket
import ssl
import sys


def run(case, proxy):
    raw = None
    try:
        raw = socket.create_connection((proxy, case["port"]), timeout=10)
        if case.get("tls"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=case.get("sni") or None)
        lines = [f"GET {case['target']} HTTP/1.1", f"Host: {case['host']}", "Connection: close"]
        lines += [f"{k}: {v}" for k, v in case.get("headers", [])]
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        data = b""
        while True:
            chunk = raw.recv(65536)
            if not chunk:
                break
            data += chunk
    except (ssl.SSLError, OSError) as e:
        return {"id": case["id"], "error": f"{type(e).__name__}: {e}"}
    finally:
        if raw is not None:
            raw.close()
    head, _, body = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode(errors="replace").split("\r\n")
    headers = {}
    for h in header_lines:
        k, _, v = h.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    if headers.get("transfer-encoding") == ["chunked"]:
        out, rest = b"", body
        while rest:
            size, _, rest = rest.partition(b"\r\n")
            n = int(size.split(b";")[0] or b"0", 16)
            if n == 0:
                break
            out, rest = out + rest[:n], rest[n + 2:]
        body = out
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError):
        status = 0
    return {"id": case["id"], "status": status, "headers": headers,
            "body": body.decode(errors="replace")}


if __name__ == "__main__":
    cases = json.load(open(sys.argv[1]))
    proxy = sys.argv[2] if len(sys.argv) > 2 else "edgeproxy"
    for c in cases:
        print(json.dumps(run(c, proxy)), flush=True)
