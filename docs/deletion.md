# Delete casts after export

This deletes every surviving cast and reply in the backup plan. Local files remain.

The recovery words must derive the custody address at `m/44'/60'/0'/0/0`. Hardware wallets, smart-contract custody, other derivation paths, and hosted wallets without exportable recovery words are not supported. Enter recovery words only at the hidden local prompt.

## 1. Select the backup

Replace `12345` with the FID you exported:

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

If this reports content gaps, review them in step 3. If it reports corrupt or missing backup files, stop and repair the backup.

## 2. Build the official CLI

Requires Rust/Cargo, C/C++ build tools, and `protoc`. This builds the pinned CLI outside the repository, creates `OUT/cli-build.json`, and runs its loopback signing tests. Run this block once after step 1:

```sh
bash <<'BUILD'
set -euo pipefail
cargo --version
protoc --version
build="$HOME/.cache/farcaster-cli"
mkdir -p "$build"
git clone https://github.com/farcasterxyz/snapchain.git "$build/snapchain"
git -C "$build/snapchain" checkout --detach fc87f2686472598143062a00e1c2b2633b2d87ab
git clone https://github.com/informalsystems/malachite.git "$build/malachite"
git -C "$build/malachite" checkout --detach 13bca14cd209d985c3adf101a02924acde8723a5
CARGO_TARGET_DIR="$build/target" cargo build --locked --release \
  --manifest-path "$build/snapchain/Cargo.toml" -p fc-cli
export CLI_SOURCE="$build/snapchain"
export MALACHITE_SOURCE="$build/malachite"
export FC_BIN="$build/target/release/fc"
"$PYTHON_BIN" - <<'PY'
import hashlib, json, os, pathlib, subprocess
os.umask(0o077)
revision = 'fc87f2686472598143062a00e1c2b2633b2d87ab'
malachite_revision = '13bca14cd209d985c3adf101a02924acde8723a5'
for folder, expected in ((os.environ['CLI_SOURCE'], revision),
                         (os.environ['MALACHITE_SOURCE'], malachite_revision)):
    head = subprocess.check_output(['git', '-C', folder, 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', folder, 'status', '--porcelain'], text=True).strip()
    if head != expected or dirty:
        raise SystemExit('CLI source differs from the pinned clean checkout')
binary = pathlib.Path(os.environ['FC_BIN']).resolve()
receipt = dict(revision=revision, malachite_revision=malachite_revision,
               binary=str(binary), sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
output = pathlib.Path(os.environ['OUT']) / 'cli-build.json'
with output.open('x') as file:
    json.dump(receipt, file, indent=2)
output.chmod(0o600)
print('CLI receipt:', output)
PY
"$PYTHON_BIN" -m unittest discover -s tests -p test_official_cli.py -v
BUILD
```

The build folders must not already exist. For a later deletion session, reuse the saved receipt; do not rebuild. If you already set `FC_BIN` in your shell, unset it to use the receipt’s binary:

```sh
unset FC_BIN
```

## 3. Review content gaps

Skip this step if backup verification passed without gaps. Otherwise, inspect the selected snapshot:

```sh
SNAPSHOT="$(python -c 'import json,os,pathlib; r=pathlib.Path(os.environ["OUT"]); print(r/json.loads((r/"snapshot.json").read_text())["path"])')"
python -m json.tool "$SNAPSHOT/summary.json"
python -m json.tool "$SNAPSHOT/issues.json" | less
```

Press **Q** to leave `less`. Only missing media, unavailable casts or conversations, and missing parents can receive acceptance. The archive stays marked incomplete.

After reading the gaps, run this block. It calculates the receipt from the selected snapshot and asks for explicit acceptance through your terminal:

```sh
python - <<'PY'
import collections, hashlib, json, os
from pathlib import Path
os.umask(0o077)
root = Path(os.environ['OUT']).expanduser().resolve()
fid = int(os.environ['FID'])
pointer = json.loads((root / 'snapshot.json').read_text())
snapshot = (root / pointer['path']).resolve()
if not snapshot.is_relative_to(root / 'snapshots'):
    raise SystemExit('Invalid snapshot path')
manifest_hash = hashlib.sha256((snapshot / 'manifest.json').read_bytes()).hexdigest()
if manifest_hash != pointer['manifest_sha256']:
    raise SystemExit('Snapshot manifest changed')
manifest = json.loads((snapshot / 'manifest.json').read_text())
for name in ('summary.json', 'issues.json'):
    if hashlib.sha256((snapshot / name).read_bytes()).hexdigest() != manifest['files'].get(name):
        raise SystemExit(f'Backup checksum mismatch: {name}')
summary = json.loads((snapshot / 'summary.json').read_text())
if summary['fid'] != fid:
    raise SystemExit('Snapshot belongs to another FID')
if summary['complete']:
    raise SystemExit('Backup is complete; no gap acceptance is needed')
issues_bytes = (snapshot / 'issues.json').read_bytes()
issues = json.loads(issues_bytes)
allowed = {'unresolved-embed', 'unavailable-cast', 'missing-parent',
           'neynar-conversation-unavailable'}
if not issues or any(item.get('kind') not in allowed for item in issues):
    raise SystemExit('Critical or unclassified gaps block deletion')
print('Snapshot:', snapshot)
print('Manifest:', manifest_hash)
print('Gaps:', dict(collections.Counter(item['kind'] for item in issues)))
confirmation = f'ACCEPT GAPS FOR FID {fid}'
with open('/dev/tty', 'w') as tty:
    tty.write(f'Type {confirmation} to accept these recorded gaps: ')
    tty.flush()
with open('/dev/tty', 'r') as tty:
    if tty.readline().strip() != confirmation:
        raise SystemExit('No acceptance recorded')
receipt = dict(schema=1, fid=fid, decision='accept-recorded-content-gaps',
               manifest_sha256=manifest_hash,
               issues_sha256=hashlib.sha256(issues_bytes).hexdigest(), issue_count=len(issues))
output = root / 'reviewed-backup.json'
temporary = root / 'reviewed-backup.json.partial'
temporary.write_text(json.dumps(receipt, indent=2) + '\n')
temporary.chmod(0o600)
temporary.replace(output)
print('Saved:', output)
PY
```

The broadcast command still checks every backup file. Acceptance applies only to this snapshot and these recorded gaps.

## 4. Delete

```sh
cd "$HOME/farcaster-archive"
FID=12345 OUT="$HOME/Farcaster/fid12345" \
  NEYNAR_KEY_FILE="$HOME/.config/neynar_api_key" \
  PYTHON_BIN="$PWD/.venv/bin/python" \
  ./farcaster_cleanup_all.sh --broadcast
```

Replace `12345` in both places. The prompt appears after backup and network checks:

1. Type the exact `DELETE ALL <count> CASTS FOR FID <fid>` line shown.
2. Enter the custody recovery words at the hidden prompt if signer registration or renewal is needed.

The command verifies each deletion on two distinct peers, then requires zero casts in both peers’ and Neynar’s inventories. The phrase and custody key are not saved. A short-lived CLI process receives the phrase through its environment; use a trusted machine. Keep `OUT/secrets/signer.json` private for resumption.

## 5. Check completion or resume

In a new Bash terminal, replace `12345` with the same FID:

```sh
export FID=12345
export OUT="$HOME/Farcaster/fid$FID"
python3 -m json.tool "$OUT/state/fid$FID/complete.json"
```

Successful completion records `deleted_count` and `source_counts: [0, 0, 0]`. If `complete.json` is absent, read the last terminal error. To resume after an interruption, repeat step 4 with the same OUT. Do not remove submission journals or run parallel copies.

An uncertain submission stops without automatic resubmission. If it does not propagate to both nodes, stop and investigate. Changed custody or new unarchived casts require a new backup.
