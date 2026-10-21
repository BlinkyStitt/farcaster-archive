# Farcaster Archive

Export a Farcaster account to checksum-sealed local files. Read casts and conversation threads in a local browser. Optionally delete the account’s surviving casts after you review the backup.

Choose an account with `FID`; no source edits or account name are required. Export needs a Neynar API key, but no wallet, recovery phrase, or signer. Deletion needs control of the custody wallet.

## Export

Use macOS or Linux with Bash and Python 3.12 or newer. Native Windows is not supported; use WSL. API access and available history depend on your providers.

```sh
git clone https://github.com/BlinkyStitt/farcaster-archive.git
cd farcaster-archive
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Save your Neynar API key in a private file outside this repository. The default is `~/.config/neynar_api_key`. Do not put the key in a shell command, committed file, or GitHub issue.

Replace `12345` with the account’s FID:

```sh
export FID=12345
export OUT="$HOME/Farcaster/fid$FID"
export NEYNAR_KEY_FILE="$HOME/.config/neynar_api_key"
export PYTHON_BIN="$(command -v python)"
./farcaster_cleanup_all.sh --dry-run
```

`--dry-run` makes real read requests and downloads files. It sends no Farcaster protocol writes. Content gaps make the command exit nonzero, but it preserves the partial snapshot and issues. Keep the same FID and OUT to resume. An output directory belongs to one account; another FID cannot reuse its caches or journals. An unbound, nonempty directory is also rejected. Default OUT: `./farcaster-archive/fid<FID>`.

Exports can use substantial API credits, bandwidth, and disk space. Rate limits and credits are separate. The script does not infer your plan or remaining credits. Neynar HTTP and gRPC reads share a no-burst limit of four requests per second; cast search waits at least 1.01 seconds. This stays below the published standard limits, including grandfathered Starter limits. HTTP retries honor `Retry-After`, including dates and long delays. Public media uses a separate two-request-per-second limit per origin. See [Neynar rate limits](https://docs.neynar.com/reference/what-are-the-rate-limits-on-neynar-apis).

## Viewer

Resolve the current snapshot, then start the local viewer:

```sh
SNAPSHOT="$(python -c 'import json,os,pathlib; r=pathlib.Path(os.environ["OUT"]); print(r/json.loads((r/"snapshot.json").read_text())["path"])')"
python archive_viewer/server.py --archive "$SNAPSHOT" \
  --cache "$OUT/viewer/index.sqlite" --open
```

Open [the local viewer](http://127.0.0.1:8765/#own). It shows oldest-to-newest casts, parent context, threaded replies, local media, contacts, and export downloads. It reads the FID from the archive. Use `--contacts /path/to/snapshot` for a separate contact snapshot for the same FID. See [viewer details](archive_viewer/README.md).

The viewer needs only Python’s standard library. It binds only to loopback, has no deletion routes, and fetches no remote images or APIs. Keep its window open while reading. Keep private exports off public servers.

## Export contents

- Your surviving casts and replies, original hashes, and full source metadata in JSON, CSV, and Markdown.
- Available conversation ancestors, replies, quoted casts, and reaction targets. Retained reaction removal history stays separate from current likes and recasts.
- Downloadable media and finite HLS bundles. Failed downloads keep their original URLs, referring cast hashes, and errors.
- Separate follower and following exports, plus a deduplicated contact catalog: public names, bios, social accounts, published links, and verified wallet addresses returned by providers. Verification labels identify provider assertions; email-like bio text remains unverified.
- Raw API pages, available original protocol messages, issues, a deletion plan, and a SHA-256 manifest. Saved original bytes are checked, but full signed-byte coverage is optional.

Unavailable history cannot always be recovered. Missing ancestors, media failures, API denials, and source disagreements stay visible in `issues.json`; the tool does not assume their cause. A partial archive stays incomplete. The export cannot guarantee recovery of all historical activity.

```sh
./farcaster_cleanup_all.sh --verify-backup
```

This command makes no network requests. It checks checksums and archive references. A partial backup still exits nonzero and reports its gaps.

## Optional deletion

Read [the deletion instructions](docs/deletion.md) first. `--broadcast` removes all surviving casts and replies in the reviewed account plan and keeps local copies. It does not remove likes, recasts, follows, or profile data.

Deletion checks current custody against the archive, rejects unarchived new casts, requires exact local confirmation, verifies each deletion on two distinct peers, and requires zero remaining casts on both peers and Neynar. An uncertain write stops without automatic resubmission. Export never needs wallet secrets.

## Configuration

| Variable | Default / purpose |
| --- | --- |
| `FID` | Required positive account ID, at most `2**63-1` |
| `OUT` | `./farcaster-archive/fid<FID>`; keep private and outside Git |
| `PYTHON_BIN` | `python3`; use your virtual environment’s Python |
| `NEYNAR_KEY_FILE` | `~/.config/neynar_api_key` |
| `SNAPCHAIN_NODE` | `https://haatz.quilibrium.com` (Hypersnap) |
| `SECONDARY_NODE` | `https://snapchain-api.neynar.com` (Snapchain) |
| `OP_RPC_URL` | `https://mainnet.optimism.io`, for public custody checks |
| `FC_BIN` | Pinned official CLI binary, for deletion and CLI tests |
| `THREAD_MAX_NODES` | `250000`; reaching this bound records a gap |
| `MEDIA_MAX_BYTES` | `536870912`, maximum bytes per media/HLS bundle |

Custom endpoints must implement the expected Farcaster APIs. Public service availability can change.

## Tests and license

```sh
python -m pip install -r requirements-broadcast.txt
python -m unittest discover -s tests -v
bash -n farcaster_cleanup_all.sh
shellcheck farcaster_cleanup_all.sh archive_viewer/launch.command
node --check archive_viewer/app.js
```

Tests use synthetic fixtures and public deterministic signing vectors. Two optional CLI tests need `FC_BIN`; they send only to a loopback receiver. The real controlling-terminal test runs on macOS. CI runs the portable suite on macOS and Linux. These tests do not prove live deletion for a new account.

GPL-3.0; see [LICENSE](LICENSE) and [upstream attribution](NOTICE.md). Keep exports, API keys, signer secrets, and task logs out of Git.
