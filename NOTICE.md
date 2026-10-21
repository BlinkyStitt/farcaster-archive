# Upstream attribution

Farcaster Archive is distributed under GNU GPL version 3. Copyright (C) 2026 Bryan Stitt and contributors.

The embedded `WIRE_SCHEMA` descriptor set and generated decoders in `tests/message_pb2.py` and `tests/username_proof_pb2.py` derive from the official [Snapchain protobuf definitions](https://github.com/farcasterxyz/snapchain/tree/fc87f2686472598143062a00e1c2b2633b2d87ab/proto/definitions), revision `fc87f2686472598143062a00e1c2b2633b2d87ab`. Snapchain identifies them as GPL-3.0; its original license is retained in `tests/LICENSE-SNAPCHAIN`.

The optional `fc` CLI is built separately from that revision with adjacent Malachite revision `13bca14cd209d985c3adf101a02924acde8723a5`. This repository does not vendor their source or binaries. Dependencies retain their own licenses.

Signing tests use the public BIP39 `abandon ... about` vector and deterministic Ed25519 fixture bytes. These are public test data, not private credentials. Never use them for real accounts.
