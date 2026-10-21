# Farcaster Archive

Export casts, threads, media, followers, and following. Browse the backup locally, then optionally delete your casts.

Requires macOS or Linux, Bash, Python 3.12+, and a Neynar API key. On Windows, use WSL. Run the commands below in Bash.

## 1. Install

```sh
git clone https://github.com/BlinkyStitt/farcaster-archive.git "$HOME/farcaster-archive"
cd "$HOME/farcaster-archive"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## 2. Save your API key

Get a key from the [Neynar Developer Portal](https://dev.neynar.com/). Open a private key file:

```sh
mkdir -p "$HOME/.config"
(umask 077; touch "$HOME/.config/neynar_api_key")
chmod 600 "$HOME/.config/neynar_api_key"
nano "$HOME/.config/neynar_api_key"
```

Paste the key on one line. Press **Ctrl+O**, **Enter**, then **Ctrl+X**.

## 3. Export

Replace `12345` with your numeric Farcaster ID:

```sh
cd "$HOME/farcaster-archive"
. .venv/bin/activate
export FID=12345
export OUT="$HOME/Farcaster/fid$FID"
export NEYNAR_KEY_FILE="$HOME/.config/neynar_api_key"
export PYTHON_BIN="$PWD/.venv/bin/python"
./farcaster_cleanup_all.sh --dry-run
```

`--dry-run` downloads the archive without changing your account. Repeat it with the same FID and OUT to resume. Each OUT belongs to one FID.

If the command ends with `Partial backup preserved`, open the viewer anyway. Missing media, parents, and API records stay listed in `issues.json`; their URLs and references remain saved.

## 4. Open the viewer

This block also works in a new terminal. Use the same FID as the export:

```sh
cd "$HOME/farcaster-archive"
. .venv/bin/activate
export FID=12345
export OUT="$HOME/Farcaster/fid$FID"
SNAPSHOT="$(python -c 'import json,os,pathlib; r=pathlib.Path(os.environ["OUT"]); print(r/json.loads((r/"snapshot.json").read_text())["path"])')"
python archive_viewer/server.py --archive "$SNAPSHOT" \
  --cache "$OUT/viewer/index.sqlite" --open
```

Open [127.0.0.1:8765](http://127.0.0.1:8765/#own). Keep the terminal open. Press **Ctrl+C** to stop the viewer.

The viewer shows oldest-to-newest casts, threaded replies, media, contacts, and export downloads. It reads local files and cannot delete casts. See [viewer options](archive_viewer/README.md).

## 5. Optional: delete your casts

This removes all surviving casts and replies in the backup plan. Local backups, follows, likes, recasts, and profile data remain.

Use the same FID and OUT as the export:

```sh
cd "$HOME/farcaster-archive"
. .venv/bin/activate
export FID=12345
export OUT="$HOME/Farcaster/fid$FID"
export NEYNAR_KEY_FILE="$HOME/.config/neynar_api_key"
export PYTHON_BIN="$PWD/.venv/bin/python"
python -m pip install -r requirements-broadcast.txt
./farcaster_cleanup_all.sh --verify-backup
```

Build the official CLI and create its receipt with [these commands](docs/deletion.md#2-build-the-official-cli). If verification reports content gaps, use the [review commands](docs/deletion.md#3-review-content-gaps) before continuing. Corrupt or missing backup files block deletion.

Then run:

```sh
cd "$HOME/farcaster-archive"
FID=12345 OUT="$HOME/Farcaster/fid12345" \
  NEYNAR_KEY_FILE="$HOME/.config/neynar_api_key" \
  PYTHON_BIN="$PWD/.venv/bin/python" \
  ./farcaster_cleanup_all.sh --broadcast
```

Replace `12345` in **both** places. After the checks finish, type the exact `DELETE ALL ...` line shown. Enter recovery words only at the hidden second prompt. Deletion checks every target on two distinct peers and checks that all three final inventories are empty.

See [deletion steps and supported wallets](docs/deletion.md).

## Saved files

- `own_casts.json`, `.csv`, `.md`: your casts and replies.
- `casts.json`, `.csv`, `.md`: casts plus available conversation context.
- `media.json` and `media/`: downloads, HLS bundles, original URLs, and failures.
- `contacts*.json`, `.csv`, `.md`: names, bios, social accounts, links, and wallets; followers and following stay separate.
- `reactions.json`, `reaction_history.json`, `threads.json`: available activity and context.
- `issues.json`, `summary.json`, `manifest.json`: gaps, counts, and checksums.

Deleted or unavailable history may be missing. Provider-verified contact fields retain their source labels; email-like bio text stays unverified.

## Configuration

| Variable | Default / purpose |
| --- | --- |
| `FID` | Required account ID, from 1 through `2**63-1` |
| `OUT` | `./farcaster-archive/fid<FID>` |
| `PYTHON_BIN` | `python3` |
| `NEYNAR_KEY_FILE` | `~/.config/neynar_api_key` |
| `SNAPCHAIN_NODE` | `https://haatz.quilibrium.com` (Hypersnap) |
| `SECONDARY_NODE` | `https://snapchain-api.neynar.com` (Snapchain) |
| `OP_RPC_URL` | `https://mainnet.optimism.io` |
| `FC_BIN` | Optional override for the CLI path in `OUT/cli-build.json` |
| `THREAD_MAX_NODES` | `250000`; reaching the limit records a gap |
| `MEDIA_MAX_BYTES` | `536870912` per media/HLS bundle |

Neynar reads run at most four requests per second and honor `Retry-After`. Cast search waits at least 1.01 seconds per request. See [Neynar rate limits](https://docs.neynar.com/reference/what-are-the-rate-limits-on-neynar-apis).

## Tests

```sh
python -m pip install -r requirements-dev.txt
python tools/check_python.py
python -m unittest discover -s tests -v
bash -n farcaster_cleanup_all.sh
shellcheck farcaster_cleanup_all.sh archive_viewer/launch.command
node --check archive_viewer/app.js
```

CLI tests use public fixture keys and loopback only; set `FC_BIN` to run them. The controlling-terminal test runs on macOS. CI covers macOS/Linux and Python 3.12/3.14.

[GPL-3.0](LICENSE) · [Upstream attribution](NOTICE.md)
