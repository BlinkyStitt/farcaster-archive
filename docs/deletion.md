# Optional account deletion

Export and inspect your archive first. Deletion removes every surviving cast and reply in the plan. It keeps local copies and cannot remove other authors’ casts or copies held by other parties.

The current deletion flow supports a recovery phrase that derives the custody address at `m/44'/60'/0'/0/0`. It does not support hardware-wallet prompts, smart-contract custody signatures, other derivation paths, or hosted wallets without an exportable phrase. A mismatch stops before signing. Never enter recovery words in chat, a shell command, an issue, or a file. Use only the hidden local prompt.

## Official CLI

Deletion uses the official Rust `fc` client. Build the pinned source with Rust/Cargo, the upstream native build prerequisites, and `protoc`. Snapchain needs the adjacent Malachite checkout for its workspace path dependencies. Use a build directory outside this repository:

```sh
mkdir -p "$HOME/.cache/farcaster-cli"
cd "$HOME/.cache/farcaster-cli"
git clone https://github.com/farcasterxyz/snapchain.git snapchain
git -C snapchain checkout --detach fc87f2686472598143062a00e1c2b2633b2d87ab
git clone https://github.com/informalsystems/malachite.git malachite
git -C malachite checkout --detach 13bca14cd209d985c3adf101a02924acde8723a5
cargo build --locked --release --manifest-path snapchain/Cargo.toml -p fc-cli
export FC_BIN="$PWD/snapchain/target/release/fc"
export CLI_SOURCE="$PWD/snapchain"
export MALACHITE_SOURCE="$PWD/malachite"
```

Do not substitute an arbitrary binary if the pinned build fails. Return to this tool’s checkout with FID, OUT, and PYTHON_BIN still set. Create the local build receipt:

```sh
python - <<'PY'
import hashlib, json, os, pathlib, subprocess
source = pathlib.Path(os.environ['CLI_SOURCE'])
malachite = pathlib.Path(os.environ['MALACHITE_SOURCE'])
revision = 'fc87f2686472598143062a00e1c2b2633b2d87ab'
malachite_revision = '13bca14cd209d985c3adf101a02924acde8723a5'
for folder, expected in ((source, revision), (malachite, malachite_revision)):
    assert subprocess.check_output(['git', '-C', str(folder), 'rev-parse', 'HEAD'], text=True).strip() == expected
    assert not subprocess.check_output(['git', '-C', str(folder), 'status', '--porcelain'], text=True).strip()
binary = pathlib.Path(os.environ['FC_BIN']).resolve()
receipt = dict(revision=revision, malachite_revision=malachite_revision,
               binary=str(binary), sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
output = pathlib.Path(os.environ['OUT']) / 'cli-build.json'
with output.open('x') as file:
    json.dump(receipt, file, indent=2)
output.chmod(0o600)
PY
python -m pip install -r requirements-broadcast.txt
FC_BIN="$FC_BIN" python -m unittest discover -s tests -p test_official_cli.py -v
```

These tests use public fixture keys and send only to loopback. They verify payloads and signatures, not live network acceptance.

## Review and broadcast

Run `./farcaster_cleanup_all.sh --verify-backup`. Inspect the viewer, `summary.json`, and `issues.json`. Corrupt files, missing authored records, inventory changes, and failed reaction history block deletion.

A complete backup needs no gap receipt. For a partial backup, only `unresolved-embed`, `unavailable-cast`, `missing-parent`, and `neynar-conversation-unavailable` issues can receive explicit acceptance. The archive remains incomplete after acceptance.

If you accept those exact recorded gaps, create `OUT/reviewed-backup.json` yourself with this schema. Use the selected snapshot’s actual FID, file hashes, and issue count:

```json
{
  "schema": 1,
  "fid": 12345,
  "decision": "accept-recorded-content-gaps",
  "manifest_sha256": "SHA256 of the selected snapshot manifest.json",
  "issues_sha256": "SHA256 of that snapshot issues.json",
  "issue_count": 1
}
```

This example is not a valid receipt. Do not copy another account’s receipt or accept gaps you have not reviewed. The script never generates acceptance automatically. A new snapshot needs a new review; acceptance does not bypass integrity checks.

```sh
./farcaster_cleanup_all.sh --broadcast
```

The command checks every sealed file, account identity, archived versus live custody, distinct peers, all three inventories, and the CLI receipt before input. Type the exact `DELETE ALL <count> CASTS FOR FID <fid>` line. Then enter recovery words at the hidden second prompt if signer registration or renewal is needed.

The phrase and derived custody key are not saved. A short-lived CLI child receives the phrase through its environment; processes with your OS privileges can inspect memory or the environment. Use a trusted local machine. The Ed25519 signer stays in `OUT/secrets/signer.json` with private permissions, CAST_REMOVE-only scope, and a 24-hour TTL. Keep that private directory for resumption.

Each signed payload and submission intent is saved before one protocol POST. The first deletion must propagate to both peers before the second. Every later deletion receives the same check. Final inventories must contain zero authored casts on both peers and Neynar.

If a submission is uncertain, do not remove journals or launch parallel copies. Check both nodes. A later run with the same OUT can confirm an attempted write, but never automatically resubmits that journal. A rejected or missing write can require manual investigation. The output lock prevents parallel runs. Changed custody or new unarchived casts require a new backup and review.
