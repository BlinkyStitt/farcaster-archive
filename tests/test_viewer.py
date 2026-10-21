import http.client
import fcntl
import json
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import sys

from archive_viewer import server as v


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.archive = self.path / "archive"
        self.contacts = self.path / "contacts"
        self.archive.mkdir()
        self.contacts.mkdir()
        self.cache = self.path / "derived/index.sqlite"

    def write(self, root, name, data):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data))

    def fixture(self, fid=12345):
        def cast(h, fid, stamp, parent=None, text="Saved text"):
            return dict(
                hash=h,
                fid=fid,
                timestamp=stamp,
                unix_timestamp=stamp + 1609459200,
                published="2021-01-01T00:00:00+00:00",
                text=text,
                parent=[5, parent] if parent else None,
                sources={
                    "provider": dict(
                        text=text,
                        author=dict(display_name="Alice", username="alice"),
                        embeds=[],
                    )
                },
            )

        self.records = [
            cast("later", fid, 30, "reply"),
            cast("root", 5, 1),
            cast("reply", fid, 10, "root", "<img src=x onerror=alert(1)> 100% saved"),
            cast("orphan", 6, 20, "missing"),
        ]
        media = dict(
            url="https://example.invalid/image",
            status="downloaded",
            path="media/image",
            content_type="image/png",
            bytes=6,
            referenced_by=["reply"],
            authored_casts=["reply"],
        )
        self.write(self.archive, "casts.json", self.records)
        self.write(self.archive, "media.json", [media])
        self.write(
            self.archive,
            "reactions.json",
            dict(targets=[[5, "root"], [99, "missing-target"]], quotes=[[6, "orphan"]]),
        )
        self.write(self.archive, "summary.json", dict(fid=fid, authored=2, all_casts=4))
        self.write(self.archive, "issues.json", [])
        (self.archive / "media").mkdir()
        (self.archive / "media/image").write_bytes(b"abcdef")
        person = dict(
            fid=5,
            relationships=["following"],
            display_name="Alice",
            bio="Hello",
            verified_eth_addresses=[],
            verified_sol_addresses=[],
            endpoints=[],
        )
        self.write(self.contacts, "contacts.json", dict(fid=fid, people=[person]))
        self.write(self.contacts, "contacts_summary.json", dict(unique_accounts=1))
        for root in (self.archive, self.contacts):
            files = {
                str(f.relative_to(root)): v.digest(f)
                for f in root.rglob("*")
                if f.is_file()
            }
            self.write(root, "manifest.json", dict(files=files))
        v.prepare_index(self.archive, self.contacts, self.cache)
        return v.Viewer(self.archive, self.contacts, self.cache)

    def test_account_identity_controls_own_timeline_and_contact_catalog(self):
        viewer = self.fixture(fid=67890)
        summary = viewer.api("/api/summary", {})
        self.assertEqual(summary["fid"], 67890)
        self.assertEqual(summary["oldest"], 1609459210)
        self.assertEqual(summary["newest"], 1609459230)
        self.assertEqual(
            [
                item["fid"]
                for item in viewer.api("/api/casts", {"view": ["own"]})["items"]
            ],
            [67890, 67890],
        )
        self.cache.unlink()
        catalog = json.loads((self.contacts / "contacts.json").read_text())
        catalog["fid"] = 12345
        self.write(self.contacts, "contacts.json", catalog)
        manifest = json.loads((self.contacts / "manifest.json").read_text())
        manifest["files"]["contacts.json"] = v.digest(self.contacts / "contacts.json")
        self.write(self.contacts, "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "different FID"):
            v.prepare_index(self.archive, self.contacts, self.cache)

    def test_chronology_filtering_and_exact_raw_source(self):
        viewer = self.fixture()
        own = viewer.api("/api/casts", {"view": ["own"]})
        self.assertEqual([c["hash"] for c in own["items"]], ["reply", "later"])
        self.assertEqual(own["items"][0]["parent_preview"]["hash"], "root")
        self.assertEqual(len(own["items"][0]["media"]), 1)
        newest = viewer.api("/api/casts", {"order": ["desc"]})
        self.assertEqual(newest["items"][0]["hash"], "later")
        literal = viewer.api("/api/casts", {"q": ["100%"]})
        self.assertEqual(literal["total"], 1)
        first = viewer.api("/api/casts", {"limit": ["1"]})
        second = viewer.api(
            "/api/casts", {"limit": ["1"], "offset": [str(first["next"])]}
        )
        self.assertEqual(second["items"][0]["hash"], "later")
        raw = viewer.api("/api/cast", {"hash": ["reply"], "raw": ["1"]})
        self.assertEqual(raw, self.records[2])
        only_later = viewer.api("/api/casts", {"from": [str(1609459220)]})
        self.assertEqual([c["hash"] for c in only_later["items"]], ["later"])
        only_earlier = viewer.api("/api/casts", {"to": [str(1609459220)]})
        self.assertEqual([c["hash"] for c in only_earlier["items"]], ["reply"])

    def test_thread_context_missing_parents_and_reaction_gaps(self):
        viewer = self.fixture()
        thread = viewer.api("/api/thread", {"hash": ["later"]})
        self.assertEqual(thread["root"], "root")
        self.assertEqual(
            [c["hash"] for c in thread["items"]], ["root", "reply", "later"]
        )
        context = viewer.api("/api/cast", {"hash": ["later"]})
        self.assertEqual([c["hash"] for c in context["ancestors"]], ["root", "reply"])
        orphan = viewer.api("/api/thread", {"hash": ["orphan"]})
        self.assertEqual(orphan["root"], "missing")
        self.assertEqual(len(orphan["items"]), 1)
        missing = viewer.api("/api/missing-targets", {})
        self.assertEqual([r["hash"] for r in missing], ["missing-target"])
        self.assertEqual(
            viewer.api("/api/casts", {"view": ["quotes"]})["items"][0]["hash"], "orphan"
        )
        self.assertEqual(
            [
                c["hash"]
                for c in viewer.api("/api/casts", {"view": ["reactions"]})["items"]
            ],
            ["root"],
        )
        self.assertEqual(
            viewer.api("/api/contacts", {"relationship": ["following"]})["total"], 1
        )
        self.assertEqual(
            viewer.api("/api/contacts", {"relationship": ["followers"]})["total"], 0
        )

    def test_server_ranges_host_guard_paths_and_source_immutability(self):
        viewer = self.fixture()
        before = {
            f: v.digest(f)
            for root in (self.archive, self.contacts)
            for f in root.rglob("*")
            if f.is_file()
        }
        httpd = v.ThreadingHTTPServer(("127.0.0.1", 0), v.Handler)
        httpd.viewer = viewer
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
        self.addCleanup(conn.close)

        def request(path, headers=None, method="GET"):
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.headers), response.read()

        status, headers, body = request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Your Farcaster archive", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, headers, body = request("/saved/media/image", {"Range": "bytes=2-4"})
        self.assertEqual((status, body), (206, b"cde"))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Content-Range"], "bytes 2-4/6")
        self.assertEqual(request("/saved/media/image", {"Range": "bytes=-2"})[2], b"ef")
        self.assertEqual(request("/saved/media/image", {"Range": "bytes=90-"})[0], 416)
        self.assertEqual(request("/saved/media/image", method="HEAD")[2], b"")
        self.assertEqual(request("/", {"Host": "attacker.example"})[0], 403)
        self.assertEqual(
            request("/api/summary", {"Sec-Fetch-Site": "cross-site"})[0], 403
        )
        self.assertEqual(request("/saved/../summary.json")[0], 404)
        self.assertEqual(request("/files/casts/../secrets/signer.json")[0], 404)
        status, headers, body = request("/files/casts/casts.json")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(json.loads(body), self.records)
        self.assertEqual(before, {f: v.digest(f) for f in before})

    def test_corruption_does_not_replace_index_and_parent_cycles_fail(self):
        self.fixture()
        old_index = v.digest(self.cache)
        manifest = v.load(self.archive / "manifest.json")
        manifest["files"]["casts.json"] = "incorrect"
        self.write(self.archive, "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            v.prepare_index(self.archive, self.contacts, self.cache)
        self.assertEqual(v.digest(self.cache), old_index)
        with self.assertRaisesRegex(ValueError, "Cycle"):
            v.roots_for({"a": "b", "b": "a"}, {})

    def test_stream_parser_and_known_root_after_missing_parent(self):
        file = self.path / "array.json"
        payload = [{"text": "Hello 🐈" * 200000}, {"last": 1}]
        file.write_text(json.dumps(payload))
        self.assertEqual(list(v.array_items(file)), payload)
        file.write_text("[]")
        self.assertEqual(list(v.array_items(file)), [])
        file.write_text("[{},]")
        with self.assertRaises(ValueError):
            list(v.array_items(file))
        self.assertEqual(
            v.roots_for({"reply": "missing", "root": None}, {"reply": "root"})["reply"],
            "root",
        )

    def test_second_process_cannot_rebuild_an_index_in_use(self):
        self.fixture()
        before = v.digest(self.cache)
        with self.cache.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [
                    sys.executable,
                    v.__file__,
                    "--archive",
                    str(self.archive),
                    "--contacts",
                    str(self.contacts),
                    "--cache",
                    str(self.cache),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("already in use", result.stderr)
        self.assertEqual(v.digest(self.cache), before)


if __name__ == "__main__":
    unittest.main()
