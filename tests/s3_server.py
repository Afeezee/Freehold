"""A tiny S3-compatible server for tests, over real HTTP.

It verifies the SigV4 Authorization header by recomputing the signature from
the request as actually received, so a driver that signs the wrong canonical
request, or lies about its payload hash, fails here rather than silently
working against a mock.
"""
from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from freehold.drivers.s3api import authorization

ACCESS_KEY = "FREEHOLDTESTKEY"
SECRET_KEY = "freehold-test-secret-key-0000000000000000"
REGION = "auto"


class Store:
    def __init__(self) -> None:
        self.objects: Dict[str, bytes] = {}
        self.fail_next = 0          # return 503 this many times, then recover
        self.offline = False        # return 403 (simulates revoked creds)
        self.auth_failures = 0
        self.bad_payload_hash = 0
        self.live = 0
        self.peak = 0
        self.delay = 0.0            # artificial latency, for timing tests
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    store: Store = None  # type: ignore
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # keep pytest output clean
        pass

    # -- helpers ----------------------------------------------------------
    def _enter(self) -> None:
        with self.store.lock:
            self.store.live += 1
            self.store.peak = max(self.store.peak, self.store.live)
        if self.store.delay:
            time.sleep(self.store.delay)

    def _exit(self) -> None:
        with self.store.lock:
            self.store.live -= 1

    def _send(self, code: int, body: bytes = b"",
              ctype: str = "application/xml") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _check_auth(self, method: str, body: bytes) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("AWS4-HMAC-SHA256 "):
            self.store.auth_failures += 1
            return False
        parts = dict(
            p.strip().split("=", 1)
            for p in header[len("AWS4-HMAC-SHA256 "):].split(",")
            if "=" in p)
        signed = parts.get("SignedHeaders", "").split(";")
        amz_date = self.headers.get("x-amz-date", "")
        claimed = self.headers.get("x-amz-content-sha256", "")
        actual = hashlib.sha256(body).hexdigest()
        if claimed != actual:
            self.store.bad_payload_hash += 1
            return False
        headers = {}
        for name in signed:
            value = self.headers.get(name)
            if value is None and name == "host":
                value = self.headers.get("Host", "")
            headers[name] = value or ""
        path, _, query = self.path.partition("?")
        # re-normalise the query the way a signer must: sorted, encoded
        if query:
            pairs = sorted(urllib.parse.parse_qsl(query, keep_blank_values=True))
            query = "&".join(
                urllib.parse.quote(k, safe="-_.~") + "="
                + urllib.parse.quote(v, safe="-_.~") for k, v in pairs)
        expected = authorization(method, path, query, headers, claimed,
                                 ACCESS_KEY, SECRET_KEY, REGION, amz_date)
        if expected.split("Signature=")[-1] != parts.get("Signature"):
            self.store.auth_failures += 1
            return False
        return True

    def _guard(self, method: str, body: bytes = b"") -> Optional[bool]:
        if self.store.offline:
            self._send(403, b"<Error><Code>AccessDenied</Code></Error>")
            return None
        if self.store.fail_next > 0:
            self.store.fail_next -= 1
            self._send(503, b"<Error><Code>SlowDown</Code></Error>")
            return None
        if not self._check_auth(method, body):
            self._send(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
            return None
        return True

    def _key(self) -> str:
        path = self.path.partition("?")[0].lstrip("/")
        _bucket, _, key = path.partition("/")
        return urllib.parse.unquote(key)

    # -- verbs ------------------------------------------------------------
    def do_PUT(self) -> None:
        self._enter()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)
            if self._guard("PUT", body) is None:
                return
            self.store.objects[self._key()] = body
            self._send(200)
        finally:
            self._exit()

    def do_GET(self) -> None:
        self._enter()
        try:
            if self._guard("GET") is None:
                return
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            if "list-type" in query:
                self._send(200, self._listing(query))
                return
            data = self.store.objects.get(self._key())
            if data is None:
                self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
                return
            self._send(200, data, "application/octet-stream")
        finally:
            self._exit()

    def do_HEAD(self) -> None:
        if self._guard("HEAD") is None:
            return
        self._send(200)

    def do_DELETE(self) -> None:
        if self._guard("DELETE") is None:
            return
        if self.store.objects.pop(self._key(), None) is None:
            self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
        else:
            self._send(204)

    def _listing(self, query) -> bytes:
        prefix = (query.get("prefix") or [""])[0]
        keys = sorted(k for k in self.store.objects if k.startswith(prefix))
        # one page at a time, so pagination is genuinely exercised
        page_size = 2
        token = (query.get("continuation-token") or [None])[0]
        start = int(token) if token else 0
        page = keys[start:start + page_size]
        nxt = start + page_size
        truncated = nxt < len(keys)
        rows = "".join(
            "<Contents><Key>" + k + "</Key><Size>"
            + str(len(self.store.objects[k])) + "</Size></Contents>"
            for k in page)
        more = ("<NextContinuationToken>" + str(nxt)
                + "</NextContinuationToken>") if truncated else ""
        return ("<?xml version='1.0'?><ListBucketResult "
                "xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
                + rows + "<IsTruncated>"
                + ("true" if truncated else "false")
                + "</IsTruncated>" + more
                + "</ListBucketResult>").encode()


class FakeS3:
    def __init__(self) -> None:
        self.store = Store()
        handler = type("BoundHandler", (Handler,), {"store": self.store})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://" + str(host) + ":" + str(port)

    def config(self, **extra) -> Dict[str, str]:
        cfg = {"endpoint": self.endpoint, "bucket": "freehold-test",
               "region": REGION, "access_key": ACCESS_KEY,
               "secret_key": SECRET_KEY}
        cfg.update({k: str(v) for k, v in extra.items()})
        return cfg

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
