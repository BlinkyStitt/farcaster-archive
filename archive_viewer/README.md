# Local archive viewer

Run `python3 archive_viewer/server.py --archive /path/to/sealed-snapshot --cache /path/outside/snapshots/index.sqlite --open`, then visit <http://127.0.0.1:8765>. Keep the server window open while reading. Python 3.12 or newer runs it; no extra packages or API keys are needed. `launch.command` accepts the same arguments and uses `PYTHON_BIN` or `python3`. Contacts default to the cast snapshot; a separate `--contacts` snapshot must have the same FID.

The default page shows all surviving authored casts and replies in ascending message timestamp order, with a stable hash tie break. Scroll to load more, or use the button. Search, date filters in your browser's local timezone, and newest-first order are available. A reply includes its immediate parent preview; select it to read all obtainable ancestors. Open a full thread to see all archived conversation records grouped beneath their parents, with siblings in timestamp order. Your selected cast has a purple border. Large branches load into the page in batches; a selected reply stays visible even when earlier siblings are collapsed. Missing parents and roots stay visible as gaps.

Other sections show every saved cast, obtainable liked/recast/quoted targets, retained reaction-removal labels, missing reaction targets, local media and unresolved URLs, searchable follower/following contacts, original JSON/CSV/Markdown exports, and additional media-recovery CSV reports. Cast cards can show the original complete source metadata. Public profile links and provider-verified contact fields retain their provenance. Original public links open only when clicked. The viewer does not fetch remote images, avatars, frames, or API data.

Images and supported audio/video play from saved files. Local finite HLS playlists use native browser playback, including Safari and supported Chrome on macOS. Browsers without native HLS show a playlist link for a compatible player. Unsupported formats remain downloadable. Media responses support byte ranges and manifest-listed local HLS resources. No external player scripts or services are used.

The server binds only to `127.0.0.1` and checks the Host header. Only GET/HEAD routes exist; no signer or deletion code is imported. File routes allow only selected exports and manifest-listed media. Untrusted cast text and names are inserted as text, never HTML. A content security policy blocks remote resources and embedded pages.

The SQLite file is a derived search index outside the sealed snapshots. The index builder streams the large cast export, verifies the checksums of the files it uses, checks total and authored counts, and atomically replaces the index after success. It keeps complete source records compressed for the metadata view. It reuses an index only for the same pair of source manifests. Source snapshots are read only and remain separate from deletion state. Viewing this archive does not accept content gaps or authorize any deletion.

To use other snapshots:

```sh
python archive_viewer/server.py --archive /path/to/cast-snapshot \
  --contacts /path/to/contact-snapshot --cache /path/outside/snapshots/index.sqlite \
  --reports /optional/csv-report-folder --port 8765
```

Run the viewer tests with the your Python environment (host access is needed for the local HTTP test):

```sh
python -m unittest discover -s tests -p test_viewer.py -v
node --check archive_viewer/app.js
```
