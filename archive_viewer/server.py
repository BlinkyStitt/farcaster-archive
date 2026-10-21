#!/usr/bin/env python3
"""Read-only, loopback-only browser for a sealed Farcaster archive."""

import argparse
import collections
from contextlib import closing
import fcntl
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import urllib.parse
import webbrowser
import zlib


def digest(path):
    with path.open("rb") as fp:
        return hashlib.file_digest(fp, "sha256").hexdigest()


def load(path):
    return json.loads(path.read_text())


def media_preview(item):
    fields = (
        "id",
        "url",
        "status",
        "content_type",
        "detected_content_type",
        "path",
        "format",
        "bytes",
        "bundle_bytes",
        "detail",
        "error",
    )
    result = {k: item[k] for k in fields if k in item}
    result["reference_count"] = len(item.get("referenced_by", []))
    result["referenced_by"] = item.get("referenced_by", [])[:1]
    result["authored_casts"] = item.get("authored_casts", [])[:1]
    return result


def array_items(path):
    """Read the large top-level cast array without loading it into memory."""
    decoder = json.JSONDecoder()
    with path.open() as fp:
        buffer, eof, started, expect_value, first = "", False, False, True, True
        while True:
            buffer = buffer.lstrip()
            if not buffer and not eof:
                chunk = fp.read(1024 * 1024)
                buffer += chunk
                eof = not chunk
                continue
            if not started:
                if not buffer.startswith("["):
                    raise ValueError("Expected a top-level JSON array")
                started, buffer = True, buffer[1:]
                continue
            if buffer.startswith("]"):
                if expect_value and not first:
                    raise ValueError("Trailing comma in JSON array")
                if (buffer[1:] + fp.read()).strip():
                    raise ValueError("Data after JSON array")
                return
            if not expect_value:
                if not buffer.startswith(","):
                    raise ValueError("Missing comma in JSON array")
                buffer, expect_value = buffer[1:], True
                continue
            try:
                value, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if eof:
                    raise
                chunk = fp.read(1024 * 1024)
                buffer += chunk
                eof = not chunk
                continue
            yield value
            buffer, expect_value, first = buffer[end:], False, False


def roots_for(parents, hints):
    roots = {}
    for start in parents:
        chain, seen, current = [], set(), start
        while current in parents and current not in roots:
            if current in seen:
                raise ValueError("Cycle in cast parent graph")
            chain.append(current)
            seen.add(current)
            if not parents[current]:
                break
            current = parents[current]
        root = roots.get(current, current)
        # An unavailable immediate parent must not hide the known thread root.
        if root not in parents:
            root = next((hints[h] for h in chain if hints.get(h) in parents), root)
        for h in chain:
            roots[h] = root
    return roots


def read_manifest(path):
    manifest = load(path / "manifest.json")
    entries = manifest["files"]
    if isinstance(entries, list):
        entries = {r["path"]: r for r in entries}
    return entries


def check_source(root, name, manifest):
    record = manifest[name]
    expected = record if isinstance(record, str) else record["sha256"]
    if digest(root / name) != expected:
        raise ValueError("Archive checksum mismatch: " + name)


def prepare_index(archive, contacts, cache):
    """Build only derived data. Never write to either source snapshot."""
    key = digest(archive / "manifest.json") + ":" + digest(contacts / "manifest.json")
    if cache.exists():
        with closing(sqlite3.connect(cache)) as db:
            try:
                if db.execute(
                    "select value from meta where key='source'"
                ).fetchone() == (key,):
                    return
            except sqlite3.DatabaseError:
                pass
    cache.parent.mkdir(parents=True, exist_ok=True)
    pending = cache.with_suffix(".building.sqlite")
    if pending.exists():
        pending.unlink()
    manifest, contact_manifest = read_manifest(archive), read_manifest(contacts)
    for name in (
        "casts.json",
        "media.json",
        "reactions.json",
        "summary.json",
        "issues.json",
    ):
        check_source(archive, name, manifest)
    check_source(contacts, "contacts.json", contact_manifest)
    summary = load(archive / "summary.json")
    account_fid = summary["fid"]
    if type(account_fid) is not int or not 0 < account_fid <= 2**63 - 1:
        raise ValueError("Invalid archive FID")
    if load(contacts / "contacts.json").get("fid") != account_fid:
        raise ValueError("Contact catalog belongs to a different FID")
    db = sqlite3.connect(pending)
    try:
        db.executescript("""
            create table meta(key text primary key, value text);
            create table casts(hash text primary key, fid integer, stamp integer,
              parent text, thread text, search text, view text, raw blob);
            create table media(id integer primary key, url text, status text,
              kind text, path text, own integer, view text);
            create table cast_media(hash text, media_id integer);
            create table targets(hash text primary key, fid integer, stamp integer, labels text);
            create table contacts(fid integer primary key, following integer,
              follower integer, search text, view text);
        """)
        media_records = load(archive / "media.json")
        for ix, item in enumerate(media_records):
            item = dict(item, id=ix)
            db.execute(
                "insert into media values(?,?,?,?,?,?,?)",
                (
                    ix,
                    item["url"],
                    item["status"],
                    item.get("detected_content_type", item.get("content_type", "")),
                    item.get("path"),
                    bool(item.get("authored_casts")),
                    json.dumps(item),
                ),
            )
            db.executemany(
                "insert into cast_media values(?,?)",
                [(h, ix) for h in item.get("referenced_by", [])],
            )
        del media_records
        parents, hints = {}, {}
        count = 0
        for cast in array_items(archive / "casts.json"):
            hydrated = next((s for s in cast["sources"].values() if "author" in s), {})
            author = hydrated.get("author", {})
            text = hydrated.get("text", cast["text"])
            parent = (cast.get("parent") or [None, None])[1]
            parents[cast["hash"]] = parent
            hints[cast["hash"]] = hydrated.get("thread_hash")
            quotes = set()
            for source in cast["sources"].values():
                body = source.get("data", {}).get("castAddBody", source)
                for embed in body.get("embeds", []):
                    target = embed.get("castId") or embed.get("cast")
                    if target and target.get("hash"):
                        quotes.add(target["hash"])
            view = dict(
                hash=cast["hash"],
                fid=cast["fid"],
                timestamp=cast["unix_timestamp"],
                published=cast["published"],
                text=text,
                parent=parent,
                author=author.get("display_name")
                or author.get("username")
                or f"FID {cast['fid']}",
                username=author.get("username", ""),
                quotes=sorted(quotes),
                channel=(hydrated.get("channel") or {}).get("name", ""),
            )
            db.execute(
                "insert into casts values(?,?,?,?,?,?,?,?)",
                (
                    cast["hash"],
                    cast["fid"],
                    cast["unix_timestamp"],
                    parent,
                    None,
                    (
                        text
                        + " "
                        + view["author"]
                        + " "
                        + view["username"]
                        + " "
                        + cast["hash"]
                    ).lower(),
                    json.dumps(view),
                    zlib.compress(json.dumps(cast, ensure_ascii=False).encode()),
                ),
            )
            count += 1
            if count % 10000 == 0:
                print(f"Indexed {count:,} casts", flush=True)
        roots = roots_for(parents, hints)
        db.executemany(
            "update casts set thread=? where hash=?",
            [(root, h) for h, root in roots.items()],
        )
        reactions = load(archive / "reactions.json")
        targets = {}

        def target(fid, h, label, stamp=0):
            row = targets.setdefault(h, dict(fid=fid, labels=set(), stamp=stamp))
            row["labels"].add(label)
            row["stamp"] = max(row["stamp"], stamp)

        for item in reactions.get("original_history", []):
            if item.get("target"):
                fid, h = item["target"]
                label = "Liked" if item["reaction_type"] == 1 else "Recast"
                if item["type"] == 4:
                    label += " (removed)"
                target(fid, h, label, item["timestamp"] + 1609459200)
        for message in reactions.get("protocol", []):
            body = message["data"]["reactionBody"]
            if body.get("targetCastId"):
                t = body["targetCastId"]
                target(
                    t["fid"],
                    t["hash"],
                    "Liked" if body["type"] in (1, "REACTION_TYPE_LIKE") else "Recast",
                )
        for item in reactions.get("neynar", []):
            cast = item["cast"]
            target(
                cast["author"]["fid"],
                cast["hash"],
                "Liked" if item["reaction_type"] == "like" else "Recast",
            )
        for fid, h in reactions.get("targets", []):
            if h not in targets:
                target(fid, h, "Reaction target")
        for fid, h in reactions.get("quotes", []):
            target(fid, h, "Quoted")
        db.executemany(
            "insert into targets values(?,?,?,?)",
            [
                (h, row["fid"], row["stamp"], json.dumps(sorted(row["labels"])))
                for h, row in targets.items()
            ],
        )
        for person in load(contacts / "contacts.json")["people"]:
            db.execute(
                "insert into contacts values(?,?,?,?,?)",
                (
                    person["fid"],
                    "following" in person["relationships"],
                    "followers" in person["relationships"],
                    json.dumps(person, ensure_ascii=False).lower(),
                    json.dumps(person, ensure_ascii=False),
                ),
            )
        summary = load(archive / "summary.json")
        if count != summary["all_casts"]:
            raise ValueError("Indexed cast count differs from archive summary")
        if (
            db.execute(
                "select count(*) from casts where fid=?", (account_fid,)
            ).fetchone()[0]
            != summary["authored"]
        ):
            raise ValueError("Indexed own casts differ from archive summary")
        db.executescript("""
            create index cast_date on casts(stamp,hash);
            create index own_date on casts(fid,stamp,hash);
            create index cast_parent on casts(parent,stamp,hash);
            create index cast_thread on casts(thread,stamp,hash);
            create index media_cast on cast_media(hash);
        """)
        db.executemany(
            "insert into meta values(?,?)",
            [("source", key), ("summary", json.dumps(summary))],
        )
        db.commit()
    finally:
        db.close()
    os.chmod(pending, 0o600)
    os.replace(pending, cache)
    print(f"Index ready: {count:,} casts", flush=True)


class Viewer:
    def __init__(self, archive, contacts, cache, reports=None):
        self.archive, self.contacts, self.cache = archive, contacts, cache
        self.fid = load(archive / "summary.json")["fid"]
        self.static = Path(__file__).parent
        self.manifest = read_manifest(archive)
        self.exports = {}
        for label, root in (("casts", archive), ("contacts", contacts)):
            for f in sorted(root.iterdir()):
                if f.is_file() and f.suffix in (".json", ".csv", ".md"):
                    self.exports[label + "/" + f.name] = f
        if reports:
            for f in sorted(reports.glob("*.csv")):
                if f.is_file():
                    self.exports["reports/" + f.name] = f

    def db(self):
        db = sqlite3.connect(self.cache.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def cast(self, db, row):
        if not row:
            return None
        result = json.loads(row["view"])
        result["thread"] = row["thread"]
        result["replies"] = db.execute(
            "select count(*) from casts where parent=?", (row["hash"],)
        ).fetchone()[0]
        result["media"] = [
            media_preview(json.loads(m[0]))
            for m in db.execute(
                "select m.view from media m join cast_media cm on m.id=cm.media_id where cm.hash=?",
                (row["hash"],),
            )
        ]
        labels = db.execute(
            "select labels from targets where hash=?", (row["hash"],)
        ).fetchone()
        result["labels"] = json.loads(labels[0]) if labels else []
        if result["parent"]:
            parent = db.execute(
                "select view from casts where hash=?", (result["parent"],)
            ).fetchone()
            result["parent_preview"] = json.loads(parent[0]) if parent else None
        return result

    def api(self, route, params):
        def value(key, default=""):
            return params.get(key, [default])[0]

        offset = max(0, int(value("offset", "0")))
        limit = min(100, max(1, int(value("limit", "30"))))
        search = value("q").lower()[:500]
        like = (
            "%"
            + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        with closing(self.db()) as db:
            if route == "/api/summary":
                summary = json.loads(
                    db.execute("select value from meta where key='summary'").fetchone()[
                        0
                    ]
                )
                summary["contacts"] = load(self.contacts / "contacts_summary.json")
                summary["oldest"] = db.execute(
                    "select min(stamp) from casts where fid=?", (self.fid,)
                ).fetchone()[0]
                summary["newest"] = db.execute(
                    "select max(stamp) from casts where fid=?", (self.fid,)
                ).fetchone()[0]
                summary["exports"] = [
                    dict(key=k, bytes=p.stat().st_size) for k, p in self.exports.items()
                ]
                summary["issues"] = collections.Counter(
                    i["kind"] for i in load(self.archive / "issues.json")
                )
                return summary
            if route == "/api/casts":
                view = value("view", "own")
                args, where = [], []
                join = ""
                if view in ("reactions", "quotes"):
                    join = " join targets t on t.hash=c.hash"
                    where.append(
                        "t.labels like ?" if view == "quotes" else "t.labels != ?"
                    )
                    if view == "quotes":
                        args.append('%"Quoted"%')
                    else:
                        args.append(json.dumps(["Quoted"]))
                elif view == "own":
                    where.append("c.fid=?")
                    args.append(self.fid)
                elif view != "all":
                    raise ValueError("Unknown cast view")
                if search:
                    where.append("c.search like ? escape '\\'")
                    args.append(like)
                for key, op in (("from", ">="), ("to", "<")):
                    if value(key):
                        where.append("c.stamp" + op + "?")
                        args.append(int(value(key)))
                clause = " where " + " and ".join(where) if where else ""
                total = db.execute(
                    "select count(*) from casts c" + join + clause, args
                ).fetchone()[0]
                order = "desc" if value("order") == "desc" else "asc"
                rows = db.execute(
                    "select c.* from casts c"
                    + join
                    + clause
                    + f" order by c.stamp {order},c.hash {order} limit ? offset ?",
                    (*args, limit, offset),
                )
                items = [self.cast(db, r) for r in rows]
                return dict(
                    total=total,
                    items=items,
                    next=offset + len(items) if offset + len(items) < total else None,
                )
            if route == "/api/cast":
                row = db.execute(
                    "select * from casts where hash=?", (value("hash"),)
                ).fetchone()
                if value("raw") == "1":
                    return (
                        json.loads(zlib.decompress(row["raw"]))
                        if row
                        else dict(unavailable=True, hash=value("hash"))
                    )
                result = self.cast(db, row)
                ancestors, seen = [], {value("hash")}
                h = result.get("parent") if result else None
                while h and h not in seen:
                    seen.add(h)
                    parent = db.execute(
                        "select * from casts where hash=?", (h,)
                    ).fetchone()
                    if not parent:
                        ancestors.append(dict(hash=h, unavailable=True))
                        break
                    ancestors.append(self.cast(db, parent))
                    h = parent["parent"]
                return dict(cast=result, ancestors=list(reversed(ancestors)))
            if route == "/api/thread":
                h = value("hash")
                row = db.execute("select * from casts where hash=?", (h,)).fetchone()
                root = row["thread"] if row else h
                rows = db.execute(
                    "select * from casts where thread=? order by stamp,hash", (root,)
                )
                # Each thread loads once. Render its branches in bounded batches in the browser.
                return dict(root=root, focus=h, items=[self.cast(db, r) for r in rows])
            if route == "/api/contacts":
                clause, args = ["search like ? escape '\\'"], [like]
                if value("relationship") in ("followers", "following"):
                    clause.append(
                        "following=1"
                        if value("relationship") == "following"
                        else "follower=1"
                    )
                where = " where " + " and ".join(clause)
                total = db.execute(
                    "select count(*) from contacts" + where, args
                ).fetchone()[0]
                rows = db.execute(
                    "select view from contacts"
                    + where
                    + " order by fid limit ? offset ?",
                    (*args, limit, offset),
                )
                items = [json.loads(r[0]) for r in rows]
                return dict(
                    total=total,
                    items=items,
                    next=offset + len(items) if offset + len(items) < total else None,
                )
            if route == "/api/media":
                clause, args = ["url like ? escape '\\'"], [like]
                if value("status"):
                    clause.append("status=?")
                    args.append(value("status"))
                if value("own") == "1":
                    clause.append("own=1")
                where = " where " + " and ".join(clause)
                total = db.execute(
                    "select count(*) from media" + where, args
                ).fetchone()[0]
                rows = db.execute(
                    "select view from media" + where + " order by id limit ? offset ?",
                    (*args, limit, offset),
                )
                items = [media_preview(json.loads(r[0])) for r in rows]
                return dict(
                    total=total,
                    items=items,
                    next=offset + len(items) if offset + len(items) < total else None,
                )
            if route == "/api/missing-targets":
                rows = db.execute(
                    "select t.* from targets t left join casts c on c.hash=t.hash where c.hash is null order by t.stamp,t.hash"
                )
                return [dict(r) for r in rows]
        raise ValueError("Unknown API route")


class Handler(BaseHTTPRequestHandler):
    server_version = "FarcasterArchive/1"

    def send_headers(self, status, kind, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        self.serve(False)

    def do_HEAD(self):
        self.serve(True)

    def serve(self, head):
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        parsed = urllib.parse.urlsplit(self.path)
        route = urllib.parse.unquote(parsed.path)
        viewer = self.server.viewer
        try:
            if route.startswith("/api/"):
                data = json.dumps(
                    viewer.api(route, urllib.parse.parse_qs(parsed.query)),
                    ensure_ascii=False,
                ).encode()
                self.send_headers(200, "application/json; charset=utf-8", len(data))
                if not head:
                    self.wfile.write(data)
                return
            extra = {}
            if route in ("/", "/app.js", "/style.css"):
                file = viewer.static / ("index.html" if route == "/" else route[1:])
                kind = mimetypes.guess_type(file)[0] or "text/plain"
            elif route.startswith("/files/"):
                file = viewer.exports[route.removeprefix("/files/")]
                kind = "application/octet-stream"
                extra["Content-Disposition"] = (
                    'attachment; filename="' + file.name + '"'
                )
            elif route.startswith("/saved/"):
                relative = route.removeprefix("/saved/")
                if not relative.startswith("media/") or relative not in viewer.manifest:
                    raise KeyError(relative)
                file = (viewer.archive / relative).resolve()
                if not file.is_relative_to((viewer.archive / "media").resolve()):
                    raise KeyError(relative)
                kind = mimetypes.guess_type(file)[0] or "application/octet-stream"
                if file.suffix not in (
                    ".m3u8",
                    ".ts",
                    ".m4s",
                    ".mp4",
                    ".aac",
                    ".m4a",
                    ".mp3",
                    ".vtt",
                    ".key",
                ):
                    with closing(viewer.db()) as db:
                        row = db.execute(
                            "select kind from media where path=? and status='downloaded'",
                            (relative,),
                        ).fetchone()
                    if row and row[0].startswith(("image/", "video/", "audio/")):
                        kind = row[0]
                if kind in ("text/html", "application/javascript", "image/svg+xml"):
                    kind = "application/octet-stream"
                    extra["Content-Disposition"] = "attachment"
            else:
                raise KeyError(route)
            size = file.stat().st_size
            start, end, status = 0, size - 1, 200
            if self.headers.get("Range"):
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"])
                if not match or not any(match.groups()):
                    self.send_headers(
                        416, kind, 0, {"Content-Range": f"bytes */{size}"}
                    )
                    return
                a, b = match.groups()
                start = int(a) if a else max(0, size - int(b))
                end = min(size - 1, int(b)) if a and b else size - 1
                if start >= size or end < start:
                    self.send_headers(
                        416, kind, 0, {"Content-Range": f"bytes */{size}"}
                    )
                    return
                status, extra["Content-Range"] = 206, f"bytes {start}-{end}/{size}"
            extra["Accept-Ranges"] = "bytes"
            self.send_headers(status, kind, end - start + 1, extra)
            if not head:
                with file.open("rb") as fp:
                    fp.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = fp.read(min(remaining, 256 * 1024))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (KeyError, FileNotFoundError):
            self.send_error(404)
        except ValueError:
            self.send_error(400)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, required=True, help="Sealed cast snapshot folder"
    )
    parser.add_argument(
        "--contacts",
        type=Path,
        help="Sealed contact snapshot folder (defaults to --archive)",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        required=True,
        help="Derived SQLite index outside the snapshots",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--open", action="store_true", help="Open the local page after startup"
    )
    parser.add_argument(
        "--reports",
        type=Path,
        help="Optional folder with additional CSV export reports",
    )
    args = parser.parse_args()
    archive, contacts, cache = (
        args.archive.resolve(),
        (args.contacts or args.archive).resolve(),
        args.cache.resolve(),
    )
    if cache.is_relative_to(archive) or cache.is_relative_to(contacts):
        parser.error("The index must stay outside the sealed snapshots")
    os.umask(0o077)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.with_suffix(".lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(
                "This viewer index is already in use. Open the existing local viewer, or wait for it to finish starting."
            )
        prepare_index(archive, contacts, cache)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.viewer = Viewer(archive, contacts, cache, args.reports)
        print(f"Local viewer: http://127.0.0.1:{server.server_port}", flush=True)
        if args.open:
            webbrowser.open(f"http://127.0.0.1:{server.server_port}/#own")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
