#!/bin/bash
# Export the selected FID. Network deletion requires a separate --broadcast run.
# Set FID, NEYNAR_KEY_FILE, and optionally OUT and PYTHON_BIN.
# Optional: SNAPCHAIN_NODE, SECONDARY_NODE, OP_RPC_URL, NEYNAR_KEY_FILE,
# THREAD_MAX_NODES,
# MEDIA_MAX_BYTES, FC_BIN (pinned official CLI with a local build receipt).
set -Eeuo pipefail
umask 077
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  printf '%s\n' 'Usage: farcaster_cleanup_all.sh --dry-run | --broadcast | --verify-backup' \
    'Set FID to a positive account ID. OUT defaults to ./farcaster-archive/fid<FID>.' \
    'Use the same FID and OUT for backup and broadcast. Review issues.json first.' \
    'Read mode needs grpcio, protobuf, blake3, cryptography, and httpx in Python 3.12 or newer.' \
    'Broadcast requires a verified backup and explicit acceptance of recorded gaps,' \
    'local eth-account and cryptography packages, and exact confirmation on /dev/tty.' \
    'Recovery phrases use hidden local input only. No X follows run from this script.'
  exit 0
fi
exec "$PYTHON_BIN" - "$@" <<'FARCASTER_PYTHON'
"""Local Farcaster archive and reviewed cleanup. Embedded in the Bash deliverable."""

import base64
import collections
import concurrent.futures
import csv
import datetime as dt
import fcntl
import email.utils
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

FID = int(os.environ.get("FID", "0")) if os.environ.get("FID", "0").isdecimal() else 0
REGISTRY = "0x00000000Fc6c5F01Fc30151999387Bb99A9f489b"
EPOCH = 1609459200
CLI_REV = "fc87f2686472598143062a00e1c2b2633b2d87ab"
HASH = re.compile(r"0x[0-9a-f]{40}")


def fail(message):
    raise RuntimeError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial-" + str(threading.get_ident()))
    data = (
        obj
        if isinstance(obj, bytes)
        else (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()
    )
    with tmp.open("wb") as fp:
        os.fchmod(fp.fileno(), 0o600)
        fp.write(data)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as fp:
        for b in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def cast_hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value.lower()):
        fail("Invalid full cast hash")
    return value.lower()


def cast(raw, protocol=False):
    if protocol:
        d = raw["data"]
        if d["type"] not in (1, "MESSAGE_TYPE_CAST_ADD") or d["network"] not in (
            1,
            "FARCASTER_NETWORK_MAINNET",
        ):
            fail("Unexpected protocol type/network")
        fid, timestamp, body = d["fid"], d["timestamp"], d["castAddBody"]
        parent = body.get("parentCastId")
        parent = [int(parent["fid"]), cast_hash(parent["hash"])] if parent else None
    else:
        fid, body = raw["author"]["fid"], raw
        timestamp = (
            int(
                dt.datetime.fromisoformat(
                    raw["timestamp"].replace("Z", "+00:00")
                ).timestamp()
            )
            - EPOCH
        )
        parent = (
            [raw.get("parent_author", {}).get("fid"), cast_hash(raw["parent_hash"])]
            if raw.get("parent_hash")
            else None
        )
    if (
        not isinstance(fid, int)
        or fid <= 0
        or not isinstance(timestamp, int)
        or timestamp < 0
    ):
        fail("Invalid FID or Farcaster timestamp")
    return dict(
        fid=fid,
        hash=cast_hash(raw["hash"]),
        timestamp=timestamp,
        unix_timestamp=timestamp + EPOCH,
        published=dt.datetime.fromtimestamp(
            timestamp + EPOCH, dt.timezone.utc
        ).isoformat(),
        text=body.get("text", ""),
        parent=parent,
        raw=raw,
        protocol=protocol,
    )


def plan(own):
    ordered = sorted(own, key=lambda c: (c["timestamp"], c["hash"]))
    if any(c["fid"] != FID for c in ordered) or len(
        {c["hash"] for c in ordered}
    ) != len(ordered):
        fail("Deletion plan contains wrong author or duplicate hashes")
    return dict(fid=FID, protected=None, deletes=[c["hash"] for c in ordered])


class APIError(RuntimeError):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__("API HTTP " + str(status))

    def missing(self):
        # A proxy 404/HTML page does not establish protocol absence.
        try:
            o = json.loads(self.body)
            code = str(o.get("errCode", "")).lower()
            return self.status in (400, 404) and (
                code in ("not_found", "not_found.cast", "not_found.message")
                or (
                    self.status == 404
                    and o.get("code") == "NotFound"
                    and o.get("property") == "cast"
                )
                or (
                    o.get("error") == "Failed to get cast"
                    and 'status: NotFound, message: "cast not found"'
                    in o.get("error_detail", "")
                )
            )
        except (ValueError, AttributeError):
            return False


class ReadPacer:
    """Share a no-burst budget across Neynar HTTP and gRPC read requests.

    4/s overall stays below the smallest published standard plan (Starter:
    5/s per endpoint, 500/min global). Search has its own 60/min limit.
    https://docs.neynar.com/reference/what-are-the-rate-limits-on-neynar-apis
    """

    def __init__(self, clock=None, sleep=None):
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = threading.Lock()
        self.next_global = 0
        self.next_endpoint = {}

    def wait(self, endpoint):
        with self.lock:
            delay = (
                max(self.next_global, self.next_endpoint.get(endpoint, 0))
                - self.clock()
            )
            if delay > 0:
                self.sleep(delay)
            current = self.clock()
            self.next_global = current + 0.25
            self.next_endpoint[endpoint] = current + (
                1.01 if endpoint == "/v2/farcaster/cast/search" else 0.25
            )

    def defer(self, seconds):
        with self.lock:
            self.next_global = max(self.next_global, self.clock() + max(0, seconds))


def retry_delay(value, minimum):
    """Honor Retry-After seconds or HTTP dates without shortening the delay."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = 0
    return max(minimum, seconds)


class Client:
    def __init__(self, root):
        self.root = root
        self.node = os.environ.get(
            "SNAPCHAIN_NODE", "https://haatz.quilibrium.com"
        ).rstrip("/")
        self.second = os.environ.get(
            "SECONDARY_NODE", "https://snapchain-api.neynar.com"
        ).rstrip("/")
        self.neynar = "https://api.neynar.com"
        self.rpc = os.environ.get("OP_RPC_URL", "https://mainnet.optimism.io")
        self.key = (
            Path(
                os.environ.get(
                    "NEYNAR_KEY_FILE", str(Path.home() / ".config/neynar_api_key")
                )
            )
            .read_text()
            .strip()
        )
        if not self.key:
            fail("Saved Neynar key is empty")
        import httpx

        self.httpx = httpx
        self.pacer = ReadPacer()
        self.wire = WireReader(self.key, self.pacer)
        self.http = httpx.Client(
            timeout=40,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=12),
        )
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.http.close()
        self.wire.channel.close()

    def request(self, base, path="", params=None, cache=None, data=None):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        key = hashlib.sha256(url.encode()).hexdigest()
        file = cache / (key + ".json") if cache else None
        if file and file.exists():
            envelope = read(file)
            if (
                envelope["url"] != url
                or envelope["sha256"]
                != hashlib.sha256(
                    json.dumps(envelope["response"], sort_keys=True).encode()
                ).hexdigest()
            ):
                fail("Cached response integrity error")
            return envelope["response"]
        headers = {
            "Accept": "application/json",
            "User-Agent": "Farcaster-local-archive/1",
        }
        if base in (self.neynar, "https://snapchain-api.neynar.com"):
            headers["x-api-key"] = self.key
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            if "x-api-key" in headers:
                self.pacer.wait(path.rstrip("/"))
            try:
                response = self.http.request(
                    "POST" if data is not None else "GET",
                    url,
                    headers=headers,
                    content=data,
                )
            except self.httpx.TransportError:
                if attempt == 3:
                    fail(
                        "API transport failed at " + urllib.parse.urlsplit(base).netloc
                    )
                time.sleep(2**attempt)
                continue
            if response.is_success:
                obj = response.json()
                if not isinstance(obj, dict):
                    fail("Non-object API response")
                if file:
                    write(
                        file,
                        dict(
                            url=url,
                            fetched_at=now(),
                            response=obj,
                            sha256=hashlib.sha256(
                                json.dumps(obj, sort_keys=True).encode()
                            ).hexdigest(),
                        ),
                    )
                return obj
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise APIError(response.status_code, response.text[:8192])
            retry_after = response.headers.get("Retry-After", "0")
            delay = retry_delay(retry_after, 2**attempt)
            if "x-api-key" in headers:
                self.pacer.defer(delay)
            else:
                time.sleep(delay)

    def pages(self, base, path, params, field, cache=None):
        cursor = None
        seen = set()
        neynar = base == self.neynar
        for _ in range(20000):
            q = dict(params)
            q["limit" if neynar else "pageSize"] = 100 if neynar else 1000
            if cursor:
                q["cursor" if neynar else "pageToken"] = cursor
            obj = self.request(base, path, q, cache)
            vals = obj.get(field)
            if not isinstance(vals, list):
                fail(path + ": missing " + field + " array")
            yield vals
            if neynar and (
                not isinstance(obj.get("next"), dict) or "cursor" not in obj["next"]
            ):
                fail(path + ": missing pagination completion marker")
            token = obj["next"]["cursor"] if neynar else obj.get("nextPageToken")
            if token is None or token == "":
                return
            # These cross-shard endpoints encode Vec<Option<Vec<u8>>> as base64 JSON.
            # Snapchain get_links_by_target serializes the all-exhausted vector too.
            if not neynar and path in ("/v1/linksByTargetFid", "/v1/castsByParent"):
                shards = json.loads(base64.b64decode(token, validate=True))
                if not isinstance(shards, list) or not shards:
                    fail("Invalid cross-shard pagination state")
                if all(t is None for t in shards):
                    return
            if not isinstance(token, str) or token in seen:
                fail(path + ": invalid/repeated pagination token")
            seen.add(token)
            cursor = token
        fail(path + ": pagination limit reached")

    def all(self, base, path, params, field="messages", cache=None):
        return [
            x for page in self.pages(base, path, params, field, cache) for x in page
        ]

    def custody(self, expected_address=None):
        def rpc(method, params):
            o = self.request(
                self.rpc,
                data=json.dumps(
                    dict(jsonrpc="2.0", id=1, method=method, params=params)
                ).encode(),
            )
            if "error" in o or "result" not in o:
                fail("Optimism RPC error")
            return o["result"]

        if int(rpc("eth_chainId", []), 16) != 10:
            fail("Wrong RPC chain")
        block = rpc("eth_blockNumber", [])
        # Independently checked with `cast sig custodyOf(uint256)` and IdRegistry source.
        word = rpc(
            "eth_call",
            [{"to": REGISTRY, "data": "0x65269e47" + format(FID, "064x")}, block],
        )
        if not isinstance(word, str) or not re.fullmatch(
            r"0x0{24}[0-9a-fA-F]{40}", word
        ):
            fail("Invalid on-chain custody response")
        address = "0x" + word[-40:]
        if int(address, 16) == 0:
            fail("FID is not registered")
        if expected_address is not None and address.lower() != expected_address.lower():
            fail(
                "On-chain custody differs from the archived address; make a new backup"
            )
        return dict(
            fid=FID,
            custody=address,
            chain_id=10,
            block=block,
            registry=REGISTRY,
            checked_at=now(),
            limitation="Checks public custody. Secret possession is checked locally only before signing.",
        )

    def nodes(self):
        a = self.request(self.node, "/v1/info")
        b = self.request(self.second, "/v1/info")
        if not a.get("peer_id") or not b.get("peer_id") or a["peer_id"] == b["peer_id"]:
            fail("Independent node peer IDs are required")
        return {
            "primary": a,
            "secondary": b,
            "primary_url": self.node,
            "secondary_url": self.second,
        }

    def inventory(self, cache=None):
        jobs = [
            (self.node, "/v1/castsByFid", {"fid": FID}, "messages"),
            (self.second, "/v1/castsByFid", {"fid": FID}, "messages"),
            (
                self.neynar,
                "/v2/farcaster/feed/user/casts/",
                {"fid": FID, "include_replies": "true"},
                "casts",
            ),
        ]
        results = list(self.pool.map(lambda j: self.all(*j, cache=cache), jobs))
        converted = []
        for i, xs in enumerate(results):
            adds = []
            for x in xs:
                if i < 2 and x.get("data", {}).get("type") in (
                    2,
                    "MESSAGE_TYPE_CAST_REMOVE",
                ):
                    if x["data"]["fid"] != FID or x["data"]["network"] not in (
                        1,
                        "FARCASTER_NETWORK_MAINNET",
                    ):
                        fail("Invalid historical cast-remove author/network")
                    target = base64.b64decode(
                        x["data"]["castRemoveBody"]["targetHash"], validate=True
                    )
                    if len(target) != 20:
                        fail("Invalid historical cast-remove target")
                    continue  # Raw pages retain tombstones; they are not surviving authored casts.
                adds.append(cast(x, i < 2))
            converted.append(adds)
        if any(c["fid"] != FID for xs in converted for c in xs):
            fail("Authored-cast endpoint returned a different author")
        return converted

    def present(self, base, h):
        try:
            c = cast(self.request(base, "/v1/castById", dict(fid=FID, hash=h)), True)
            if c["hash"] != h or c["fid"] != FID:
                fail("Cast lookup returned wrong author/hash")
            return True
        except APIError as e:
            if e.missing():
                return False
            raise


# Official Snapchain protocol descriptors at CLI_REV; upstream GPL-3.0.
WIRE_SCHEMA = """
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
"""


def open_public(url):
    """Open public media without credentials; validate every redirect destination."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    target = url
    for _ in range(8):
        parsed = urllib.parse.urlsplit(target)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
            or not parsed.hostname
        ):
            fail("Unsupported media URL")
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if not addresses or any(
            not ipaddress.ip_address(a[4][0]).is_global for a in addresses
        ):
            fail("Non-public media destination")
        try:
            return opener.open(
                urllib.request.Request(
                    target, headers={"User-Agent": "Farcaster-local-archive/1"}
                ),
                timeout=30,
            )
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308) or not exc.headers.get(
                "Location"
            ):
                raise
            target = urllib.parse.urljoin(target, exc.headers["Location"])
            exc.close()
    fail("Media redirect limit")


class MediaReader:
    """Pace each public origin, honor throttling, and cache per-run NXDOMAIN."""

    def __init__(self, clock=None, sleep=None, interval=0.5):
        if interval < 0.5:
            fail("Media request interval cannot be below 0.5 seconds")
        self.interval = interval
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = threading.Lock()
        self.next_host = {}
        self.dns_missing = {}
        self.events = []

    def wait(self, host):
        while True:
            with self.lock:
                if host in self.dns_missing:
                    raise socket.gaierror(*self.dns_missing[host])
                delay = self.next_host.get(host, 0) - self.clock()
                if delay <= 0:
                    self.next_host[host] = self.clock() + self.interval
                    return
            self.sleep(delay)

    def __call__(self, url):
        host = urllib.parse.urlsplit(url).netloc
        for attempt in range(3):
            self.wait(host)
            try:
                return open_public(url)
            except socket.gaierror as exc:
                if exc.errno == socket.EAI_NONAME:
                    with self.lock:
                        self.dns_missing[host] = (exc.errno, exc.strerror)
                raise
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 502, 503, 504) or attempt == 2:
                    raise
                delay = retry_delay(
                    exc.headers.get("Retry-After"), max(self.interval, 5 * 2**attempt)
                )
                with self.lock:
                    self.next_host[host] = max(
                        self.next_host.get(host, 0), self.clock() + delay
                    )
                    self.events.append(
                        dict(
                            url=url,
                            status=exc.code,
                            attempt=attempt + 1,
                            retry_after_seconds=delay,
                            at=now(),
                        )
                    )
                exc.close()


def media_signature(prefix):
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "image/avif" if prefix[8:12] in (b"avif", b"avis") else "video/mp4"
    return None


def expects_media(url):
    return Path(urllib.parse.urlsplit(url).path).suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".svg",
        ".mp4",
        ".mov",
        ".webm",
        ".m3u8",
        ".mp3",
        ".m4a",
        ".wav",
    }


class HlsArchive:
    """Save finite HLS playlists and all their referenced resources for local playback."""

    def __init__(self, root, limit, fetch=None):
        self.root, self.limit = root, limit
        self.fetch = fetch or open_public
        self.used = 0
        self.done = {}
        self.visiting = set()
        self.resources = []

    def account(self, count):
        self.used += count
        if self.used > self.limit:
            fail("HLS bundle exceeds the per-asset byte limit")

    def record(self, path, source):
        self.resources.append(
            dict(
                path=str(path), url=source, bytes=path.stat().st_size, sha256=sha(path)
            )
        )

    def get(self, url, kind="segment"):
        if url in self.done:
            return self.done[url]
        if url in self.visiting:
            fail("Cycle in HLS playlists")
        if len(self.done) + len(self.visiting) >= 10000:
            fail("HLS resource limit reached")
        self.visiting.add(url)
        self.root.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha256(url.encode()).hexdigest()
        with self.fetch(url) as response:
            final_url = response.geturl()
            first = response.read(16)
            mime = response.headers.get_content_type()
            playlist = first.lstrip().startswith(b"#EXTM3U") or mime in (
                "application/vnd.apple.mpegurl",
                "application/x-mpegurl",
            )
            if playlist:
                original = first + response.read(4 * 1024 * 1024 + 1)
                if len(original) > 4 * 1024 * 1024:
                    fail("HLS manifest size limit reached")
                length = response.headers.get("Content-Length")
                if length and len(original) != int(length):
                    fail("Truncated HLS manifest")
                self.account(len(original))
                text = original.decode("utf-8-sig")
                if not text.startswith("#EXTM3U"):
                    fail("Invalid HLS manifest")
                master = (
                    "#EXT-X-STREAM-INF:" in text or "#EXT-X-I-FRAME-STREAM-INF:" in text
                )
                if not master and "#EXT-X-ENDLIST" not in text:
                    fail("Unbounded live HLS playlist cannot be a complete backup")
                if not master and not any(
                    line.strip() and not line.lstrip().startswith("#")
                    for line in text.splitlines()
                ):
                    fail("Empty HLS media playlist")
                original_path = self.root / (stem + ".original.m3u8")
                write(original_path, original)
                self.record(original_path, url)
                output = []
                next_playlist = False
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        child = self.get(
                            urllib.parse.urljoin(final_url, stripped),
                            "playlist" if next_playlist else "segment",
                        )
                        output.append(child.name)
                        next_playlist = False
                    elif stripped.startswith("#"):
                        expected = (
                            "key"
                            if stripped.startswith(
                                ("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")
                            )
                            else (
                                "playlist"
                                if stripped.startswith(
                                    ("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")
                                )
                                else "segment"
                            )
                        )

                        def substitute(match):
                            child = self.get(
                                urllib.parse.urljoin(final_url, match.group(1)),
                                expected,
                            )
                            return 'URI="' + child.name + '"'

                        output.append(re.sub(r'URI="([^"]+)"', substitute, line))
                        next_playlist = stripped.startswith("#EXT-X-STREAM-INF:")
                    else:
                        output.append(line)
                rewritten = ("\n".join(output) + "\n").encode()
                self.account(len(rewritten))
                path = self.root / (stem + ".m3u8")
                write(path, rewritten)
                self.record(path, url)
            else:
                if kind == "playlist":
                    fail("HLS reference did not return a playlist")
                suffix = Path(urllib.parse.urlsplit(final_url).path).suffix.lower()
                if kind == "key":
                    suffix = ".key"
                elif suffix not in (
                    ".ts",
                    ".mp4",
                    ".m4s",
                    ".aac",
                    ".m4a",
                    ".mp3",
                    ".vtt",
                ):
                    suffix = (
                        ".mp4"
                        if first[4:8] in (b"ftyp", b"styp", b"moof", b"sidx")
                        else ".ts"
                    )
                path = self.root / (stem + suffix)
                tmp = path.with_suffix(path.suffix + ".partial")
                total = 0
                with tmp.open("wb") as fp:
                    chunk = first
                    while chunk:
                        self.account(len(chunk))
                        total += len(chunk)
                        fp.write(chunk)
                        chunk = response.read(1024 * 1024)
                    fp.flush()
                    os.fsync(fp.fileno())
                length = response.headers.get("Content-Length")
                if not total or length and total != int(length):
                    fail("Truncated or empty HLS resource")
                if kind == "key" and total != 16:
                    fail("Unsupported HLS encryption key size")
                os.replace(tmp, path)
                self.record(path, url)
        self.visiting.remove(url)
        self.done[url] = path
        return path


class WireReader:
    """Read original protocol bytes with the pinned official protobuf descriptors."""

    def __init__(self, key, pacer=None):
        self.pacer = pacer or ReadPacer()
        import grpc
        import blake3
        import zlib
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        self.grpc, self.blake3, self.public_key = grpc, blake3, Ed25519PublicKey
        schema = descriptor_pb2.FileDescriptorSet.FromString(
            zlib.decompress(base64.b64decode(WIRE_SCHEMA))
        )
        pool = descriptor_pool.DescriptorPool()
        for file in schema.file:
            pool.Add(file)
        self.types = {
            name: message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
            for name in (
                "Message",
                "MessageData",
                "MessagesResponse",
                "FidTimestampRequest",
                "CastId",
            )
        }
        self.channel = grpc.secure_channel(
            "snapchain-grpc-api.neynar.com:443",
            grpc.ssl_channel_credentials(),
            options=[("grpc.max_receive_message_length", 64 * 1024 * 1024)],
        )
        self.metadata = [("x-api-key", key)]
        self.source = "snapchain-grpc-api.neynar.com:443"

    def call(self, method, request, cache):
        if method not in (
            "GetAllCastMessagesByFid",
            "GetCast",
            "GetAllReactionMessagesByFid",
        ):
            fail("Non-read gRPC method is prohibited")
        body = request.SerializeToString()
        filename = cache / (method + "-" + hashlib.sha256(body).hexdigest() + ".pb")
        if filename.exists():
            return filename.read_bytes(), filename
        stub = self.channel.unary_unary(
            "/HubService/" + method,
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        for attempt in range(4):
            self.pacer.wait("/HubService/" + method)
            try:
                response = stub(body, timeout=40, metadata=self.metadata)
                write(filename, response)
                return response, filename
            except self.grpc.RpcError as exc:
                if exc.code() == self.grpc.StatusCode.NOT_FOUND:
                    return None, None
                if (
                    exc.code()
                    not in (
                        self.grpc.StatusCode.UNAVAILABLE,
                        self.grpc.StatusCode.DEADLINE_EXCEEDED,
                        self.grpc.StatusCode.RESOURCE_EXHAUSTED,
                    )
                    or attempt == 3
                ):
                    detail = (exc.details() or "").replace(
                        self.metadata[0][1], "[REDACTED]"
                    )
                    identity = " fid=" + str(request.fid)
                    if method == "GetCast":
                        identity += " hash=0x" + request.hash.hex()
                    fail(
                        "Read-only gRPC "
                        + method
                        + identity
                        + " failed: "
                        + exc.code().name
                        + ": "
                        + detail[:2048]
                    )
                self.pacer.defer(2**attempt)

    def validate(
        self, message, expected_fid=None, expected_hash=None, allowed_types=(1, 2)
    ):
        raw = (
            message.data_bytes
            if message.HasField("data_bytes")
            else message.data.SerializeToString()
        )
        data = self.types["MessageData"].FromString(raw)
        if (
            not raw
            or data != message.data
            or data.network != 1
            or data.type not in allowed_types
        ):
            fail("Original protocol bytes differ from decoded mainnet cast data")
        if (
            expected_fid is not None
            and data.fid != expected_fid
            or expected_hash is not None
            and "0x" + message.hash.hex() != expected_hash
        ):
            fail("Original protocol message identity mismatch")
        if (
            message.hash_scheme != 1
            or message.signature_scheme != 1
            or len(message.hash) != 20
        ):
            fail("Unexpected original protocol signature/hash scheme")
        if self.blake3.blake3(raw).digest()[:20] != message.hash:
            fail("Original signed data hash does not match")
        self.public_key.from_public_bytes(message.signer).verify(
            message.signature, message.hash
        )
        return data

    def reaction_history(self, archive):
        cache = archive.path / "wire" / "responses"
        rows, targets, hashes = [], set(), set()
        self.reaction_rows = rows
        token, tokens = None, set()
        for _ in range(20000):
            request = self.types["FidTimestampRequest"](fid=FID, page_size=1000)
            if token:
                request.page_token = token
            body, filename = self.call("GetAllReactionMessagesByFid", request, cache)
            if body is None:
                fail("Original reaction-history inventory unavailable")
            response = self.types["MessagesResponse"].FromString(body)
            for message in response.messages:
                data = self.validate(message, FID, allowed_types=(3, 4))
                h = "0x" + message.hash.hex()
                if h in hashes:
                    continue
                hashes.add(h)
                filename = archive.path / "wire" / "reactions" / (h + ".pb")
                write(filename, message.SerializeToString())
                target = None
                if data.reaction_body.HasField("target_cast_id"):
                    target = [
                        data.reaction_body.target_cast_id.fid,
                        "0x" + data.reaction_body.target_cast_id.hash.hex(),
                    ]
                    if target[0] <= 0:
                        fail("Invalid historical reaction target FID")
                    cast_hash(target[1])
                    targets.add(tuple(target))
                rows.append(
                    dict(
                        hash=h,
                        fid=FID,
                        type=data.type,
                        timestamp=data.timestamp,
                        reaction_type=data.reaction_body.type,
                        target=target,
                        target_url=data.reaction_body.target_url
                        if data.reaction_body.HasField("target_url")
                        else None,
                        path=str(filename.relative_to(archive.path)),
                        sha256=sha(filename),
                        verified_hash_and_signature=True,
                    )
                )
            token = response.next_page_token
            if not token:
                break
            if token in tokens:
                fail("Repeated original reaction-history cursor")
            tokens.add(token)
        else:
            fail("Original reaction-history pagination limit reached")
        write(
            archive.path / "reaction_history.json",
            dict(source=self.source, complete=True, messages=rows),
        )
        return rows, targets

    def archive(self, archive):
        messages = archive.path / "wire" / "messages"
        index = {}
        self.index = index

        def save(message, expected_fid=None, expected_hash=None):
            data = self.validate(message, expected_fid, expected_hash)
            h = "0x" + message.hash.hex()
            record = archive.casts.get(h)
            if record:
                if (
                    data.type != 1
                    or data.fid != record["fid"]
                    or data.timestamp != record["timestamp"]
                ):
                    fail("Signed cast differs from archived metadata")
                if record["protocol"] and data.cast_add_body.text != record["text"]:
                    fail("Signed cast text differs from archived protocol text")
            filename = messages / (h + ".pb")
            write(filename, message.SerializeToString())
            return h, dict(
                fid=data.fid,
                hash=h,
                type=data.type,
                timestamp=data.timestamp,
                path=str(filename.relative_to(archive.path)),
                sha256=sha(filename),
                original_data_bytes=len(message.data_bytes),
                verified_hash_and_signature=True,
            )

        # JSON content and original hashes define the archive. Keep and verify
        # already obtained protocol originals without making extra RPC reads.
        for filename in sorted(messages.glob("*.pb")):
            h = cast_hash(filename.stem)
            message = self.types["Message"].FromString(filename.read_bytes())
            expected_fid = archive.casts[h]["fid"] if h in archive.casts else None
            h, row = save(message, expected_fid, h)
            index[h] = row
        write(
            archive.path / "wire_index.json",
            dict(
                source=self.source,
                schema_revision=CLI_REV,
                messages=index,
                verified=len(index),
                collection="previously-obtained-originals",
                limitation="Valid original bytes and signatures do not prove historical signer authorization independently of node state.",
            ),
        )
        self.channel.close()
        return len(index)


def cast_media_urls(c):
    urls = set()

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if (
                    key in ("url", "image", "image_url", "video_url", "thumbnail_url")
                    and isinstance(value, str)
                    and value.startswith(("http://", "https://"))
                ):
                    urls.add(value)
                elif isinstance(value, (list, dict)):
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for raw in c["sources"].values():
        body = raw.get("data", {}).get("castAddBody", raw)
        for embed in body.get("embeds", []):
            if embed.get("url"):
                urls.add(embed["url"])
            walk(embed.get("metadata", {}))
        urls.update(body.get("embedsDeprecated", []))
    return urls


CONTACT_FIELDS = (
    "fid",
    "relationships",
    "display_name",
    "farcaster_username",
    "farcaster_url",
    "bio",
    "profile_links",
    "x_usernames",
    "github_usernames",
    "other_verified_accounts",
    "email_links",
    "email_candidates",
    "messaging_links",
    "verified_eth_addresses",
    "verified_sol_addresses",
    "profile_picture_urls",
    "profile_available",
)


def contact_link_kind(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() in ("mailto", "tel", "xmpp", "matrix", "nostr"):
        return parsed.scheme.lower()
    host = (parsed.hostname or "").lower().removeprefix("www.")
    groups = {
        "x": {"x.com", "twitter.com"},
        "github": {"github.com"},
        "telegram": {"t.me", "telegram.me"},
        "discord": {"discord.gg", "discord.com", "discordapp.com"},
        "signal": {"signal.me"},
        "linkedin": {"linkedin.com"},
        "bluesky": {"bsky.app"},
        "instagram": {"instagram.com"},
        "facebook": {"facebook.com", "fb.com"},
        "youtube": {"youtube.com", "youtu.be"},
        "farcaster": {"farcaster.xyz", "warpcast.com"},
    }
    return next((kind for kind, hosts in groups.items() if host in hosts), "website")


def profile_contacts(fid, profiles):
    preferred = [profiles[d] for d in ("following", "followers") if d in profiles]
    for u in preferred:
        if u and u.get("fid") != fid:
            fail("Contact profile FID differs from source key")
    available = [u for u in preferred if u.get("username")]
    primary = (available or preferred)[0]
    endpoints = []
    accounts = set()
    eth, sol, pictures, bios = set(), set(), set(), []
    for direction, u in profiles.items():
        bio = (u.get("profile") or {}).get("bio") or {}
        text = bio.get("text", "")
        if text and text not in bios:
            bios.append(text)
        if u.get("pfp_url"):
            pictures.add(u["pfp_url"])
        addresses = u.get("verified_addresses") or {}
        eth.update(addresses.get("eth_addresses") or [])
        sol.update(addresses.get("sol_addresses") or [])
        for account in u.get("verified_accounts") or []:
            platform, name = account.get("platform"), account.get("username")
            if not isinstance(platform, str) or not isinstance(name, str) or not name:
                continue
            name = name.removeprefix("@")
            accounts.add((platform, name))
            account_url = {
                "x": "https://x.com/",
                "twitter": "https://x.com/",
                "github": "https://github.com/",
            }.get(platform)
            endpoints.append(
                dict(
                    kind=platform,
                    value=name,
                    url=account_url + urllib.parse.quote(name, safe="")
                    if account_url
                    else "",
                    source_relationship=direction,
                    source_field="verified_accounts",
                    verification="Neynar verified_accounts",
                )
            )
        candidates = []
        if isinstance(u.get("url"), str) and u["url"].strip():
            candidates.append((u["url"].strip(), "url", "profile_link"))
        covered = []
        for match in re.finditer(
            r"(?:https?://|mailto:|tel:|xmpp:|matrix:|nostr:)[^\s<>\"']+", text, re.I
        ):
            token = match.group().rstrip(".,;!")
            for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
                while token.endswith(closing) and token.count(closing) > token.count(
                    opening
                ):
                    token = token[:-1]
            candidates.append((token, "profile.bio.text", "profile_link"))
            covered.append(match.span())
        for match in re.finditer(
            r"(?<![\w@])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text
        ):
            if any(a <= match.start() < b for a, b in covered):
                continue
            # An email-like string can also be adjacent prose or an account mention.
            # Preserve it as a candidate; never assert ownership or deliverability.
            endpoints.append(
                dict(
                    kind="email_candidate",
                    value=match.group(),
                    url="",
                    source_relationship=direction,
                    source_field="profile.bio.text",
                    verification="Unverified email-like profile text",
                )
            )
            covered.append(match.span())
        bare = r"(?<![\w@/.-])(?:[A-Za-z0-9][A-Za-z0-9-]{0,62}\.)+[A-Za-z]{2,24}(?:/[^\s<>\"']*)?"
        for match in re.finditer(bare, text):
            if any(a <= match.start() < b for a, b in covered):
                continue
            token = match.group().rstrip(".,;!)")
            if token.split("/", 1)[0].lower().endswith((".eth", ".sol")):
                continue
            candidates.append(
                ("https://" + token, "profile.bio.text", "scheme_added_to_bare_link")
            )
        for value, source_field, normalization in candidates:
            if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
                value = "https://" + value
                normalization = "scheme_added_to_bare_link"
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme.lower() not in (
                "http",
                "https",
                "mailto",
                "tel",
                "xmpp",
                "matrix",
                "nostr",
            ):
                continue
            endpoints.append(
                dict(
                    kind=contact_link_kind(value),
                    value=value,
                    url=value,
                    source_relationship=direction,
                    source_field=source_field,
                    verification="Published profile link; ownership not verified",
                    normalization=normalization,
                )
            )
    endpoints = sorted(
        {json.dumps(e, sort_keys=True): e for e in endpoints}.values(),
        key=lambda e: (
            e["kind"],
            e["value"],
            e["source_relationship"],
            e["source_field"],
        ),
    )
    verified = [dict(platform=p, username=n) for p, n in sorted(accounts)]
    username = primary.get("username", "")
    return dict(
        fid=fid,
        relationships=sorted(profiles),
        display_name=primary.get("display_name", ""),
        farcaster_username=username,
        farcaster_url="https://farcaster.xyz/" + urllib.parse.quote(username, safe="")
        if username
        else "",
        bio=(primary.get("profile") or {}).get("bio", {}).get("text", ""),
        bio_variants=bios,
        profile_available=bool(available),
        verified_accounts=verified,
        endpoints=endpoints,
        verified_eth_addresses=sorted(eth),
        verified_sol_addresses=sorted(sol),
        profile_picture_urls=sorted(pictures),
    )


def contact_catalog(sources):
    people = {}
    for direction in ("followers", "following"):
        source = sources[direction]
        if source.get("relationship") != direction:
            fail("Contact source relationship differs from its file")
        profiles = {int(fid): user for fid, user in source["profiles"].items()}
        fids = set(profiles)
        for message in source.get("protocol_primary", []) + source.get(
            "protocol_secondary", []
        ):
            data = message["data"]
            body = data["linkBody"]
            if (
                body.get("type") != "follow"
                or (
                    data.get("fid")
                    if direction == "following"
                    else body.get("targetFid")
                )
                != FID
            ):
                fail("Invalid contact relationship source")
            fids.add(body["targetFid"] if direction == "following" else data["fid"])
        if source["count"] != len(fids):
            fail("Contact source count differs from unique FIDs")
        for fid in fids:
            people.setdefault(fid, {})[direction] = profiles.get(fid, {})
    records = [
        profile_contacts(fid, profiles) for fid, profiles in sorted(people.items())
    ]
    summary = dict(
        unique_accounts=len(records),
        mutual_accounts=sum(len(p["relationships"]) == 2 for p in records),
    )
    for direction in ("followers", "following"):
        subset = [p for p in records if direction in p["relationships"]]
        summary[direction] = dict(
            accounts=len(subset),
            profiles=sum(p["profile_available"] for p in subset),
            display_names=sum(bool(p["display_name"]) for p in subset),
            verified_x_accounts=sum(
                any(a["platform"] in ("x", "twitter") for a in p["verified_accounts"])
                for p in subset
            ),
            verified_github_accounts=sum(
                any(a["platform"] == "github" for a in p["verified_accounts"])
                for p in subset
            ),
            profile_links=sum(
                any(
                    e["source_field"] != "verified_accounts" and e["url"]
                    for e in p["endpoints"]
                )
                for p in subset
            ),
            explicit_email_links=sum(
                any(e["kind"] == "mailto" for e in p["endpoints"]) for p in subset
            ),
            email_candidates=sum(
                any(e["kind"] == "email_candidate" for e in p["endpoints"])
                for p in subset
            ),
            messaging_links=sum(
                any(
                    e["kind"]
                    in (
                        "telegram",
                        "discord",
                        "signal",
                        "xmpp",
                        "matrix",
                        "nostr",
                        "tel",
                    )
                    for e in p["endpoints"]
                )
                for p in subset
            ),
            verified_eth_wallets=sum(bool(p["verified_eth_addresses"]) for p in subset),
            verified_sol_wallets=sum(bool(p["verified_sol_addresses"]) for p in subset),
        )
    return dict(
        schema=1, fid=FID, summary=summary, people=records, source_profiles=sources
    )


def contact_csv_row(person):
    row = {
        k: person[k]
        for k in (
            "fid",
            "display_name",
            "farcaster_username",
            "farcaster_url",
            "bio",
            "profile_available",
        )
    }
    for key in (
        "relationships",
        "verified_eth_addresses",
        "verified_sol_addresses",
        "profile_picture_urls",
    ):
        row[key] = "; ".join(person[key])
    for field, platforms in (
        ("x_usernames", {"x", "twitter"}),
        ("github_usernames", {"github"}),
    ):
        row[field] = "; ".join(
            sorted(
                {
                    a["username"]
                    for a in person["verified_accounts"]
                    if a["platform"] in platforms
                }
            )
        )
    row["other_verified_accounts"] = json.dumps(
        [
            a
            for a in person["verified_accounts"]
            if a["platform"] not in ("x", "twitter", "github")
        ],
        ensure_ascii=False,
    )
    for key, kinds in (
        ("email_links", {"mailto"}),
        ("email_candidates", {"email_candidate"}),
        (
            "messaging_links",
            {"telegram", "discord", "signal", "xmpp", "matrix", "nostr", "tel"},
        ),
    ):
        row[key] = "; ".join(
            sorted({e["value"] for e in person["endpoints"] if e["kind"] in kinds})
        )
    row["profile_links"] = "; ".join(
        sorted(
            {
                e["url"]
                for e in person["endpoints"]
                if e["url"] and e["source_field"] != "verified_accounts"
            }
        )
    )
    return row


class Archive:
    def __init__(self, api, path):
        self.api, self.path = api, path
        self.cache = path / "raw"
        self.casts = {}
        self.issues = []
        self.thread = set()
        self.mutex = threading.RLock()
        self.media_reader = MediaReader()
        self.unavailable = set()

    def issue(self, kind, **details):
        item = dict(kind=kind, **details)
        with self.mutex:
            self.issues.append(item)
            write(self.path / "issues.json", self.issues)

    def add(self, c, source):
        with self.mutex:
            h = c["hash"]
            if h in self.casts:
                prior = self.casts[h]
                if (
                    prior["fid"],
                    prior["timestamp"],
                    (prior.get("parent") or [None, None])[1],
                ) != (c["fid"], c["timestamp"], (c.get("parent") or [None, None])[1]):
                    fail("Providers disagree on cast content: " + h)
                if prior["protocol"] and c["protocol"] and prior["text"] != c["text"]:
                    fail("Protocol nodes disagree on original cast text: " + h)
                prior["sources"][source] = c["raw"]
                if not prior["protocol"] and c["protocol"]:
                    sources = prior["sources"]
                    self.casts[h] = {**c, "sources": sources}
            else:
                self.casts[h] = {**c, "sources": {source: c["raw"]}}
            return self.casts[h]

    def lookup(self, fid, h):
        if h in self.casts:
            c = self.casts[h]
            if fid and c["fid"] != fid:
                fail("Referenced cast author mismatch")
            return c
        if (fid, h) in self.unavailable:
            return None
        checked = []
        for base in (self.api.node, self.api.second) if fid else ():
            try:
                c = cast(
                    self.api.request(
                        base, "/v1/castById", dict(fid=fid, hash=h), self.cache
                    ),
                    True,
                )
                if c["hash"] != h or c["fid"] != fid:
                    fail("Protocol cast identity mismatch")
                return self.add(c, base)
            except APIError as e:
                if not e.missing():
                    raise
                checked.append(
                    dict(source=base, http_status=e.status, result="not-found")
                )
        try:
            c = cast(
                self.api.request(
                    self.api.neynar,
                    "/v2/farcaster/cast/",
                    dict(identifier=h, type="hash"),
                    self.cache,
                )["cast"]
            )
            if c["hash"] != h or fid and c["fid"] != fid:
                fail("Neynar cast identity mismatch")
            return self.add(c, self.api.neynar)
        except APIError as e:
            if not e.missing():
                raise
            checked.append(
                dict(source=self.api.neynar, http_status=e.status, result="not-found")
            )
            self.unavailable.add((fid, h))
            self.issue(
                "unavailable-cast",
                fid=fid,
                hash=h,
                checked_sources=checked,
                cause="Deleted, pruned, or otherwise unavailable; exact cause unproven",
            )
            return None

    def connections(self, direction):
        if direction not in ("followers", "following"):
            fail("Invalid follow relationship direction")
        following = direction == "following"
        a = self.api
        endpoint = "/v1/linksByFid" if following else "/v1/linksByTargetFid"
        params = (
            dict(fid=FID, link_type="follow")
            if following
            else dict(target_fid=FID, link_type="follow")
        )
        links = a.all(
            a.node,
            endpoint,
            params,
            cache=self.cache,
        )
        links2 = a.all(
            a.second,
            endpoint,
            params,
            cache=self.cache,
        )
        profiles = a.all(
            a.neynar,
            "/v2/farcaster/" + direction + "/",
            {"fid": FID},
            "users",
            self.cache,
        )
        fids = set()
        for m in links + links2:
            if (
                m["data"]["type"] not in (5, "MESSAGE_TYPE_LINK_ADD")
                or m["data"]["linkBody"].get("type") != "follow"
                or (
                    m["data"].get("fid")
                    if following
                    else m["data"]["linkBody"].get("targetFid")
                )
                != FID
            ):
                fail("Unexpected " + direction + " link message")
            other = (
                m["data"]["linkBody"].get("targetFid")
                if following
                else m["data"]["fid"]
            )
            if not isinstance(other, int) or other <= 0:
                fail("Invalid follow relationship FID")
            fids.add(other)
        users = {x["user"]["fid"]: x["user"] for x in profiles}
        missing = sorted(fids - users.keys())
        for start in range(0, len(missing), 100):
            result = a.request(
                a.neynar,
                "/v2/farcaster/user/bulk/",
                {"fids": ",".join(map(str, missing[start : start + 100]))},
                self.cache,
            )
            for user in result["users"]:
                users[user["fid"]] = user
        for fid in fids - users.keys():
            self.issue("missing-" + direction + "-profile", fid=fid)
        fids.update(users)
        rows = []
        for fid in sorted(fids):
            user = users.get(fid, {})
            accounts = user.get("verified_accounts") or []
            handles = sorted(
                {
                    x["username"].lstrip("@")
                    for x in accounts
                    if x.get("platform") in ("x", "twitter")
                    and re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", x.get("username", ""))
                }
            )
            for handle in handles or [""]:
                rows.append(
                    dict(
                        relationship=direction,
                        fid=fid,
                        farcaster_username=user.get("username", ""),
                        display_name=user.get("display_name", ""),
                        x_username=handle,
                        x_url="https://x.com/" + handle if handle else "",
                        verification_source="Neynar verified_accounts"
                        if handle
                        else "",
                    )
                )
        write(
            self.path / (direction + ".json"),
            dict(
                relationship=direction,
                protocol_primary=links,
                protocol_secondary=links2,
                profiles=users,
                count=len(fids),
            ),
        )
        self.csv(
            direction + ".csv",
            rows,
            [
                "relationship",
                "fid",
                "farcaster_username",
                "display_name",
                "x_username",
                "x_url",
                "verification_source",
            ],
        )
        self.csv(
            direction + "_verified_x.csv",
            [r for r in rows if r["x_username"]],
            list(rows[0]) if rows else ["fid", "x_username"],
        )
        md = [
            "# " + direction.title(),
            "",
            "Verified X names come from Neynar. They are not Farcaster protocol attestations.",
            "",
        ]
        md += [
            f"- FID {r['fid']} @{r['farcaster_username']} {r['display_name']} {r['x_url']}"
            for r in rows
        ]
        write(self.path / (direction + ".md"), "\n".join(md).encode())
        print(
            direction.title() + ":",
            len(fids),
            "unique;",
            sum(bool(r["x_username"]) for r in rows),
            "verified X rows",
            flush=True,
        )
        return len(fids), len(
            {r["x_username"].lower() for r in rows if r["x_username"]}
        )

    def export_contacts(self):
        sources = {
            d: read(self.path / (d + ".json")) for d in ("followers", "following")
        }
        catalog = contact_catalog(sources)
        write(self.path / "contacts.json", catalog)
        write(self.path / "contacts_summary.json", catalog["summary"])
        rows = [contact_csv_row(p) for p in catalog["people"]]
        self.csv("contacts.csv", rows, CONTACT_FIELDS)
        for direction in ("followers", "following"):
            self.csv(
                "contacts_" + direction + ".csv",
                [
                    contact_csv_row(p)
                    for p in catalog["people"]
                    if direction in p["relationships"]
                ],
                CONTACT_FIELDS,
            )
        endpoints = [
            {
                "fid": p["fid"],
                "relationships": "; ".join(p["relationships"]),
                "display_name": p["display_name"],
                **e,
            }
            for p in catalog["people"]
            for e in p["endpoints"]
        ]
        self.csv(
            "contacts_endpoints.csv",
            endpoints,
            [
                "fid",
                "relationships",
                "display_name",
                "kind",
                "value",
                "url",
                "source_relationship",
                "source_field",
                "verification",
                "normalization",
            ],
        )

        def escape(value):
            return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", str(value)).replace(
                "\n", " "
            )

        def link(endpoint):
            label = escape(endpoint["value"])
            if not endpoint["url"]:
                return label
            target = (
                endpoint["url"]
                .replace("<", "%3C")
                .replace(">", "%3E")
                .replace("\n", "%0A")
            )
            return "[" + label + "](<" + target + ">)"

        md = [
            "# Farcaster contact catalog",
            "",
            "Profile links can refer to projects or other people. Provider-verified accounts and published links retain separate provenance. Email-like text remains an unverified candidate. Wallet addresses are account identifiers, not verified email or telephone contacts.",
            "",
        ]
        for p in catalog["people"]:
            md.extend(
                [
                    "## FID " + str(p["fid"]) + " | " + escape(p["display_name"]),
                    "",
                    "Relationships: " + ", ".join(p["relationships"]),
                    "Farcaster: " + p["farcaster_url"],
                    "Bio: " + escape(p["bio"]),
                    "",
                ]
            )
            md.extend(
                "- "
                + escape(e["kind"])
                + ": "
                + link(e)
                + " ("
                + escape(e["verification"])
                + ")"
                for e in p["endpoints"]
            )
            md.append("")
        write(self.path / "contacts.md", "\n".join(md).encode())
        return catalog["summary"]

    def csv(self, name, rows, fields):
        import io

        fp = io.StringIO(newline="")
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # CSV text can be opened in a spreadsheet; JSON retains the original text.
            writer.writerow(
                {
                    k: "'" + v
                    if isinstance(v, str)
                    and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                    else v
                    for k, v in row.items()
                }
            )
        write(self.path / name, fp.getvalue().encode())

    def reactions(self, own):
        a = self.api
        raw = []
        for base in (a.node, a.second):
            for typ in ("Like", "Recast"):
                raw += a.all(
                    base,
                    "/v1/reactionsByFid",
                    dict(fid=FID, reaction_type=typ),
                    cache=self.cache,
                )
        try:
            history, historical_targets = a.wire.reaction_history(self)
        except Exception as exc:
            self.issue(
                "reaction-history-failed", error=type(exc).__name__, detail=str(exc)
            )
            history = getattr(a.wire, "reaction_rows", [])
            historical_targets = {
                tuple(r["target"]) for r in history if r.get("target")
            }
            write(
                self.path / "reaction_history.json",
                dict(source=a.wire.source, complete=False, messages=history),
            )
        targets = set(historical_targets)
        for m in raw:
            if m["data"]["fid"] != FID or m["data"]["type"] not in (
                3,
                "MESSAGE_TYPE_REACTION_ADD",
            ):
                fail("Unexpected reaction author/type")
            b = m["data"]["reactionBody"]
            if b.get("targetCastId"):
                t = b["targetCastId"]
                targets.add((t["fid"], cast_hash(t["hash"])))
        hydrated = a.all(
            a.neynar,
            "/v2/farcaster/reactions/user/",
            dict(fid=FID, type="all"),
            "reactions",
            self.cache,
        )
        for r in hydrated:
            if not isinstance(r.get("cast"), dict):
                self.issue("missing-reaction-target", reaction=r)
                continue
            c = self.add(cast(r["cast"]), a.neynar)
            targets.add((c["fid"], c["hash"]))
        quotes = set()
        for c in own:
            for src in c["sources"].values():
                b = src.get("data", {}).get("castAddBody", src)
                for e in b.get("embeds", []):
                    t = e.get("castId") or e.get("cast")
                    if t:
                        quotes.add(
                            (
                                t.get("fid") or (t.get("author") or {}).get("fid"),
                                cast_hash(t["hash"]),
                            )
                        )
                    u = e.get("url", "")
                    match = re.search(r"/(0x[0-9a-fA-F]{40})(?:[/?#]|$)", u)
                    if match and urllib.parse.urlsplit(u).hostname in (
                        "warpcast.com",
                        "farcaster.xyz",
                    ):
                        quotes.add((None, cast_hash(match[1])))
        for fid, h in targets | quotes:
            self.lookup(fid, h)
        write(
            self.path / "reactions.json",
            dict(
                protocol=raw,
                neynar=hydrated,
                original_history=history,
                targets=sorted(targets),
                quotes=sorted(quotes, key=lambda t: t[1]),
            ),
        )
        print(
            "Reaction targets:",
            len(targets),
            "; quoted targets:",
            len(quotes),
            flush=True,
        )
        return len(targets), len(quotes)

    def threads(self, own):
        def ancestors(c):
            seen = set()
            while c:
                h = c["hash"]
                if h in seen:
                    fail("Cycle in parent graph")
                seen.add(h)
                with self.mutex:
                    self.thread.add(h)
                if not c["parent"]:
                    return h
                parent = self.lookup(*c["parent"])
                if not parent:
                    self.issue("missing-parent", child=h, parent=c["parent"])
                    # A deleted immediate parent must not hide an obtainable root/siblings.
                    indexed = [
                        r for r in list(c["sources"].values()) if "thread_hash" in r
                    ]
                    if not indexed:
                        try:
                            raw = self.api.request(
                                self.api.neynar,
                                "/v2/farcaster/cast/",
                                dict(identifier=h, type="hash"),
                                self.cache,
                            )["cast"]
                            self.add(cast(raw), self.api.neynar)
                            indexed = [raw]
                        except APIError as exc:
                            if exc.status != 404:
                                raise
                    root_hashes = {
                        r.get("thread_hash") for r in indexed if r.get("thread_hash")
                    }
                    for root_hash in root_hashes:
                        root_hash = cast_hash(root_hash)
                        if root_hash not in seen:
                            root = self.lookup(None, root_hash)
                            if root:
                                c = root
                                break
                    else:
                        return h
                    continue
                c = parent

        roots = set()
        for ix, root in enumerate(self.api.pool.map(ancestors, own), 1):
            roots.add(root)
            if ix % 100 == 0:
                print("Ancestor walks:", ix, "/", len(own), flush=True)

        def descendants(h):
            c = self.casts[h]
            children = {}
            for batch in self.api.pages(
                self.api.node,
                "/v1/castsByParent",
                dict(fid=c["fid"], hash=h),
                "messages",
                self.cache,
            ):
                for raw in batch:
                    child = cast(raw, True)
                    if child["parent"] != [c["fid"], h]:
                        fail("Protocol returned an unrelated child")
                    children[child["hash"]] = self.add(child, self.api.node)
            # A hydrated cast with an explicit zero reply count is already a Neynar
            # leaf record. Still query every protocol reply page above to reconcile it.
            indexed = [r for r in list(c["sources"].values()) if "replies" in r]
            if indexed and all(r.get("replies", {}).get("count") == 0 for r in indexed):
                return children
            cursor, cursors = None, set()
            for _ in range(20000):
                params = dict(
                    identifier=h,
                    type="hash",
                    reply_depth=1,
                    include_chronological_parent_casts="false",
                    limit=50,
                )
                if cursor:
                    params["cursor"] = cursor
                try:
                    obj = self.api.request(
                        self.api.neynar,
                        "/v2/farcaster/cast/conversation/",
                        params,
                        self.cache,
                    )
                except APIError as e:
                    if e.status == 404:
                        self.issue("neynar-conversation-unavailable", hash=h)
                        break
                    raise
                root = obj["conversation"]["cast"]
                if cast_hash(root["hash"]) != h:
                    fail("Conversation response root mismatch")
                self.add(cast(root), self.api.neynar)
                replies = root.get("direct_replies")
                if not isinstance(replies, list):
                    fail("Conversation missing direct_replies array")
                for raw in replies:
                    child = cast(raw)
                    if not child["parent"] or child["parent"][1] != h:
                        fail("Neynar returned unrelated direct reply")
                    children[child["hash"]] = self.add(child, self.api.neynar)
                if not isinstance(obj.get("next"), dict) or "cursor" not in obj["next"]:
                    fail("Conversation missing pagination completion marker")
                cursor = obj["next"]["cursor"]
                if not cursor:
                    break
                if not isinstance(cursor, str) or cursor in cursors:
                    fail("Repeated conversation cursor")
                cursors.add(cursor)
            else:
                fail("Conversation pagination limit reached")
            return children

        todo = collections.deque(sorted(self.thread))
        visited = set()
        pending = {}
        completed = 0
        try:
            while todo or pending:
                while todo and len(pending) < 6:
                    h = todo.popleft()
                    if h in visited:
                        continue
                    visited.add(h)
                    if len(visited) > int(os.environ.get("THREAD_MAX_NODES", "250000")):
                        fail("Thread traversal limit reached")
                    pending[self.api.pool.submit(descendants, h)] = h
                if not pending:
                    continue
                done, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    pending.pop(future)
                    children = future.result()
                    self.thread.update(children)
                    todo.extend(children)
                    completed += 1
                    if completed % 60 == 0:
                        print(
                            "Thread casts checked:",
                            completed,
                            "; queue:",
                            len(todo),
                            flush=True,
                        )
        finally:
            for future in pending:
                future.cancel()
        write(
            self.path / "threads.json",
            dict(
                roots=sorted(roots), hashes=sorted(self.thread), traversed=len(visited)
            ),
        )
        return len(roots), len(self.thread)

    def download_media(self, url):
        record = dict(url=url)
        filename = "media/" + hashlib.sha256(url.encode()).hexdigest()
        dest = self.path / filename
        meta = dest.with_suffix(".json")
        if meta.exists():
            r = read(meta)
            if (
                r.get("status") == "non-media-link"
                and r.get("content_type")
                not in ("application/octet-stream", "application/pdf", "text/plain")
                and not expects_media(url)
                and not expects_media(r.get("final_url", url))
            ):
                return r
            if r.get("status") == "downloaded":
                files = [
                    dict(path=r["path"], sha256=r["sha256"], bytes=r["bytes"]),
                    *r.get("resources", []),
                ]
                for entry in files:
                    local = self.path / entry["path"]
                    if (
                        not local.is_file()
                        or local.stat().st_size != entry["bytes"]
                        or sha(local) != entry["sha256"]
                    ):
                        fail("Media checksum mismatch")
                return r
        try:
            response = self.media_reader(url)
            target = response.geturl()
            with response:
                content_type = response.headers.get_content_type()
                record.update(content_type=content_type, final_url=target)
                is_hls = content_type in (
                    "application/vnd.apple.mpegurl",
                    "application/x-mpegurl",
                ) or urllib.parse.urlsplit(target).path.endswith(".m3u8")
                if is_hls:
                    # Close this detection response; the bounded HLS fetch saves every original resource.
                    response.close()
                    hls = HlsArchive(
                        dest,
                        int(os.environ.get("MEDIA_MAX_BYTES", str(512 * 1024 * 1024))),
                        fetch=self.media_reader,
                    )
                    playlist = hls.get(target, "playlist")
                    resources = [
                        {**r, "path": str(Path(r["path"]).relative_to(self.path))}
                        for r in hls.resources
                    ]
                    record.update(
                        status="downloaded",
                        path=str(playlist.relative_to(self.path)),
                        bytes=playlist.stat().st_size,
                        sha256=sha(playlist),
                        resources=resources,
                        bundle_bytes=sum(r["bytes"] for r in resources),
                        format="hls-vod",
                    )
                    write(meta, record)
                    return record
                prefix = b""
                if not content_type.startswith(
                    ("image/", "video/", "audio/")
                ) and content_type not in (
                    "application/octet-stream",
                    "application/pdf",
                ):
                    prefix = response.read(64)
                    detected = media_signature(prefix)
                    if detected:
                        record["detected_content_type"] = detected
                    else:
                        if expects_media(url) or expects_media(target):
                            fail(
                                "Media URL returned non-media content: " + content_type
                            )
                        record["status"] = "non-media-link"
                        write(meta, record)
                        return record
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".partial")
                total = len(prefix)
                if total > int(
                    os.environ.get("MEDIA_MAX_BYTES", str(512 * 1024 * 1024))
                ):
                    fail("Media exceeds per-file byte limit")
                with tmp.open("wb") as fp:
                    fp.write(prefix)
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > int(
                            os.environ.get("MEDIA_MAX_BYTES", str(512 * 1024 * 1024))
                        ):
                            fail("Media exceeds per-file byte limit")
                        fp.write(chunk)
                    fp.flush()
                    os.fsync(fp.fileno())
                length = response.headers.get("Content-Length")
                if not total or length and total != int(length):
                    fail("Truncated or empty media response")
                os.replace(tmp, dest)
                record.update(
                    status="downloaded",
                    path=filename,
                    bytes=total,
                    sha256=sha(dest),
                )
                write(meta, record)
        except Exception as e:
            record.update(
                status="unresolved", error=type(e).__name__, detail=str(e)[:400]
            )
        return record

    def media(self):
        references = collections.defaultdict(set)
        own_hashes = {h for h, c in self.casts.items() if c.get("fid") == FID}
        for h, c in self.casts.items():
            for url in cast_media_urls(c):
                references[url].add(h)
        urls = set(references)

        records = []
        for r in self.api.pool.map(self.download_media, sorted(urls)):
            r["referenced_by"] = sorted(references[r["url"]])
            r["authored_casts"] = sorted(references[r["url"]] & own_hashes)
            records.append(r)
            if r["status"] == "unresolved":
                self.issue("unresolved-embed", url=r["url"], error=r["error"])
            if len(records) % 50 == 0:
                print("Embed URLs checked:", len(records), "/", len(urls), flush=True)
        self.media_records = records
        write(self.path / "media.json", records)
        write(self.path / "media_retries.json", self.media_reader.events)
        return sum(r["status"] == "downloaded" for r in records)

    def render_casts(self, casts, media):
        by_url = {record["url"]: record for record in media}
        for name, records in (
            ("casts.md", casts),
            ("own_casts.md", [record for record in casts if record["fid"] == FID]),
        ):
            md = [
                f"# Farcaster archive for FID {FID}",
                "",
                "Full original content and source metadata remain in casts.json. Local media links work without Farcaster.",
                "",
            ]
            for record in records:
                text = next(
                    (
                        raw["text"]
                        for raw in record["sources"].values()
                        if "author" in raw and isinstance(raw.get("text"), str)
                    ),
                    record["text"],
                )
                md += [
                    f"## FID {record['fid']} | {record['published']}",
                    f"Hash: `{record['hash']}`",
                    f"Parent: `{record['parent']}`",
                    "",
                    *["> " + line for line in text.splitlines()],
                    "",
                ]
                for url in sorted(cast_media_urls(record)):
                    item = by_url.get(url, {})
                    if item.get("status") == "downloaded":
                        kind = item.get(
                            "detected_content_type", item.get("content_type", "")
                        )
                        md.append(
                            f"- [Local media]({item['path']}) | [Original URL](<{url}>)"
                        )
                        if kind.startswith("image/"):
                            md.append(f"![Embedded image]({item['path']})")
                    else:
                        detail = item.get("detail", item.get("status", "not retrieved"))
                        md.append(f"- [Embed URL](<{url}>) — {detail}")
                md.append("")
            write(self.path / name, "\n".join(md).encode())

    def run(self):
        a = self.api
        write(self.path / "custody.json", a.custody())
        write(self.path / "nodes.json", a.nodes())
        inventories = a.inventory(self.cache)
        for source, cs in zip((a.node, a.second, a.neynar), inventories):
            for c in cs:
                self.add(c, source)
        own = list(self.casts.values())
        write(
            self.path / "own_inventory.json",
            [[c["hash"] for c in cs] for cs in inventories],
        )
        deletion = plan(own)
        write(self.path / "deletion_plan.json", deletion)
        write(self.path / "own_casts.json", own)
        self.csv(
            "own_casts.csv",
            sorted(own, key=lambda c: c["timestamp"]),
            ["fid", "hash", "timestamp", "published", "text"],
        )
        print(
            "Authored casts by source:",
            [len(cs) for cs in inventories],
            "; union:",
            len(own),
            flush=True,
        )
        print(
            "Planned deletion: all",
            len(own),
            "authored casts; none retained.",
            flush=True,
        )
        followers, xhandles = self.connections("followers")
        following, following_xhandles = self.connections("following")
        contacts = self.export_contacts()
        reactions, quotes = self.reactions(own)
        roots, thread_count = self.threads(own)
        try:
            signed_messages = a.wire.archive(self)
        except Exception as exc:
            self.issue("wire-archive-failed", error=type(exc).__name__, detail=str(exc))
            partial = getattr(a.wire, "index", {})
            write(
                self.path / "wire_index.json",
                dict(
                    source=a.wire.source,
                    schema_revision=CLI_REV,
                    messages=partial,
                    verified=len(partial),
                    complete=False,
                ),
            )
            signed_messages = len(partial)
        media = self.media()
        all_casts = sorted(
            self.casts.values(), key=lambda c: (c["timestamp"], c["hash"])
        )
        active_own = {c["hash"] for c in own}
        for c in all_casts:
            if c["fid"] == FID and c["hash"] not in active_own:
                self.issue(
                    "additional-authored-cast-outside-inventory",
                    hash=c["hash"],
                    protocol_available=c["protocol"],
                )
        write(self.path / "casts.json", all_casts)
        self.csv(
            "casts.csv",
            all_casts,
            [
                "fid",
                "hash",
                "timestamp",
                "unix_timestamp",
                "published",
                "text",
                "parent",
            ],
        )
        self.csv(
            "own_casts.csv",
            sorted(own, key=lambda c: c["timestamp"]),
            ["fid", "hash", "timestamp", "published", "text"],
        )
        self.render_casts(all_casts, self.media_records)
        # Re-read all three inventories live; cached pagination alone cannot establish a stable snapshot.
        final = a.inventory()
        for old, new in zip(inventories, final):
            if {c["hash"] for c in old} != {c["hash"] for c in new}:
                self.issue("authored-inventory-changed-during-backup")
        write(self.path / "issues.json", self.issues)
        summary = dict(
            fid=FID,
            authored=len(own),
            source_counts=[len(cs) for cs in inventories],
            protected=deletion["protected"],
            planned_deletions=len(deletion["deletes"]),
            followers=followers,
            verified_x_accounts=xhandles,
            following=following,
            following_verified_x_accounts=following_xhandles,
            contacts=contacts,
            reaction_targets=reactions,
            quoted_targets=quotes,
            thread_roots=roots,
            thread_casts=thread_count,
            all_casts=len(all_casts),
            media_downloaded=media,
            authored_embed_urls=sum(
                bool(record["authored_casts"]) for record in self.media_records
            ),
            authored_media_downloaded=sum(
                bool(record["authored_casts"]) and record["status"] == "downloaded"
                for record in self.media_records
            ),
            authored_embed_unresolved=sum(
                bool(record["authored_casts"]) and record["status"] == "unresolved"
                for record in self.media_records
            ),
            verified_original_protocol_messages=signed_messages,
            issue_count=len(self.issues),
            complete=not self.issues,
            finished_at=now(),
            coverage="All pages returned by the recorded sources. Deleted/pruned history may be unavailable. X identities are provider assertions.",
        )
        write(self.path / "summary.json", summary)
        files = {
            str(p.relative_to(self.path)): sha(p)
            for p in sorted(self.path.rglob("*"))
            if p.is_file() and ".partial" not in p.name and p.name != "manifest.json"
        }
        write(
            self.path / "manifest.json",
            dict(schema=1, files=files, complete=not self.issues, sealed_at=now()),
        )
        return summary


def verify_snapshot(path, require_complete=False):
    manifest = read(path / "manifest.json")
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), dict):
        fail("Invalid snapshot manifest")
    required = {
        "summary.json",
        "deletion_plan.json",
        "casts.json",
        "casts.csv",
        "casts.md",
        "own_casts.json",
        "own_casts.csv",
        "followers.json",
        "followers.csv",
        "followers.md",
        "followers_verified_x.csv",
        "reactions.json",
        "threads.json",
        "media.json",
        "issues.json",
        "custody.json",
        "nodes.json",
        "own_inventory.json",
        "wire_index.json",
        "reaction_history.json",
    }
    if not required <= manifest["files"].keys():
        fail("Incomplete snapshot manifest")
    file_count = len(manifest["files"])
    print(
        f"Checking sealed backup: {file_count:,} files. Input prompts follow these checks.",
        flush=True,
    )
    for ix, (name, digest) in enumerate(manifest["files"].items(), 1):
        f = path / name
        if (
            not f.resolve().is_relative_to(path.resolve())
            or f.is_symlink()
            or not f.is_file()
            or sha(f) != digest
        ):
            fail("Snapshot file missing, modified, or unsafe: " + name)
        if ix % 10000 == 0 or ix == file_count:
            print(f"Backup files verified: {ix:,} / {file_count:,}", flush=True)
    print(
        "File checks passed. Checking cast records, deletion plan, and media references.",
        flush=True,
    )
    summary = read(path / "summary.json")
    custody = read(path / "custody.json")
    if summary.get("fid") != FID or custody.get("fid") != FID:
        fail("Snapshot belongs to a different FID")
    if (
        not re.fullmatch(r"0x[0-9a-fA-F]{40}", custody.get("custody", ""))
        or int(custody["custody"], 16) == 0
    ):
        fail("Snapshot custody address is invalid")
    original = read(path / "deletion_plan.json")
    inventories = read(path / "own_inventory.json")
    if (
        not isinstance(inventories, list)
        or len(inventories) != 3
        or any(not isinstance(v, list) for v in inventories)
    ):
        fail("Invalid authored source inventories")
    active = {cast_hash(h) for group in inventories for h in group}
    archived_casts = {c["hash"]: c for c in read(path / "casts.json")}
    if not active <= archived_casts.keys():
        fail("Surviving authored inventory is not fully archived")
    own = [archived_casts[h] for h in active]
    recalculated = plan(own)
    # Sealed plans record past intent. Validate that record without enabling
    # retention in the active delete-all workflow.
    protected = original["protected"]
    if protected is not None:
        matching = [c for c in own if c["hash"] == protected["hash"]]
        if len(matching) != 1:
            fail("Archived protected cast is outside the surviving inventory")
        recalculated["protected"] = {
            k: matching[0][k]
            for k in (
                "fid",
                "hash",
                "timestamp",
                "unix_timestamp",
                "published",
                "text",
                "parent",
            )
        }
        recalculated["deletes"].remove(protected["hash"])
    if original != recalculated or summary["protected"] != protected:
        fail("Deletion plan differs from archived authored casts")
    personal = read(path / "own_casts.json")
    plan(personal)
    if {c["hash"] for c in personal} != active:
        fail("Personal authored archive differs from source inventories")
    casts = read(path / "casts.json")
    if (
        summary["authored"] != len(active)
        or len(active) != int(protected is not None) + len(original["deletes"])
        or summary["planned_deletions"] != len(original["deletes"])
        or summary["all_casts"] != len(casts)
    ):
        fail("Archive count mismatch")
    hashes = {c["hash"] for c in casts}
    threads = read(path / "threads.json")
    if not set(threads["hashes"]) <= hashes or summary["thread_casts"] != len(
        set(threads["hashes"])
    ):
        fail("Thread references missing archived casts")
    issues = read(path / "issues.json")
    if summary["complete"] and issues:
        fail("Complete snapshot has unresolved archival checks")
    if summary["issue_count"] != len(issues):
        fail("Archive gap count mismatch")
    for media in read(path / "media.json"):
        if media["status"] == "downloaded":
            name = media["path"]
            if (
                name not in manifest["files"]
                or manifest["files"][name] != media["sha256"]
                or (path / name).stat().st_size != media["bytes"]
            ):
                fail("Downloaded media missing or changed")
    for media in read(path / "media.json"):
        for resource in media.get("resources", []):
            name = resource["path"]
            if (
                name not in manifest["files"]
                or manifest["files"][name] != resource["sha256"]
                or (path / name).stat().st_size != resource["bytes"]
            ):
                fail("HLS resource missing or changed")
    history = read(path / "reaction_history.json")
    if summary["complete"] and not history["complete"]:
        fail("Complete snapshot has incomplete reaction history")
    for record in history["messages"]:
        name = record["path"]
        if (
            name not in manifest["files"]
            or manifest["files"][name] != record["sha256"]
            or not record["verified_hash_and_signature"]
        ):
            fail("Original historical reaction message missing or changed")
    wire = read(path / "wire_index.json")
    if manifest["complete"] != summary["complete"]:
        fail("Snapshot completion flags disagree")
    for record in wire["messages"].values():
        name = record["path"]
        if (
            name not in manifest["files"]
            or manifest["files"][name] != record["sha256"]
            or not record["verified_hash_and_signature"]
        ):
            fail("Original protocol message missing or changed")
    if require_complete and (
        not manifest["complete"]
        or not summary["complete"]
        or read(path / "issues.json")
    ):
        fail(
            "Backup has unresolved archival checks; broadcast blocked. See issues.json"
        )
    print("Backup integrity passed. Recorded content gaps:", len(issues), flush=True)
    return summary


def prepare_working(root, previous=None):
    """Reuse verified partial caches while keeping the sealed snapshot intact."""
    pending = root / "working"
    if pending.exists():
        return pending
    if previous is None or read(previous / "summary.json")["complete"]:
        pending.mkdir()
        return pending
    staging = root / ("working-prepare-" + os.urandom(6).hex())
    staging.mkdir()
    try:
        for name in read(previous / "manifest.json")["files"]:
            if Path(name).parts[0] not in ("raw", "wire", "media"):
                continue
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Cache writers use atomic replacement, so later writes cannot alter
            # the sealed file behind this hardlink. Never link partial files.
            os.link(previous / name, destination)
        write(
            staging / "resume_source.json",
            dict(
                path=str(previous.relative_to(root)),
                manifest_sha256=sha(previous / "manifest.json"),
                resumed_at=now(),
            ),
        )
        os.rename(staging, pending)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return pending


def snapshot_path(root, pointer):
    record = read(pointer)
    path = (root / record["path"]).resolve()
    if not path.is_relative_to((root / "snapshots").resolve()) or path.is_symlink():
        fail("Invalid snapshot pointer")
    if sha(path / "manifest.json") != record["manifest_sha256"]:
        fail("Snapshot manifest changed since sealing")
    return path


def confirmed_signer(api, pub):
    states = []
    for base in (api.node, api.second):
        records = []
        token = None
        seen = set()
        nonce = None
        for _ in range(20000):
            params = dict(fid=FID, pageSize=1000)
            if token:
                params["pageToken"] = token
            obj = api.request(base, "/v1/signersByFid", params)
            if not isinstance(obj.get("currentUserNonce"), int) or not isinstance(
                obj.get("signers"), list
            ):
                fail("Malformed signer state")
            if nonce is not None and nonce != obj["currentUserNonce"]:
                fail("Signer nonce changed during pagination")
            nonce = obj["currentUserNonce"]
            records.extend(obj["signers"])
            token = obj.get("nextPageToken")
            if not token:
                break
            if token in seen:
                fail("Repeated signer pagination token")
            seen.add(token)
        else:
            fail("Signer pagination limit")
        match = [x for x in records if x.get("key", "").lower() == pub]
        for s in match:
            if (
                s.get("fid") != FID
                or s.get("keyType") != 1
                or s.get("source") != "SIGNER_SOURCE_OFFCHAIN"
                or s.get("scopes") != ["MESSAGE_TYPE_CAST_REMOVE"]
                or s.get("expiresAt", 0) <= int(time.time()) + 300
            ):
                fail("Signer author, source, scope, or expiry is not acceptable")
        states.append((bool(match), nonce))
    return states


def capture_cli(fc, args, env, destination):
    # The official CLI signs into this loopback sink. It cannot submit to mainnet.
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/v1/submitMessage" or result:
                self.send_error(400)
                return
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > 1024 * 1024:
                self.send_error(400)
                return
            result.append(self.rfile.read(n))
            write(destination, result[0])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = HTTPServer(("127.0.0.1", 0), Sink)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        run = subprocess.run(
            [
                str(fc),
                "--network",
                "mainnet",
                "--node",
                "http://127.0.0.1:" + str(server.server_port),
                *args,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if run.returncode or len(result) != 1:
            fail(
                "Official CLI failed during local message preparation; output withheld to protect secrets"
            )
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    return destination


def submit_once(api, body_file, journal):
    # Intent is durable before the only network submission. An uncertain result is never retried.
    if journal.exists():
        fail(
            "Submission already attempted; verify propagation. Automatic resubmission is prohibited: "
            + str(journal)
        )
    body = body_file.read_bytes()
    write(
        journal, dict(status="attempted", body_sha256=sha(body_file), started_at=now())
    )
    req = urllib.request.Request(
        api.node + "/v1/submitMessage",
        data=body,
        headers={"Content-Type": "application/octet-stream"},
    )

    # No redirect handler: a 307 must never cause a second protocol submission.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=45) as r:
            data = r.read(65536)
            status = r.status
    except urllib.error.HTTPError as e:
        status, data = e.code, e.read(65536)
        e.close()
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        fail("Submission outcome uncertain. No automatic retry; verify both nodes")
    write(
        journal,
        dict(
            status="responded",
            http_status=status,
            response=data.decode(errors="replace"),
            body_sha256=sha(body_file),
            finished_at=now(),
        ),
    )
    # Even HTTP success is only receipt. The caller must verify committed state on both nodes.


def await_state(predicate, label):
    for _ in range(30):
        if predicate():
            return
        time.sleep(4)
    fail("Propagation not verified: " + label + ". No automatic resubmission.")


def verify_broadcast_backup(root, snapshot):
    summary = verify_snapshot(snapshot)
    if not read(snapshot / "reaction_history.json")["complete"]:
        fail("Incomplete reaction history; broadcast blocked")
    if summary["complete"]:
        return summary
    issues = read(snapshot / "issues.json")
    reviewable = {
        "unresolved-embed",
        "unavailable-cast",
        "missing-parent",
        "neynar-conversation-unavailable",
    }
    if not issues or any(i.get("kind") not in reviewable for i in issues):
        fail("Critical or unclassified archival checks failed; broadcast blocked")
    expected = dict(
        schema=1,
        fid=FID,
        decision="accept-recorded-content-gaps",
        manifest_sha256=sha(snapshot / "manifest.json"),
        issues_sha256=sha(snapshot / "issues.json"),
        issue_count=len(issues),
    )
    review = root / "reviewed-backup.json"
    if not review.exists() or read(review) != expected:
        fail(
            "Backup has unresolved archival checks; explicit review of this exact snapshot is required"
        )
    return summary


def confirm_deletion(count):
    confirmation = f"DELETE ALL {count} CASTS FOR FID {FID}"
    with open("/dev/tty", "w") as tty:
        tty.write(
            "\nSTEP 1 OF 2: Confirm deletion. Do not enter recovery words at this step.\n"
        )
        tty.write("Type " + confirmation + " to authorize mainnet deletion: ")
        tty.flush()
    with open("/dev/tty", "r") as tty:
        if tty.readline().strip() != confirmation:
            fail(
                "Deletion not confirmed. Enter the exact DELETE ALL line at step 1; recovery words belong only at the hidden step 2 prompt."
            )


def broadcast(api, root, snapshot):
    verify_broadcast_backup(root, snapshot)
    deletion = read(snapshot / "deletion_plan.json")
    if deletion["protected"] is not None:
        fail(
            "Archived plan retains a cast; rebuild the delete-all plan before broadcast"
        )
    if not deletion["deletes"]:
        fail("No surviving authored casts to delete")
    custody = read(snapshot / "custody.json")["custody"]
    state = root / "state" / f"fid{FID}"
    state.mkdir(parents=True, exist_ok=True)
    print("Checking live custody on Optimism.", flush=True)
    api.custody(custody)
    print("Checking independent node identities.", flush=True)
    api.nodes()
    print("Fetching current cast inventories from all three sources.", flush=True)
    inventories = api.inventory()
    print("Live cast counts:", [len(cs) for cs in inventories], flush=True)
    archived = set(deletion["deletes"])
    for cs in inventories:
        live = {c["hash"] for c in cs}
        if live - archived:
            fail("Unarchived casts appeared; new backup required")
    receipt_path = root / "cli-build.json"
    receipt = read(receipt_path) if receipt_path.exists() else {}
    fc = Path(os.environ.get("FC_BIN") or receipt.get("binary", ""))
    if not fc.is_file() or not os.access(fc, os.X_OK) or not receipt_path.exists():
        fail(
            "FC_BIN and verified cli-build.json required; build pinned official source before broadcast"
        )
    receipt = read(receipt_path)
    if receipt.get("revision") != CLI_REV or receipt.get("sha256") != sha(fc):
        fail("Official CLI build receipt does not match binary/source revision")
    print("Delete all archived authored casts:", len(deletion["deletes"]), flush=True)
    print("Snapshot manifest SHA256:", sha(snapshot / "manifest.json"), flush=True)
    confirm_deletion(len(deletion["deletes"]))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PrivateFormat,
        PublicFormat,
        NoEncryption,
    )

    secret_file = root / "secrets" / "signer.json"
    if secret_file.exists():
        if secret_file.stat().st_mode & 0o077:
            fail("Signer secret file permissions must be 600")
        saved = read(secret_file)
        secret = bytes.fromhex(saved["secret"])
        signing = Ed25519PrivateKey.from_private_bytes(secret)
        pub = (
            "0x"
            + signing.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        )
        if pub != saved["pub"]:
            fail("Signer keypair mismatch")
    else:
        signing = Ed25519PrivateKey.generate()
        secret = signing.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        pub = (
            "0x"
            + signing.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        )
        write(secret_file, dict(secret=secret.hex(), pub=pub))
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MNEMONIC", "SIGNER_SECRET", "NEYNAR_API_KEY", "X_USER_TOKEN")
    }
    env["SIGNER_SECRET"] = "0x" + secret.hex()
    signers = confirmed_signer(api, pub)
    if not all(s[0] for s in signers):
        if signers[0][1] != signers[1][1] or signers[0][1] >= 0xFFFFFFFF:
            fail("Signer nonce disagreement or exhaustion")
        intent = state / ("key-add-" + str(signers[0][1] + 1) + ".json")
        if intent.exists() or any(s[0] for s in signers):
            await_state(
                lambda: all(s[0] for s in confirmed_signer(api, pub)),
                "signer on both nodes",
            )
        else:
            phrase = getpass.getpass(
                "STEP 2 OF 2: Recovery words (local hidden input; never saved): "
            )
            from eth_account import Account

            Account.enable_unaudited_hdwallet_features()
            try:
                derived = Account.from_mnemonic(phrase, account_path="m/44'/60'/0'/0/0")
            except Exception:
                fail(
                    "Recovery phrase derivation failed. Input and library details withheld."
                )
            if derived.address.lower() != custody.lower():
                fail(
                    "Phrase-derived address does not match current custody "
                    + custody
                    + ". No signing or submission occurred in this attempt."
                )
            env["MNEMONIC"] = phrase
            payload = capture_cli(
                fc,
                [
                    "key-add",
                    "--fid",
                    str(FID),
                    "--nonce",
                    str(signers[0][1] + 1),
                    "--scopes",
                    "cast-remove",
                    "--ttl",
                    "86400",
                    "--path",
                    "m/44'/60'/0'/0/0",
                    "--yes",
                ],
                env,
                intent.with_suffix(".bin"),
            )
            del phrase, derived
            env.pop("MNEMONIC")
            api.custody(custody)
            submit_once(api, payload, intent)
            await_state(
                lambda: all(s[0] for s in confirmed_signer(api, pub)),
                "signer on both nodes",
            )
    for ix, h in enumerate(deletion["deletes"]):
        if not HASH.fullmatch(h):
            fail("Invalid deletion target")
        journal = state / (h + ".json")
        if journal.exists():
            await_state(
                lambda: all(
                    not api.present(base, h) for base in (api.node, api.second)
                ),
                h,
            )
            continue
        if not all(api.present(base, h) for base in (api.node, api.second)):
            fail(
                "Target was not present on both nodes before deletion; cannot prove propagation: "
                + h
            )
        if not all(s[0] for s in confirmed_signer(api, pub)):
            fail("Signer no longer active")
        api.custody(custody)
        payload = capture_cli(
            fc,
            ["cast-remove", "--fid", str(FID), "--target-hash", h],
            env,
            state / (h + ".bin"),
        )
        submit_once(api, payload, journal)
        await_state(
            lambda: all(not api.present(base, h) for base in (api.node, api.second)), h
        )
        write(
            state / (h + ".verified.json"),
            dict(hash=h, verified_at=now(), nodes=[api.node, api.second]),
        )
        print(
            "Verified deletion on both nodes:",
            ix + 1,
            "/",
            len(deletion["deletes"]),
            h,
            flush=True,
        )
        # Every target gets the same independent check, including the first canary.
    final = api.inventory()
    if any(cs for cs in final):
        fail("Final independent inventories still contain authored casts")
    write(
        state / "complete.json",
        dict(
            deleted_count=len(deletion["deletes"]),
            snapshot_manifest_sha256=sha(snapshot / "manifest.json"),
            verified_at=now(),
            source_counts=[len(cs) for cs in final],
        ),
    )
    print("Verified final state: zero authored casts on all three sources.")


def bind_archive(root):
    """Bind caches and journals to one account before any network request."""
    identity = root / "account.json"
    expected = dict(schema=1, fid=FID)
    if identity.exists():
        if read(identity) != expected:
            fail("OUT belongs to a different FID; use a separate archive directory")
    else:
        if any(p.name != ".lock" for p in root.iterdir()):
            fail(
                "OUT has no account identity and is not empty; choose an empty directory"
            )
        write(identity, expected)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in (
        "--dry-run",
        "--broadcast",
        "--verify-backup",
    ):
        fail(
            "Usage: farcaster_cleanup_all.sh --dry-run | --broadcast | --verify-backup"
        )
    os.umask(0o077)
    if not 0 < FID <= 2**63 - 1:
        fail("Set FID to a positive integer no greater than 2**63-1")
    root = (
        Path(os.environ.get("OUT", f"farcaster-archive/fid{FID}"))
        .expanduser()
        .resolve()
    )
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / ".lock").open("a+")
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Another cleanup process owns the archive lock")
        bind_archive(root)
        mode = sys.argv[1]
        pointer = root / "snapshot.json"
        if mode == "--verify-backup":
            snapshot = snapshot_path(root, pointer)
            summary = verify_snapshot(snapshot)
        else:
            with Client(root) as api:
                if mode == "--broadcast":
                    if not pointer.exists():
                        fail(
                            "Run --dry-run first. Broadcast never creates or replaces a backup"
                        )
                    broadcast(api, root, snapshot_path(root, pointer))
                    return
                # Read-only mode never enters signer or protocol submission code.
                api.custody()
                api.nodes()
                old = None
                if pointer.exists():
                    old = snapshot_path(root, pointer)
                    verify_snapshot(old)
                pending = prepare_working(root, old)
                try:
                    summary = Archive(api, pending).run()
                except Exception as e:
                    write(
                        root / "last-failure.json",
                        dict(
                            stage="archive",
                            error=type(e).__name__,
                            message=str(e),
                            at=now(),
                        ),
                    )
                    raise
                destination = (
                    root
                    / "snapshots"
                    / (
                        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
                        + "-"
                        + os.urandom(3).hex()
                    )
                )
                destination.parent.mkdir(exist_ok=True)
                os.rename(pending, destination)
                verify_snapshot(destination)
                write(
                    pointer,
                    dict(
                        path=str(destination.relative_to(root)),
                        manifest_sha256=sha(destination / "manifest.json"),
                    ),
                )
                snapshot = destination
        print(
            json.dumps(
                {k: v for k, v in summary.items() if k != "protected"}, indent=2
            ),
            flush=True,
        )
        print("Snapshot:", snapshot, flush=True)
        print("Retained cast:", summary["protected"], flush=True)
        if not summary["complete"]:
            fail("Partial backup preserved. Broadcast blocked; inspect issues.json")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "Interrupted. Backup and submission journals remain for safe resumption.",
            file=sys.stderr,
        )
        sys.exit(130)
    except Exception as exc:
        print("ERROR:", str(exc), file=sys.stderr)
        sys.exit(1)
FARCASTER_PYTHON
