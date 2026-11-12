"""Minimal S3 client with SigV4 signing, standard library only.

Works against any S3-compatible endpoint (Cloudflare R2, Backblaze B2, MinIO,
AWS). No boto3: the only third-party dependency Freehold has is `cryptography`,
and signing is a few lines of HMAC.

Path-style addressing throughout (`{endpoint}/{bucket}/{key}`), which R2, B2
and MinIO all accept.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Iterator, Optional, Tuple

from ..core.errors import BackendUnavailable, ChunkNotFound, ConfigError

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
USER_AGENT = "freehold/0.1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
RETRY_STATUS = (500, 502, 503, 504)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), date)
    k = _sign(k, region)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


def canonical_request(method: str, path: str, query: str,
                      headers: Dict[str, str], payload_hash: str) -> str:
    lower = {k.lower(): str(v).strip() for k, v in headers.items()}
    names = sorted(lower)
    canon_headers = "".join(n + ":" + lower[n] + "\n" for n in names)
    return "\n".join([method, path, query, canon_headers,
                      ";".join(names), payload_hash])


def authorization(method: str, path: str, query: str,
                  headers: Dict[str, str], payload_hash: str, access_key: str,
                  secret_key: str, region: str, amz_date: str) -> str:
    date = amz_date[:8]
    scope = "/".join([date, region, SERVICE, "aws4_request"])
    creq = canonical_request(method, path, query, headers, payload_hash)
    to_sign = "\n".join([ALGORITHM, amz_date, scope,
                         hashlib.sha256(creq.encode()).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region),
                         to_sign.encode(), hashlib.sha256).hexdigest()
    signed = ";".join(sorted(k.lower() for k in headers))
    return (ALGORITHM + " Credential=" + access_key + "/" + scope
            + ", SignedHeaders=" + signed + ", Signature=" + signature)


class S3API:
    def __init__(self, endpoint: str, region: str, access_key: str,
                 secret_key: str, timeout: int = 60, max_retries: int = 4):
        if not endpoint:
            raise ConfigError("s3 driver needs an endpoint")
        if not access_key or not secret_key:
            raise ConfigError("s3 driver needs credentials")
        if "://" not in endpoint:
            endpoint = "https://" + endpoint
        parsed = urllib.parse.urlparse(endpoint.rstrip("/"))
        self.scheme = parsed.scheme
        self.host = parsed.netloc
        self.base_path = parsed.path.rstrip("/")
        self.region = region or "auto"
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.max_retries = max_retries

    # -- transport --------------------------------------------------------
    def _request(self, method: str, bucket: str, key: str = "",
                 data: Optional[bytes] = None,
                 query: Optional[Dict[str, str]] = None) -> Tuple[int, bytes]:
        path = self.base_path + "/" + bucket
        if key:
            path += "/" + urllib.parse.quote(key, safe="/~")
        qs = ""
        if query:
            qs = "&".join(
                urllib.parse.quote(k, safe="-_.~") + "="
                + urllib.parse.quote(str(v), safe="-_.~")
                for k, v in sorted(query.items()))
        payload = data if data is not None else b""
        payload_hash = (EMPTY_SHA256 if not payload
                        else hashlib.sha256(payload).hexdigest())
        attempt = 0
        while True:
            amz_date = dt.datetime.now(dt.timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ")
            headers = {"Host": self.host, "x-amz-date": amz_date,
                       "x-amz-content-sha256": payload_hash}
            if data is not None:
                headers["Content-Length"] = str(len(payload))
            headers["Authorization"] = authorization(
                method, path, qs, headers, payload_hash, self.access_key,
                self.secret_key, self.region, amz_date)
            headers["User-Agent"] = USER_AGENT
            url = self.scheme + "://" + self.host + path + ("?" + qs if qs
                                                            else "")
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code == 429:
                    raise BackendUnavailable(
                        "s3 endpoint is rate limiting") from None
                if exc.code in RETRY_STATUS and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(20, 2 ** attempt) + random.random())
                    continue
                return exc.code, body
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(20, 2 ** attempt) + random.random())
                    continue
                raise BackendUnavailable("s3 endpoint unreachable: "
                                         + str(exc.reason)) from None

    # -- operations -------------------------------------------------------
    def put(self, bucket: str, key: str, data: bytes) -> None:
        status, body = self._request("PUT", bucket, key, data=data)
        if status >= 400:
            raise _error(status, body)

    def get(self, bucket: str, key: str) -> bytes:
        status, body = self._request("GET", bucket, key)
        if status == 404:
            raise ChunkNotFound(bucket + "/" + key)
        if status >= 400:
            raise _error(status, body)
        return body

    def delete(self, bucket: str, key: str) -> bool:
        status, body = self._request("DELETE", bucket, key)
        if status == 404:
            return False
        if status >= 400:
            raise _error(status, body)
        return True

    def head_bucket(self, bucket: str) -> None:
        status, body = self._request("HEAD", bucket)
        if status >= 400:
            raise _error(status, body)

    def list(self, bucket: str, prefix: str = "") -> Iterator[Tuple[str, int]]:
        token = None
        while True:
            query = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            status, body = self._request("GET", bucket, query=query)
            if status >= 400:
                raise _error(status, body)
            root = ET.fromstring(body)
            for item in root:
                if _tag(item) != "Contents":
                    continue
                key = size = None
                for child in item:
                    if _tag(child) == "Key":
                        key = child.text
                    elif _tag(child) == "Size":
                        size = int(child.text or 0)
                if key:
                    yield key, (size or 0)
            token = None
            truncated = False
            for child in root:
                if _tag(child) == "IsTruncated":
                    truncated = (child.text or "").strip().lower() == "true"
                elif _tag(child) == "NextContinuationToken":
                    token = child.text
            if not truncated or not token:
                return


def _tag(el) -> str:
    return el.tag.split("}", 1)[-1]


def _error(status: int, body: bytes) -> Exception:
    text = body[:300].decode("utf-8", "replace")
    msg = "s3 " + str(status) + ": " + text
    if status in (401, 403):
        return BackendUnavailable(msg)
    if status == 404:
        return ChunkNotFound(msg)
    return BackendUnavailable(msg)
