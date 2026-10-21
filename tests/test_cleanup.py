import base64
import json
import os
import types
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]

source = (
    (BASE / "farcaster_cleanup_all.sh")
    .read_text()
    .split("<<'FARCASTER_PYTHON'\n", 1)[1]
    .rsplit("\nFARCASTER_PYTHON", 1)[0]
)
c = types.ModuleType("cleanup")
with unittest.mock.patch.dict(os.environ, {"FID": "12345"}):
    exec(compile(source, "embedded_cleanup.py", "exec"), c.__dict__)


def h(n):
    return "0x" + format(n, "040x")


def hydrated(n, fid=c.FID, timestamp=100, parent=None):
    return dict(
        hash=h(n),
        author={"fid": fid},
        timestamp=c.dt.datetime.fromtimestamp(
            c.EPOCH + timestamp, c.dt.timezone.utc
        ).isoformat(),
        text="cast " + str(n),
        parent_hash=h(parent[1]) if parent else None,
        parent_author={"fid": parent[0]} if parent else {},
        embeds=[],
    )


def protocol(n, fid=c.FID, timestamp=100, parent=None):
    body = dict(text="cast " + str(n), embeds=[], mentions=[], mentionsPositions=[])
    if parent:
        body["parentCastId"] = {"fid": parent[0], "hash": h(parent[1])}
    return dict(
        hash=h(n),
        data=dict(
            type="MESSAGE_TYPE_CAST_ADD",
            fid=fid,
            timestamp=timestamp,
            network="FARCASTER_NETWORK_MAINNET",
            castAddBody=body,
        ),
    )


class Fake:
    node = "https://one.example"
    second = "https://two.example"
    neynar = "https://api.neynar.com"

    def __init__(self):
        import concurrent.futures

        self.pool = concurrent.futures.ThreadPoolExecutor(6)
        self.visited = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.pool.shutdown(wait=True, cancel_futures=True)

    def pages(self, base, path, params, field, cache=None):
        self.visited.append((base, path, params))
        n = int(params["hash"], 16)
        if n == 1:
            yield [protocol(2, timestamp=101, parent=(c.FID, 1))]
        else:
            yield []

    def request(self, base, path="", params=None, cache=None, data=None):
        if path.endswith("/conversation/"):
            n = int(params["identifier"], 16)
            values = {
                1: hydrated(1),
                2: hydrated(2, timestamp=101, parent=(c.FID, 1)),
                3: hydrated(3, fid=99, timestamp=102, parent=(c.FID, 2)),
                4: hydrated(4, timestamp=103, parent=(99, 3)),
            }
            obj = values[n].copy()
            obj["direct_replies"] = (
                [values[3]] if n == 2 else ([values[4]] if n == 3 else [])
            )
            return {"conversation": {"cast": obj}, "next": {"cursor": None}}
        raise AssertionError(path)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FC_BIN", None)
        self.tmp = tempfile.TemporaryDirectory(dir=BASE / "tests")
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delete_all_includes_latest_reply_and_timestamp_ties(self):
        own = [
            c.cast(protocol(1, timestamp=100), True),
            c.cast(protocol(2, timestamp=200, parent=(99, 8)), True),
            c.cast(protocol(3, timestamp=200), True),
        ]
        result = c.plan(own)
        self.assertIsNone(result["protected"])
        self.assertEqual(result["deletes"], [h(1), h(2), h(3)])
        self.assertEqual(own[1]["unix_timestamp"], c.EPOCH + 200)
        with self.assertRaisesRegex(RuntimeError, "wrong author"):
            c.plan([c.cast(protocol(1, fid=99), True)])
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            c.plan([own[0], own[0]])

    def test_historical_retention_plan_is_valid_evidence_but_cannot_broadcast(self):
        self.make_snapshot(self.path)
        own = c.read(self.path / "own_casts.json")
        deletion = c.read(self.path / "deletion_plan.json")
        deletion["protected"] = {
            k: own[1][k]
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
        deletion["deletes"].remove(h(2))
        summary = c.read(self.path / "summary.json")
        summary.update(protected=deletion["protected"], planned_deletions=1)
        c.write(self.path / "deletion_plan.json", deletion)
        c.write(self.path / "summary.json", summary)
        manifest = c.read(self.path / "manifest.json")
        manifest["files"] = {n: c.sha(self.path / n) for n in manifest["files"]}
        c.write(self.path / "manifest.json", manifest)
        self.assertTrue(c.verify_snapshot(self.path, True)["complete"])
        with self.assertRaisesRegex(RuntimeError, "retains a cast"):
            c.broadcast(types.SimpleNamespace(), self.path, self.path)

    def test_protocol_remove_is_archived_but_not_a_cast_target(self):
        api = object.__new__(c.Client)
        api.node, api.second, api.neynar = "one", "two", "three"
        import concurrent.futures

        api.pool = concurrent.futures.ThreadPoolExecutor(3)
        removed = dict(
            data=dict(
                type="MESSAGE_TYPE_CAST_REMOVE",
                fid=c.FID,
                network="FARCASTER_NETWORK_MAINNET",
                castRemoveBody={
                    "targetHash": base64.b64encode(bytes.fromhex(h(99)[2:])).decode()
                },
            )
        )
        api.all = (
            lambda base, *args, **kwargs: [protocol(1), removed]
            if base != "three"
            else [hydrated(1)]
        )
        try:
            values = api.inventory()
            self.assertEqual([[x["hash"] for x in xs] for xs in values], [[h(1)]] * 3)
        finally:
            api.pool.shutdown()

    def test_rendered_mentions_are_preserved_without_false_conflict(self):
        archive = c.Archive(Fake(), self.path)
        a, b = protocol(1), hydrated(1)
        a["data"]["castAddBody"]["text"] = " hello"
        b["text"] = "@name hello"
        archive.add(c.cast(a, True), "protocol")
        archive.add(c.cast(b), "index")
        self.assertEqual(archive.casts[h(1)]["sources"]["index"]["text"], "@name hello")
        a["data"]["castAddBody"]["text"] = "different"
        with self.assertRaisesRegex(RuntimeError, "Protocol nodes disagree"):
            archive.add(c.cast(a, True), "second")

    def test_pagination_keeps_empty_intermediate_pages_and_checks_completion(self):
        api = object.__new__(c.Client)
        api.neynar = "n"
        seq = iter(
            [
                {"casts": [], "next": {"cursor": "opaque+/="}},
                {"casts": [1], "next": {"cursor": None}},
            ]
        )
        api.request = lambda *args: next(seq)
        self.assertEqual(api.all("n", "/casts", {}, "casts"), [1])
        api.request = lambda *args: {"casts": []}
        with self.assertRaisesRegex(RuntimeError, "completion"):
            api.all("n", "/casts", {}, "casts")
        api.request = lambda *args: {"casts": [], "next": {"cursor": "same"}}
        with self.assertRaisesRegex(RuntimeError, "repeated"):
            api.all("n", "/casts", {}, "casts")

    def test_thread_walk_includes_index_only_nested_replies(self):
        api = Fake()
        archive = c.Archive(api, self.path)
        archive.add(c.cast(protocol(1), True), api.node)
        roots, count = archive.threads(list(archive.casts.values()))
        self.assertEqual((roots, count), (1, 4))
        self.assertEqual(set(archive.casts), {h(i) for i in (1, 2, 3, 4)})
        self.assertEqual(len(api.visited), 4)
        api.pool.shutdown()

    def test_missing_response_is_distinct_from_transport_or_auth_failure(self):
        body = json.dumps(
            {
                "error": "Failed to get cast",
                "error_detail": 'status: NotFound, message: "cast not found", details: []',
            }
        )
        self.assertTrue(c.APIError(400, body).missing())
        self.assertFalse(c.APIError(401, body).missing())
        self.assertFalse(c.APIError(404, "<html>proxy not found</html>").missing())
        self.assertFalse(c.APIError(500, body).missing())

    def test_submit_once_does_not_retry_server_error_or_resume(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                seen.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"upstream failed")

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            api = Fake()
            api.node = "http://127.0.0.1:" + str(server.server_port)
            payload, journal = self.path / "signed.bin", self.path / "intent.json"
            c.write(payload, b"signed-fixture")
            c.submit_once(api, payload, journal)
            self.assertEqual(seen, [b"signed-fixture"])
            self.assertEqual(c.read(journal)["http_status"], 500)
            with self.assertRaisesRegex(RuntimeError, "already attempted"):
                c.submit_once(api, payload, journal)
            self.assertEqual(len(seen), 1)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_atomic_writes_preserve_original_until_replace(self):
        f = self.path / "file.json"
        c.write(f, {"original": True})
        with patch.object(c.os, "replace", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                c.write(f, {"original": False})
        self.assertEqual(c.read(f), {"original": True})
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_deleted_parent_does_not_hide_obtainable_root_and_siblings(self):
        api = Fake()
        archive = c.Archive(api, self.path)
        own = hydrated(2, timestamp=101, parent=(99, 999))
        own["thread_hash"] = h(1)
        archive.add(c.cast(own), api.neynar)
        root = c.cast(hydrated(1))
        root["sources"] = {api.neynar: root["raw"]}

        def lookup(fid, value):
            if value == h(999):
                return None
            if value == h(1):
                return archive.add(root, api.neynar)
            return archive.casts.get(value)

        # Preserve the real below-gap parent when fixtures enumerate that cast.
        original = api.request

        def request(base, path="", params=None, cache=None, data=None):
            obj = original(base, path, params, cache, data)
            if path.endswith("/conversation/") and params["identifier"] == h(2):
                obj["conversation"]["cast"].update(
                    parent_hash=h(999), parent_author={"fid": 99}
                )
            return obj

        api.request = request
        # Root has no direct protocol child in this deleted-parent fixture.
        api.pages = lambda *args, **kwargs: iter([[]])
        with patch.object(archive, "lookup", side_effect=lookup):
            roots, count = archive.threads(list(archive.casts.values()))
        self.assertEqual(roots, 1)
        self.assertEqual(count, 4)
        self.assertIn("missing-parent", {x["kind"] for x in archive.issues})
        api.pool.shutdown()

    def test_signer_scope_expiry_and_nonce_pagination_fail_closed(self):
        from types import SimpleNamespace

        pub = "0x" + "ab" * 32
        signer = dict(
            key=pub,
            fid=c.FID,
            keyType=1,
            source="SIGNER_SOURCE_OFFCHAIN",
            scopes=["MESSAGE_TYPE_CAST_REMOVE"],
            expiresAt=int(c.time.time()) + 10000,
        )
        api = SimpleNamespace(node="one", second="two")
        api.request = lambda *args: dict(signers=[signer], currentUserNonce=42)
        self.assertEqual(c.confirmed_signer(api, pub), [(True, 42), (True, 42)])
        signer["scopes"] = ["MESSAGE_TYPE_CAST_ADD"]
        with self.assertRaisesRegex(RuntimeError, "scope"):
            c.confirmed_signer(api, pub)
        signer["scopes"] = ["MESSAGE_TYPE_CAST_REMOVE"]
        signer["expiresAt"] = int(c.time.time()) - c.EPOCH + 10000
        with self.assertRaisesRegex(RuntimeError, "expiry"):
            c.confirmed_signer(api, pub)
        pages = iter(
            [
                dict(signers=[], currentUserNonce=42, nextPageToken="next"),
                dict(signers=[], currentUserNonce=43),
            ]
        )
        api.request = lambda *args: next(pages)
        with self.assertRaisesRegex(RuntimeError, "nonce changed"):
            c.confirmed_signer(api, pub)

    def test_failed_canary_and_resume_never_submit_the_second_target(self):
        from types import SimpleNamespace

        root = self.path
        snapshot = root / "snapshots" / "test"
        self.make_snapshot(snapshot)
        own = [c.cast(protocol(n, timestamp=n), True) for n in (1, 2, 3)]
        deletion = c.plan(own)
        summary = dict(
            fid=c.FID,
            authored=3,
            planned_deletions=3,
            all_casts=3,
            thread_casts=3,
            issue_count=0,
            complete=True,
            protected=deletion["protected"],
        )
        for name, obj in [
            ("casts.json", own),
            ("own_casts.json", own),
            ("deletion_plan.json", deletion),
            ("summary.json", summary),
            ("threads.json", {"hashes": [h(1), h(2), h(3)]}),
            ("own_inventory.json", [[h(1), h(2), h(3)]] * 3),
        ]:
            c.write(snapshot / name, obj)
        name = "wire/messages/" + h(3) + ".pb"
        c.write(snapshot / name, b"fixture-third-original-bytes")
        wire = c.read(snapshot / "wire_index.json")
        wire["messages"][h(3)] = dict(
            path=name, sha256=c.sha(snapshot / name), verified_hash_and_signature=True
        )
        c.write(snapshot / "wire_index.json", wire)
        manifest = c.read(snapshot / "manifest.json")
        manifest["files"][name] = c.sha(snapshot / name)
        manifest["files"] = {name: c.sha(snapshot / name) for name in manifest["files"]}
        c.write(snapshot / "manifest.json", manifest)
        fc = Path(c.sys.executable)
        c.write(
            root / "cli-build.json",
            dict(revision=c.CLI_REV, binary=str(fc), sha256=c.sha(fc)),
        )
        api = SimpleNamespace(
            node="one",
            second="two",
            custody=lambda expected_address=None: {},
            nodes=lambda: {},
            inventory=lambda: [own] * 3,
            present=lambda *args: True,
        )

        class Tty:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def write(self, value):
                pass

            def flush(self):
                pass

            def readline(self):
                return "DELETE ALL 3 CASTS FOR FID 12345"

        def attempted(api, payload, journal):
            c.write(journal, {"status": "attempted"})

        with (
            patch.object(c, "open", return_value=Tty(), create=True),
            patch.object(c, "confirmed_signer", return_value=[(True, 42)] * 2),
            patch.object(
                c, "capture_cli", return_value=root / "fixture.bin"
            ) as capture,
            patch.object(c, "submit_once", side_effect=attempted) as submit,
            patch.object(
                c, "await_state", side_effect=RuntimeError("canary did not propagate")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "canary"):
                c.broadcast(api, root, snapshot)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(capture.call_args.args[1][-1], h(1))
            capture.reset_mock()
            submit.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "canary"):
                c.broadcast(api, root, snapshot)
            submit.assert_not_called()
            capture.assert_not_called()

    def test_original_byte_verifier_handles_noncanonical_protobuf(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from cryptography.exceptions import InvalidSignature

        wire = c.WireReader("unused-local-test-key")
        try:
            message = wire.types["Message"]()
            message.data.type = 2
            message.data.fid = c.FID
            message.data.timestamp = 100
            message.data.network = 1
            message.data.cast_remove_body.target_hash = bytes.fromhex(h(1)[2:])
            # A repeated identical scalar field is valid protobuf, but reserialization
            # removes the duplicate. Hash verification must retain the original bytes.
            message.data_bytes = message.data.SerializeToString() + b"\x08\x02"
            message.hash = wire.blake3.blake3(message.data_bytes).digest()[:20]
            key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
            message.signer = key.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            )
            message.signature = key.sign(message.hash)
            message.hash_scheme = 1
            message.signature_scheme = 1
            self.assertNotEqual(
                message.hash,
                wire.blake3.blake3(message.data.SerializeToString()).digest()[:20],
            )
            self.assertEqual(wire.validate(message, c.FID).fid, c.FID)
            message.data.timestamp += 1
            with self.assertRaisesRegex(RuntimeError, "differ from decoded"):
                wire.validate(message, c.FID)
            message.data.timestamp -= 1
            message.signature = bytes(64)
            with self.assertRaises(InvalidSignature):
                wire.validate(message, c.FID)
            with self.assertRaisesRegex(RuntimeError, "Non-read"):
                wire.call("SubmitMessage", None, self.path)
        finally:
            wire.channel.close()

    def test_wire_archive_keeps_cached_originals_without_network_requests(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        wire = c.WireReader("unused-offline-test-key")
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        archive = c.Archive(types.SimpleNamespace(), self.path)
        own = []
        try:
            for n, fid in ((1, c.FID), (2, 99)):
                message = wire.types["Message"]()
                message.data.type = 1
                message.data.fid = fid
                message.data.timestamp = 100
                message.data.network = 1
                message.data.cast_add_body.text = "cast " + str(n)
                message.data_bytes = message.data.SerializeToString()
                message.hash = wire.blake3.blake3(message.data_bytes).digest()[:20]
                message.signature = key.sign(message.hash)
                message.signer = key.public_key().public_bytes(
                    Encoding.Raw, PublicFormat.Raw
                )
                message.hash_scheme = message.signature_scheme = 1
                raw = protocol(n, fid=fid)
                raw["hash"] = "0x" + message.hash.hex()
                record = archive.add(c.cast(raw, True), "protocol")
                own.append(record)
                c.write(
                    self.path / "wire/messages" / (raw["hash"] + ".pb"),
                    message.SerializeToString(),
                )
            # This additional context cast has JSON metadata and no cached original.
            archive.add(c.cast(protocol(3, fid=100), True), "protocol")

            with patch.object(
                wire, "call", side_effect=AssertionError("extra RPC read")
            ) as rpc:
                self.assertEqual(wire.archive(archive), 2)
            rpc.assert_not_called()
            index = c.read(self.path / "wire_index.json")
            self.assertEqual(index["collection"], "previously-obtained-originals")
            self.assertEqual(len(archive.casts), 3)
            self.assertEqual(archive.issues, [])
        finally:
            wire.channel.close()

    def test_backup_does_not_require_signed_protocol_bytes(self):
        self.make_snapshot(self.path)
        c.write(self.path / "wire_index.json", {"messages": {}})
        manifest = c.read(self.path / "manifest.json")
        manifest["files"]["wire_index.json"] = c.sha(self.path / "wire_index.json")
        c.write(self.path / "manifest.json", manifest)
        self.assertTrue(c.verify_snapshot(self.path, True)["complete"])

    def test_neynar_budget_is_shared_by_concurrent_http_and_grpc_reads(self):
        current, starts = [100.0], []

        def sleep(delay):
            current[0] += delay

        pacer = c.ReadPacer(clock=lambda: current[0], sleep=sleep)

        def read(endpoint):
            pacer.wait(endpoint)
            starts.append(current[0])

        with c.concurrent.futures.ThreadPoolExecutor(6) as pool:
            list(pool.map(read, ["/http", "/HubService/GetCast"] * 60))
        self.assertEqual(len(starts), 120)
        self.assertTrue(all(b - a >= 0.25 for a, b in zip(starts, starts[1:])))
        pacer.defer(90)
        before = current[0]
        pacer.wait("/http")
        self.assertGreaterEqual(current[0] - before, 90)
        pacer.wait("/v2/farcaster/cast/search")
        before = current[0]
        pacer.wait("/v2/farcaster/cast/search")
        self.assertGreaterEqual(current[0] - before, 1.0)

    def test_retry_after_seconds_and_http_date_are_not_shortened(self):
        self.assertEqual(c.retry_delay("90", 2), 90)
        with patch.object(c.time, "time", return_value=0):
            self.assertEqual(c.retry_delay("Thu, 01 Jan 1970 00:02:00 GMT", 2), 120)
        self.assertEqual(c.retry_delay("invalid", 4), 4)

    def test_network_deletion_preserves_own_backup_and_other_authors_context(self):
        root = self.path
        snapshot = root / "snapshots" / "test"
        self.make_snapshot(snapshot)
        original_casts = c.read(snapshot / "casts.json")
        context = c.cast(protocol(99, fid=99, timestamp=0), True)
        c.write(snapshot / "casts.json", [*original_casts, context])
        c.write(snapshot / "threads.json", {"hashes": [h(1), h(2), h(99)]})
        summary = c.read(snapshot / "summary.json")
        summary.update(all_casts=3, thread_casts=3)
        c.write(snapshot / "summary.json", summary)
        manifest = c.read(snapshot / "manifest.json")
        manifest["files"] = {name: c.sha(snapshot / name) for name in manifest["files"]}
        c.write(snapshot / "manifest.json", manifest)
        self.assertTrue(c.verify_snapshot(snapshot, True)["complete"])
        before = c.sha(snapshot / "manifest.json")
        remaining = {h(1), h(2)}
        api = types.SimpleNamespace(
            node="one",
            second="two",
            custody=lambda expected_address=None: {},
            nodes=lambda: {},
            inventory=lambda: [[x for x in original_casts if x["hash"] in remaining]]
            * 3,
            present=lambda base, value: value in remaining,
        )
        fc = Path(c.sys.executable)
        c.write(
            root / "cli-build.json",
            dict(revision=c.CLI_REV, binary=str(fc), sha256=c.sha(fc)),
        )

        def submitted(api, payload, journal):
            remaining.remove(journal.stem)
            c.write(journal, dict(status="attempted"))

        with (
            patch.object(
                c,
                "open",
                unittest.mock.mock_open(read_data="DELETE ALL 2 CASTS FOR FID 12345"),
                create=True,
            ),
            patch.object(c, "confirmed_signer", return_value=[(True, 42)] * 2),
            patch.object(
                c, "capture_cli", return_value=root / "fixture.bin"
            ) as capture,
            patch.object(c, "submit_once", side_effect=submitted) as submit,
        ):
            c.broadcast(api, root, snapshot)
        self.assertEqual(remaining, set())
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(
            [call.args[1] for call in capture.call_args_list],
            [["cast-remove", "--fid", "12345", "--target-hash", h(n)] for n in (1, 2)],
        )
        self.assertEqual(c.sha(snapshot / "manifest.json"), before)
        self.assertEqual(
            {x["hash"] for x in c.read(snapshot / "casts.json")}, {h(1), h(2), h(99)}
        )
        self.assertTrue(c.verify_snapshot(snapshot, True)["complete"])

    def test_grpc_denial_has_context_and_redacts_key_without_retry(self):
        wire = c.WireReader("private-offline-fixture")

        class Denied(wire.grpc.RpcError):
            def code(self):
                return wire.grpc.StatusCode.PERMISSION_DENIED

            def details(self):
                return "denied private-offline-fixture"

        stub = unittest.mock.Mock(side_effect=Denied())
        wire.channel.close()
        wire.channel = types.SimpleNamespace(unary_unary=lambda *a, **kw: stub)
        request = wire.types["CastId"](fid=99, hash=bytes.fromhex(h(1)[2:]))
        with self.assertRaisesRegex(
            RuntimeError, "GetCast fid=99 hash=" + h(1)
        ) as error:
            wire.call("GetCast", request, self.path)
        self.assertNotIn("private-offline-fixture", str(error.exception))
        self.assertIn("[REDACTED]", str(error.exception))
        self.assertEqual(stub.call_count, 1)

    def test_pooled_read_retries_cache_and_no_credential_redirect(self):
        import httpx

        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) < 3:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(200, json={"messages": []})

        api = object.__new__(c.Client)
        api.httpx = httpx
        api.neynar = "https://api.neynar.com"
        api.key = "private-fixture-key"
        api.pacer = c.ReadPacer(sleep=lambda delay: None)
        api.http = httpx.Client(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )
        try:
            with patch.object(c.time, "sleep"):
                self.assertEqual(
                    api.request(api.neynar, "/read", cache=self.path), {"messages": []}
                )
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1].headers.get("x-api-key"), api.key)
            api.request(api.neynar, "/read", cache=self.path)
            self.assertEqual(len(calls), 3)
            api.request("https://other.example", "/read")
            self.assertNotIn("x-api-key", calls[-1].headers)
        finally:
            api.http.close()
        redirects = []

        def redirect(request):
            redirects.append(request)
            return httpx.Response(
                302, headers={"Location": "https://other.example/capture"}
            )

        api.http = httpx.Client(
            transport=httpx.MockTransport(redirect), follow_redirects=False
        )
        try:
            with self.assertRaises(c.APIError):
                api.request(api.neynar, "/redirect")
            self.assertEqual(len(redirects), 1)
        finally:
            api.http.close()

    def test_media_reader_honors_throttle_and_does_not_repeat_nxdomain(self):
        from email.message import Message

        current = [0.0]
        reader = c.MediaReader(
            clock=lambda: current[0],
            sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
        )
        headers = Message()
        headers["Retry-After"] = "90"
        throttled = c.urllib.error.HTTPError(
            "https://image.example/a", 429, "throttled", headers, None
        )
        sentinel = object()
        with patch.object(c, "open_public", side_effect=[throttled, sentinel]) as fetch:
            self.assertIs(reader("https://image.example/a"), sentinel)
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(current[0], 90)
        self.assertEqual(reader.events[0]["retry_after_seconds"], 90)
        absent = c.socket.gaierror(c.socket.EAI_NONAME, "missing")
        with patch.object(c, "open_public", side_effect=absent) as fetch:
            for path in ("a", "b", "c"):
                with self.assertRaises(c.socket.gaierror):
                    reader("https://gone.example/" + path)
            self.assertEqual(fetch.call_count, 1)

    def test_slower_media_interval_applies_to_every_request_and_retry(self):
        current = [0.0]
        reader = c.MediaReader(
            clock=lambda: current[0],
            sleep=lambda delay: current.__setitem__(0, current[0] + delay),
            interval=10,
        )
        from email.message import Message

        headers = Message()
        headers["Retry-After"] = "1"
        throttled = c.urllib.error.HTTPError(
            "https://image.example/a", 429, "throttled", headers, None
        )
        sentinel = object()
        with patch.object(
            c, "open_public", side_effect=[throttled, sentinel, sentinel]
        ):
            self.assertIs(reader("https://image.example/a"), sentinel)
            self.assertIs(reader("https://image.example/b"), sentinel)
        self.assertEqual(current[0], 20)
        self.assertEqual(reader.events[0]["retry_after_seconds"], 10)
        with self.assertRaisesRegex(RuntimeError, "below"):
            c.MediaReader(interval=0.1)

    def test_octet_stream_media_is_saved_and_soft_404_image_is_a_gap(self):
        import io
        from email.message import Message

        class Response(io.BytesIO):
            def __init__(self, url, kind, data):
                super().__init__(data)
                self.url = url
                self.headers = Message()
                self.headers["Content-Type"] = kind
                self.headers["Content-Length"] = str(len(data))

            def geturl(self):
                return self.url

        image = "https://media.example/opaque"
        mislabeled = "https://media.example/mislabeled.png"
        missing = "https://media.example/old.png"
        api = Fake()
        archive = c.Archive(api, self.path)
        raw = hydrated(1)
        raw["embeds"] = [{"url": image}, {"url": mislabeled}, {"url": missing}]
        archive.add(c.cast(raw), api.neynar)
        # Old cached classification must not preserve the octet-stream omission.
        cached = (
            self.path
            / "media"
            / (c.hashlib.sha256(image.encode()).hexdigest() + ".json")
        )
        c.write(
            cached,
            dict(
                url=image,
                final_url=image,
                status="non-media-link",
                content_type="application/octet-stream",
            ),
        )

        def fetch(url):
            if url in (image, mislabeled):
                return Response(
                    url,
                    "application/octet-stream" if url == image else "text/plain",
                    b"\x89PNG\r\n\x1a\nfixture",
                )
            return Response(url, "text/html", b"not found")

        try:
            with patch.object(c, "open_public", side_effect=fetch):
                self.assertEqual(archive.media(), 2)
            records = {row["url"]: row for row in c.read(self.path / "media.json")}
            self.assertEqual(
                (self.path / records[image]["path"]).read_bytes(),
                b"\x89PNG\r\n\x1a\nfixture",
            )
            self.assertEqual(records[mislabeled]["detected_content_type"], "image/png")
            self.assertEqual(records[missing]["status"], "unresolved")
            self.assertIn("non-media content", records[missing]["detail"])
            self.assertEqual(archive.issues[0]["kind"], "unresolved-embed")
            bounded = c.Archive(api, self.path / "bounded")
            bounded.casts = archive.casts
            bounded.media_reader = c.MediaReader(sleep=lambda _: None)
            with (
                patch.object(c, "open_public", side_effect=fetch),
                patch.dict(os.environ, {"MEDIA_MAX_BYTES": "4"}),
            ):
                self.assertEqual(bounded.media(), 0)
            self.assertTrue(
                any(
                    "byte limit" in row.get("detail", "")
                    for row in c.read(bounded.path / "media.json")
                )
            )
        finally:
            api.pool.shutdown()

    def test_hls_preserves_nested_playlists_segments_and_shared_key(self):
        import io
        from email.message import Message

        base = "https://media.example/"
        master = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nlow/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2000\nhi/index.m3u8\n"
        child = b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXT-X-KEY:METHOD=AES-128,URI="../key"\n#EXTINF:1.0,\nseg.ts\n#EXT-X-ENDLIST\n'
        data = {
            "index.m3u8": master,
            "low/index.m3u8": child,
            "hi/index.m3u8": child,
            "key": bytes(range(16)),
        }
        for prefix in ("low/", "hi/"):
            data[prefix + "seg.ts"] = b"G" * 188
            data[prefix + "init.mp4"] = b"\x00\x00\x00\x10ftypfixture"
        calls = []

        class Response(io.BytesIO):
            def __init__(self, url, value):
                super().__init__(value)
                self.url = url
                self.headers = Message()
                self.headers["Content-Length"] = str(len(value))
                self.headers["Content-Type"] = (
                    "application/vnd.apple.mpegurl"
                    if url.endswith(".m3u8")
                    else "application/octet-stream"
                )

            def geturl(self):
                return self.url

        def fetch(url):
            calls.append(url)
            return Response(url, data[url.removeprefix(base)])

        archive = c.HlsArchive(self.path / "video", 10000, fetch)
        local = archive.get(base + "index.m3u8", "playlist")
        self.assertTrue(local.is_file())
        self.assertNotIn("https://", local.read_text())
        self.assertEqual(calls.count(base + "key"), 1)
        self.assertIn(master, [Path(r["path"]).read_bytes() for r in archive.resources])
        for resource in archive.resources:
            self.assertEqual(c.sha(resource["path"]), resource["sha256"])
        for name in local.read_text().splitlines():
            if name and not name.startswith("#"):
                self.assertTrue((local.parent / name).exists())
        self.assertEqual(archive.get(base + "index.m3u8", "playlist"), local)
        self.assertEqual(len(calls), len(data))

    def test_hls_rejects_live_empty_truncated_and_oversized_media(self):
        import io
        from email.message import Message

        class Response(io.BytesIO):
            def __init__(self, body, mime="application/vnd.apple.mpegurl", length=None):
                super().__init__(body)
                self.headers = Message()
                self.headers["Content-Type"] = mime
                self.headers["Content-Length"] = str(
                    len(body) if length is None else length
                )

            def geturl(self):
                return "https://media.example/index.m3u8"

        for body, message in [
            (b"#EXTM3U\n#EXTINF:1,\na.ts\n", "Unbounded"),
            (b"#EXTM3U\n#EXT-X-ENDLIST\n", "Empty"),
        ]:
            archive = c.HlsArchive(self.path, 10000, lambda url: Response(body))
            with self.assertRaisesRegex(RuntimeError, message):
                archive.get("https://media.example/index.m3u8")
        archive = c.HlsArchive(
            self.path,
            1,
            lambda url: Response(b"#EXTM3U\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n"),
        )
        with self.assertRaisesRegex(RuntimeError, "byte limit"):
            archive.get("https://media.example/index.m3u8")
        archive = c.HlsArchive(
            self.path, 10000, lambda url: Response(b"abc", "video/mp2t", 10)
        )
        with self.assertRaisesRegex(RuntimeError, "Truncated"):
            archive.get("https://media.example/segment.ts")
        with self.assertRaisesRegex(RuntimeError, "Unsupported media URL"):
            c.open_public("file:///etc/passwd")

    def test_hls_rejects_playlist_cycle(self):
        import io
        from email.message import Message

        class Response(io.BytesIO):
            def __init__(self):
                super().__init__(
                    b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nindex.m3u8\n"
                )
                self.headers = Message()
                self.headers["Content-Type"] = "application/vnd.apple.mpegurl"

            def geturl(self):
                return "https://media.example/index.m3u8"

        archive = c.HlsArchive(self.path, 10000, lambda url: Response())
        with self.assertRaisesRegex(RuntimeError, "Cycle"):
            archive.get("https://media.example/index.m3u8")

    def test_missing_cast_records_all_sources_and_rechecks_on_a_new_run(self):
        calls = []
        body = json.dumps(
            dict(
                error="Failed to get cast",
                error_detail='status: NotFound, message: "cast not found"',
            )
        )

        def request(base, *args, **kwargs):
            calls.append(base)
            if base == "index":
                raise c.APIError(
                    404,
                    json.dumps(
                        dict(code="NotFound", property="cast", message="cast not found")
                    ),
                )
            raise c.APIError(400, body)

        api = types.SimpleNamespace(
            node="one", second="two", neynar="index", request=request
        )
        archive = c.Archive(api, self.path)
        self.assertIsNone(archive.lookup(99, h(99)))
        self.assertIsNone(archive.lookup(99, h(99)))
        self.assertEqual(calls, ["one", "two", "index"])
        self.assertEqual(len(archive.issues), 1)
        self.assertEqual(len(archive.issues[0]["checked_sources"]), 3)
        self.assertIn("unproven", archive.issues[0]["cause"])
        self.assertIsNone(c.Archive(api, self.path / "new-run").lookup(99, h(99)))
        self.assertEqual(len(calls), 6)
        api.request = lambda *a, **kw: (_ for _ in ()).throw(
            c.APIError(404, "<html>proxy failure</html>")
        )
        with self.assertRaises(c.APIError):
            c.Archive(api, self.path / "proxy").lookup(None, h(99))

    def test_personal_markdown_uses_rendered_mentions_and_local_media(self):
        api = types.SimpleNamespace()
        archive = c.Archive(api, self.path)
        own = hydrated(1)
        own.update(
            text="@friend hello", embeds=[dict(url="https://image.example/a.png")]
        )
        archive.add(c.cast(own), "index")
        archive.add(c.cast(protocol(2, fid=99), True), "node")
        media = [
            dict(
                url="https://image.example/a.png",
                status="downloaded",
                content_type="image/png",
                path="media/saved-image",
            )
        ]
        archive.render_casts(list(archive.casts.values()), media)
        personal = (self.path / "own_casts.md").read_text()
        self.assertIn("@friend hello", personal)
        self.assertIn("![Embedded image](media/saved-image)", personal)
        self.assertNotIn("cast 2", personal)
        self.assertIn("cast 2", (self.path / "casts.md").read_text())

    def test_following_export_uses_outgoing_targets_and_unions_sources(self):
        def link(target):
            return dict(
                data=dict(
                    type=5, fid=c.FID, linkBody=dict(type="follow", targetFid=target)
                )
            )

        def profile(fid):
            return dict(
                fid=fid,
                username="user" + str(fid),
                display_name="User",
                verified_accounts=[dict(platform="x", username="x" + str(fid))],
            )

        def all_rows(base, path, params, field="messages", cache=None):
            if base == "index":
                self.assertEqual(path, "/v2/farcaster/following/")
                return [dict(user=profile(2)), dict(user=profile(3))]
            self.assertEqual(path, "/v1/linksByFid")
            self.assertEqual(params, dict(fid=c.FID, link_type="follow"))
            return [link(1), link(2)]

        def request(base, path, params, cache=None):
            self.assertEqual(params, {"fids": "1"})
            return {"users": [profile(1)]}

        api = types.SimpleNamespace(
            node="one", second="two", neynar="index", all=all_rows, request=request
        )
        archive = c.Archive(api, self.path)
        self.assertEqual(archive.connections("following"), (3, 3))
        saved = c.read(self.path / "following.json")
        self.assertEqual(set(map(int, saved["profiles"])), {1, 2, 3})
        self.assertEqual(saved["relationship"], "following")
        text = (self.path / "following_verified_x.csv").read_text()
        self.assertIn("relationship,fid", text)
        self.assertIn("following,1,user1", text)
        self.assertNotIn(str(c.FID), text)
        self.assertEqual(archive.issues, [])

    def test_contact_catalog_keeps_every_platform_and_bio_provenance(self):
        user = dict(
            fid=12,
            username="alice",
            display_name="Alice",
            url="https://alice.example",
            profile={
                "bio": {
                    "text": "Builds at example.org. https://t.me/alice_chat mailto:work@example.org @someone.eth"
                }
            },
            verified_accounts=[
                {"platform": "x", "username": "alice_x"},
                {"platform": "github", "username": "AliceGH"},
                {"platform": "other", "username": "public-account"},
            ],
            verified_addresses={
                "eth_addresses": ["0x" + "ab" * 20],
                "sol_addresses": ["public-sol-address"],
            },
        )
        person = c.profile_contacts(12, {"following": user})
        self.assertEqual(person["verified_eth_addresses"], ["0x" + "ab" * 20])
        github = next(e for e in person["endpoints"] if e["kind"] == "github")
        self.assertEqual(github["url"], "https://github.com/AliceGH")
        self.assertEqual(github["verification"], "Neynar verified_accounts")
        telegram = next(e for e in person["endpoints"] if e["kind"] == "telegram")
        self.assertEqual(telegram["source_field"], "profile.bio.text")
        self.assertIn("ownership not verified", telegram["verification"])
        self.assertTrue(any(e["kind"] == "mailto" for e in person["endpoints"]))
        self.assertTrue(
            any(e["url"] == "https://example.org" for e in person["endpoints"])
        )
        self.assertFalse(any("someone.eth" in e["value"] for e in person["endpoints"]))
        self.assertTrue(
            any(e["kind"] == "other" and not e["url"] for e in person["endpoints"])
        )
        self.assertEqual(person["bio"], user["profile"]["bio"]["text"])

    def test_email_like_bio_text_is_not_a_confirmed_contact(self):
        p = c.profile_contacts(
            1,
            {
                "followers": dict(
                    fid=1,
                    username="one",
                    profile={
                        "bio": {
                            "text": "Founder of@project.example and mending pipes@work.example"
                        }
                    },
                )
            },
        )
        candidates = [e for e in p["endpoints"] if e["kind"] == "email_candidate"]
        self.assertEqual(
            {e["value"] for e in candidates},
            {"of@project.example", "pipes@work.example"},
        )
        self.assertFalse(any(e["kind"] == "mailto" for e in p["endpoints"]))
        self.assertTrue(all(not e["url"] for e in candidates))

    def test_contact_export_deduplicates_mutuals_and_preserves_full_sources(self):
        import csv

        def source(direction, people):
            return dict(
                relationship=direction,
                count=len(people),
                profiles={str(u["fid"]): u for u in people},
            )

        alice = dict(
            fid=1,
            username="alice",
            display_name="=spreadsheet",
            profile={"bio": {"text": "original bio"}},
            verified_accounts=[{"platform": "github", "username": "AliceGH"}],
        )
        bob = dict(fid=2, username="bob", display_name="Bob")
        followers = source("followers", [alice, bob])
        following = source("following", [{**alice, "display_name": "Alice updated"}])
        c.write(self.path / "followers.json", followers)
        c.write(self.path / "following.json", following)
        archive = c.Archive(None, self.path)
        summary = archive.export_contacts()
        self.assertEqual(summary["unique_accounts"], 2)
        self.assertEqual(summary["mutual_accounts"], 1)
        catalog = c.read(self.path / "contacts.json")
        self.assertEqual(catalog["source_profiles"]["followers"], followers)
        self.assertEqual(catalog["source_profiles"]["following"], following)
        self.assertEqual(
            catalog["people"][0]["relationships"], ["followers", "following"]
        )
        self.assertEqual(catalog["people"][0]["display_name"], "Alice updated")
        rows = list(csv.DictReader((self.path / "contacts_followers.csv").open()))
        self.assertEqual({int(r["fid"]) for r in rows}, {1, 2})
        self.assertIn("AliceGH", rows[0]["github_usernames"])
        self.assertEqual(
            len(list(csv.DictReader((self.path / "contacts_following.csv").open()))), 1
        )
        self.assertIn("original bio", (self.path / "contacts.md").read_text())
        bob["display_name"] = "=untrusted formula"
        c.write(self.path / "followers.json", source("followers", [bob]))
        c.write(self.path / "following.json", source("following", []))
        archive.export_contacts()
        rows = list(csv.DictReader((self.path / "contacts.csv").open()))
        self.assertEqual(rows[0]["display_name"], "'=untrusted formula")
        self.assertEqual(
            c.read(self.path / "contacts.json")["people"][0]["display_name"],
            "=untrusted formula",
        )

    def test_contact_source_cannot_silently_drop_a_missing_profile_or_wrong_fid(self):
        source = dict(
            relationship="followers",
            count=1,
            profiles={},
            protocol_primary=[
                dict(data=dict(fid=7, linkBody=dict(type="follow", targetFid=c.FID)))
            ],
        )
        following = dict(relationship="following", count=0, profiles={})
        catalog = c.contact_catalog({"followers": source, "following": following})
        self.assertEqual(catalog["people"][0]["fid"], 7)
        self.assertFalse(catalog["people"][0]["profile_available"])
        self.assertEqual(catalog["summary"]["followers"]["accounts"], 1)
        self.assertEqual(catalog["summary"]["followers"]["profiles"], 0)
        source["profiles"] = {"7": {"fid": 8, "username": "wrong"}}
        with self.assertRaisesRegex(RuntimeError, "FID differs"):
            c.contact_catalog({"followers": source, "following": following})

    def test_original_reaction_history_includes_removed_targets(self):
        from types import SimpleNamespace
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        wire = c.WireReader("unused-offline-test-key")
        response = wire.types["MessagesResponse"]()
        key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        try:
            for typ, target in ((3, 7), (4, 8)):
                message = response.messages.add()
                message.data.type = typ
                message.data.fid = c.FID
                message.data.timestamp = 100 + typ
                message.data.network = 1
                message.data.reaction_body.type = 1
                message.data.reaction_body.target_cast_id.fid = 99
                message.data.reaction_body.target_cast_id.hash = bytes.fromhex(
                    h(target)[2:]
                )
                message.data_bytes = message.data.SerializeToString()
                message.hash = wire.blake3.blake3(message.data_bytes).digest()[:20]
                message.signature = key.sign(message.hash)
                message.signer = key.public_key().public_bytes(
                    Encoding.Raw, PublicFormat.Raw
                )
                message.hash_scheme = 1
                message.signature_scheme = 1

            def call(method, request, cache):
                self.assertEqual(method, "GetAllReactionMessagesByFid")
                self.assertEqual(request.fid, c.FID)
                return response.SerializeToString(), None

            wire.call = call
            rows, targets = wire.reaction_history(SimpleNamespace(path=self.path))
            self.assertEqual(targets, {(99, h(7)), (99, h(8))})
            self.assertEqual([r["type"] for r in rows], [3, 4])
            self.assertTrue(c.read(self.path / "reaction_history.json")["complete"])
            self.assertTrue(all((self.path / r["path"]).exists() for r in rows))
        finally:
            wire.channel.close()

    def test_historical_own_content_cannot_enter_the_surviving_deletion_plan(self):
        self.make_snapshot(self.path)
        casts = c.read(self.path / "casts.json")
        casts.append(c.cast(protocol(3, timestamp=0), True))
        c.write(self.path / "casts.json", casts)
        summary = c.read(self.path / "summary.json")
        summary.update(complete=False, all_casts=3, issue_count=1)
        c.write(self.path / "summary.json", summary)
        c.write(
            self.path / "issues.json",
            [{"kind": "additional-authored-cast-outside-inventory", "hash": h(3)}],
        )
        manifest = c.read(self.path / "manifest.json")
        manifest["complete"] = False
        manifest["files"] = {n: c.sha(self.path / n) for n in manifest["files"]}
        c.write(self.path / "manifest.json", manifest)
        self.assertFalse(c.verify_snapshot(self.path)["complete"])
        self.assertEqual(
            c.read(self.path / "deletion_plan.json")["deletes"], [h(1), h(2)]
        )

    def test_archive_root_rejects_another_account_before_reusing_caches(self):
        c.bind_archive(self.path)
        c.bind_archive(self.path)
        other = types.ModuleType("other_account")
        with patch.dict(os.environ, {"FID": "67890"}):
            exec(compile(source, "other_account.py", "exec"), other.__dict__)
        with self.assertRaisesRegex(RuntimeError, "different FID"):
            other.bind_archive(self.path)
        self.assertEqual(c.read(self.path / "account.json"), dict(schema=1, fid=c.FID))
        unbound = self.path / "unbound"
        unbound.mkdir()
        c.write(unbound / "working.json", {})
        with self.assertRaisesRegex(RuntimeError, "not empty"):
            c.bind_archive(unbound)

    def test_snapshot_rejects_different_account_even_with_valid_checksums(self):
        self.make_snapshot(self.path)
        summary = c.read(self.path / "summary.json")
        summary["fid"] = 67890
        c.write(self.path / "summary.json", summary)
        manifest = c.read(self.path / "manifest.json")
        manifest["files"]["summary.json"] = c.sha(self.path / "summary.json")
        c.write(self.path / "manifest.json", manifest)
        with self.assertRaisesRegex(RuntimeError, "different FID"):
            c.verify_snapshot(self.path)

    def test_custody_is_discovered_for_selected_fid_and_changes_are_rejected(self):
        api = object.__new__(c.Client)
        api.rpc = "https://rpc.example.invalid"
        address = "0x" + "34" * 20
        replies = [
            dict(result="0xa"),
            dict(result="0x123"),
            dict(result="0x" + "0" * 24 + address[2:]),
        ]
        api.request = unittest.mock.Mock(side_effect=replies)
        self.assertEqual(api.custody()["custody"], address)
        request = json.loads(api.request.call_args.kwargs["data"])
        self.assertEqual(
            request["params"][0]["data"], "0x65269e47" + format(c.FID, "064x")
        )
        api.request = unittest.mock.Mock(side_effect=replies)
        with self.assertRaisesRegex(RuntimeError, "differs from the archived"):
            api.custody("0x" + "56" * 20)
        api.request = unittest.mock.Mock(
            side_effect=[
                dict(result="0xa"),
                dict(result="0x123"),
                dict(result="0x" + "0" * 64),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "not registered"):
            api.custody()

    def test_missing_and_invalid_fids_fail_before_creating_output(self):
        for fid in ("", "0", "-1", "abc", str(2**63)):
            with self.subTest(fid=fid), patch.dict(os.environ, {"FID": fid}):
                module = types.ModuleType("invalid_fid")
                exec(compile(source, "invalid_fid.py", "exec"), module.__dict__)
                output = self.path / "must-not-exist"
                with (
                    patch.dict(os.environ, {"OUT": str(output)}),
                    patch.object(module.sys, "argv", ["script", "--dry-run"]),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Set FID"):
                        module.main()
                self.assertFalse(output.exists())

    def test_empty_account_can_be_archived_but_cannot_register_a_delete_signer(self):
        self.make_snapshot(self.path)
        summary = c.read(self.path / "summary.json")
        summary.update(authored=0, all_casts=0, thread_casts=0, planned_deletions=0)
        for name, obj in [
            ("summary.json", summary),
            ("deletion_plan.json", c.plan([])),
            ("casts.json", []),
            ("own_casts.json", []),
            ("own_inventory.json", [[], [], []]),
            ("threads.json", dict(hashes=[])),
            ("wire_index.json", dict(messages={})),
        ]:
            c.write(self.path / name, obj)
        manifest = c.read(self.path / "manifest.json")
        manifest["files"] = {
            name: c.sha(self.path / name) for name in manifest["files"]
        }
        c.write(self.path / "manifest.json", manifest)
        self.assertTrue(c.verify_snapshot(self.path, True)["complete"])
        with self.assertRaisesRegex(RuntimeError, "No surviving authored casts"):
            c.broadcast(types.SimpleNamespace(), self.path, self.path)
        self.assertFalse((self.path / "secrets").exists())

    def make_snapshot(self, path, partial=False):
        own = [
            c.cast(protocol(1, timestamp=1), True),
            c.cast(protocol(2, timestamp=2), True),
        ]
        deletion = c.plan(own)
        names = [
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
        ]
        for name in names:
            c.write(path / name, {})
        issues = [{"kind": "missing-parent"}] if partial else []
        summary = dict(
            fid=c.FID,
            authored=2,
            planned_deletions=2,
            all_casts=2,
            thread_casts=2,
            issue_count=len(issues),
            complete=not partial,
            protected=deletion["protected"],
        )
        for name, obj in [
            ("summary.json", summary),
            ("custody.json", dict(fid=c.FID, custody="0x" + "12" * 20)),
            ("deletion_plan.json", deletion),
            ("casts.json", own),
            ("own_casts.json", own),
            ("issues.json", issues),
            ("threads.json", {"hashes": [h(1), h(2)]}),
            ("media.json", []),
            ("own_inventory.json", [[h(1), h(2)]] * 3),
            ("reaction_history.json", {"complete": True, "messages": []}),
        ]:
            c.write(path / name, obj)
        wire = {}
        for item in own:
            name = "wire/messages/" + item["hash"] + ".pb"
            c.write(path / name, b"fixture-original-bytes")
            wire[item["hash"]] = dict(
                path=name, sha256=c.sha(path / name), verified_hash_and_signature=True
            )
            names.append(name)
        c.write(path / "wire_index.json", {"messages": wire})
        c.write(
            path / "manifest.json",
            dict(
                schema=1,
                complete=not partial,
                files={n: c.sha(path / n) for n in names},
            ),
        )
        return summary

    @unittest.skipUnless(
        c.sys.platform == "darwin", "Uses the macOS controlling-terminal harness"
    )
    def test_confirmation_uses_a_real_controlling_terminal_and_rejects_wrong_text(self):
        import selectors
        import subprocess
        import time

        worker = """
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text().split("<<'FARCASTER_PYTHON'\\n", 1)[1].rsplit('\\nFARCASTER_PYTHON', 1)[0]
import os
os.environ['FID'] = '12345'
ns = {'__name__': 'terminal_fixture'}
exec(compile(text, 'terminal_fixture.py', 'exec'), ns)
try:
    ns['confirm_deletion'](3)
except RuntimeError:
    raise SystemExit(2)
except Exception:
    raise SystemExit(3)
"""
        for answer, expected in (("DELETE ALL 3 CASTS FOR FID 12345", 0), ("no", 2)):
            with self.subTest(answer=answer):
                # macOS script creates a real controlling terminal in a native child.
                # Only public test confirmation text is sent; no phrase or signing runs.
                process = subprocess.Popen(
                    [
                        "/usr/bin/script",
                        "-qF",
                        "/dev/null",
                        c.sys.executable,
                        "-c",
                        worker,
                        str(BASE / "farcaster_cleanup_all.sh"),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                )
                try:
                    deadline, output = time.monotonic() + 10, b""
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while b"to authorize mainnet deletion: " not in output:
                            remaining = deadline - time.monotonic()
                            self.assertGreater(remaining, 0)
                            self.assertTrue(
                                selector.select(remaining), "Terminal prompt timed out"
                            )
                            chunk = os.read(process.stdout.fileno(), 4096)
                            self.assertTrue(chunk, output)
                            output += chunk
                    # Keep stdin open: BSD script sends an EOF character when its
                    # input closes, which can otherwise arrive before the answer.
                    process.stdin.write((answer + "\n").encode())
                    process.stdin.flush()
                    self.assertEqual(process.wait(timeout=10), expected, output)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdin.close()
                    process.stdout.close()

    def test_manifest_checksums_and_partial_gate(self):
        self.make_snapshot(self.path)
        self.assertTrue(c.verify_snapshot(self.path, True)["complete"])
        c.write(self.path / "casts.md", b"tampered")
        with self.assertRaisesRegex(RuntimeError, "modified"):
            c.verify_snapshot(self.path)
        self.make_snapshot(self.path, partial=True)
        self.assertFalse(c.verify_snapshot(self.path)["complete"])
        with self.assertRaisesRegex(RuntimeError, "unresolved archival"):
            c.verify_snapshot(self.path, True)
        api = Fake()
        with (
            patch.object(api, "request", side_effect=AssertionError("network used")),
            self.assertRaisesRegex(RuntimeError, "unresolved archival"),
        ):
            c.broadcast(api, self.path, self.path)
        api.pool.shutdown()

    def test_gap_acceptance_binds_exact_snapshot_and_cannot_accept_critical_failures(
        self,
    ):
        self.make_snapshot(self.path, partial=True)

        def approve():
            c.write(
                self.path / "reviewed-backup.json",
                dict(
                    schema=1,
                    fid=c.FID,
                    decision="accept-recorded-content-gaps",
                    manifest_sha256=c.sha(self.path / "manifest.json"),
                    issues_sha256=c.sha(self.path / "issues.json"),
                    issue_count=1,
                ),
            )

        with self.assertRaisesRegex(RuntimeError, "explicit review"):
            c.verify_broadcast_backup(self.path, self.path)
        approve()
        self.assertFalse(c.verify_broadcast_backup(self.path, self.path)["complete"])
        self.assertFalse(c.read(self.path / "manifest.json")["complete"])
        manifest = c.read(self.path / "manifest.json")
        manifest["sealed_at"] = "new snapshot"
        c.write(self.path / "manifest.json", manifest)
        with self.assertRaisesRegex(RuntimeError, "explicit review"):
            c.verify_broadcast_backup(self.path, self.path)
        c.write(
            self.path / "issues.json",
            [{"kind": "authored-inventory-changed-during-backup"}],
        )
        manifest["files"]["issues.json"] = c.sha(self.path / "issues.json")
        c.write(self.path / "manifest.json", manifest)
        approve()
        with self.assertRaisesRegex(RuntimeError, "Critical"):
            c.verify_broadcast_backup(self.path, self.path)

    def test_missing_personal_archive_record_still_blocks_with_gap_acceptance(self):
        self.make_snapshot(self.path, partial=True)
        c.write(self.path / "own_casts.json", c.read(self.path / "own_casts.json")[:1])
        manifest = c.read(self.path / "manifest.json")
        manifest["files"]["own_casts.json"] = c.sha(self.path / "own_casts.json")
        c.write(self.path / "manifest.json", manifest)
        with self.assertRaisesRegex(RuntimeError, "Personal authored archive"):
            c.verify_broadcast_backup(self.path, self.path)

    def test_partial_snapshot_cache_resume_preserves_sealed_originals(self):
        previous = self.path / "snapshots" / "previous"
        self.make_snapshot(previous, partial=True)
        cached = previous / "media" / "image"
        c.write(cached, b"original")
        c.write(previous / "raw" / "page.json", dict(response=[]))
        c.write(previous / "wire" / "responses" / "read.pb", b"protocol")
        manifest = c.read(previous / "manifest.json")
        for name in ("media/image", "raw/page.json", "wire/responses/read.pb"):
            manifest["files"][name] = c.sha(previous / name)
        c.write(previous / "manifest.json", manifest)
        c.verify_snapshot(previous)
        digest = c.sha(previous / "manifest.json")
        pending = c.prepare_working(self.path, previous)
        self.assertTrue(cached.samefile(pending / "media" / "image"))
        self.assertFalse((pending / "issues.json").exists())
        self.assertFalse((pending / "summary.json").exists())
        self.assertEqual((pending / "wire/responses/read.pb").read_bytes(), b"protocol")
        c.write(pending / "media" / "image", b"replacement")
        c.write(pending / "raw/page.json", dict(response=["new"]))
        self.assertEqual(cached.read_bytes(), b"original")
        self.assertEqual(c.sha(previous / "manifest.json"), digest)
        self.assertFalse(c.verify_snapshot(previous)["complete"])
        self.assertEqual(c.prepare_working(self.path, previous), pending)
        self.assertEqual((pending / "media" / "image").read_bytes(), b"replacement")

    def test_snapshot_pointer_detects_manifest_replacement(self):
        snap = self.path / "snapshots" / "test"
        self.make_snapshot(snap)
        pointer = self.path / "snapshot.json"
        c.write(
            pointer,
            dict(path="snapshots/test", manifest_sha256=c.sha(snap / "manifest.json")),
        )
        self.assertEqual(c.snapshot_path(self.path, pointer), snap)
        c.write(snap / "manifest.json", {"complete": True})
        with self.assertRaisesRegex(RuntimeError, "changed since sealing"):
            c.snapshot_path(self.path, pointer)

    def test_cross_shard_terminal_cursor_does_not_restart_pagination(self):
        api = object.__new__(c.Client)
        api.neynar = "n"
        token = base64.b64encode(b"[null,null]").decode()
        api.request = lambda *args: {"messages": [1, 2], "nextPageToken": token}
        self.assertEqual(
            api.all("node", "/v1/linksByTargetFid", {}, "messages"), [1, 2]
        )

    def test_dry_run_entrypoint_never_calls_protocol_writers(self):
        api = Fake()
        api.custody = lambda: {}
        api.nodes = lambda: {}

        def archive_run(archive):
            return self.make_snapshot(archive.path)

        with (
            patch.dict(os.environ, {"OUT": str(self.path)}),
            patch.object(c.sys, "argv", ["script", "--dry-run"]),
            patch.object(c, "Client", return_value=api),
            patch.object(c.Archive, "run", archive_run),
            patch.object(
                c, "broadcast", side_effect=AssertionError("broadcast invoked")
            ),
            patch.object(c, "submit_once", side_effect=AssertionError("write invoked")),
            patch.object(
                c, "capture_cli", side_effect=AssertionError("signer invoked")
            ),
        ):
            c.main()
        self.assertTrue((self.path / "snapshot.json").exists())
        self.assertFalse((self.path / "secrets").exists())
        api.pool.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
