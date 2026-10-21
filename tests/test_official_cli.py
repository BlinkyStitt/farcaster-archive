"""Run the real pinned CLI against loopback only, using a public BIP39 test vector."""

import os
import types
from pathlib import Path
import tempfile
import unittest
import unittest.mock

import blake3
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak
import message_pb2 as proto

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


class OfficialCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE / "tests")
        self.path = Path(self.tmp.name)
        self.fc = Path(os.environ.get("FC_BIN", "/nonexistent/fc"))
        self.secret = bytes(
            range(32)
        )  # Public deterministic test key, never registered.
        self.pub = (
            Ed25519PrivateKey.from_private_bytes(self.secret)
            .public_key()
            .public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("MNEMONIC", "SIGNER_SECRET", "NEYNAR_API_KEY", "X_USER_TOKEN")
        }
        self.env["SIGNER_SECRET"] = "0x" + self.secret.hex()

    def tearDown(self):
        self.tmp.cleanup()

    def message(self, filename):
        m = proto.Message.FromString(filename.read_bytes())
        self.assertEqual(m.data.fid, 12345)
        self.assertEqual(m.data.network, proto.FARCASTER_NETWORK_MAINNET)
        self.assertEqual(m.signer, self.pub)
        self.assertEqual(
            m.hash, blake3.blake3(m.data.SerializeToString()).digest()[:20]
        )
        self.assertEqual(m.hash_scheme, proto.HASH_SCHEME_BLAKE3)
        self.assertEqual(m.signature_scheme, proto.SIGNATURE_SCHEME_ED25519)
        Ed25519PublicKey.from_public_bytes(m.signer).verify(m.signature, m.hash)
        return m

    def test_real_cast_remove_contains_exact_target_and_valid_signature(self):
        if not self.fc.is_file():
            self.skipTest(
                "Set FC_BIN to the pinned official CLI to run loopback signing tests"
            )
        target = "0x" + "ab" * 20
        file = c.capture_cli(
            self.fc,
            ["cast-remove", "--fid", "12345", "--target-hash", target],
            self.env,
            self.path / "remove.bin",
        )
        m = self.message(file)
        self.assertEqual(m.data.type, proto.MESSAGE_TYPE_CAST_REMOVE)
        self.assertEqual(m.data.WhichOneof("body"), "cast_remove_body")
        self.assertEqual(m.data.cast_remove_body.target_hash.hex(), target[2:])

    def test_real_key_add_has_remove_only_scope_and_custody_auth(self):
        if not self.fc.is_file():
            self.skipTest(
                "Set FC_BIN to the pinned official CLI to run loopback signing tests"
            )
        # Canonical public BIP39 vector. Never use this public test vector for a real account.
        self.env["MNEMONIC"] = "abandon " * 11 + "about"
        Account.enable_unaudited_hdwallet_features()
        wallet = Account.from_mnemonic(
            self.env["MNEMONIC"], account_path="m/44'/60'/0'/0/0"
        )
        self.assertEqual(wallet.address, "0x9858EfFD232B4033E47d90003D41EC34EcaEda94")
        file = c.capture_cli(
            self.fc,
            [
                "key-add",
                "--fid",
                "12345",
                "--nonce",
                "1",
                "--scopes",
                "cast-remove",
                "--ttl",
                "86400",
                "--yes",
            ],
            self.env,
            self.path / "key.bin",
        )
        m = self.message(file)
        body = m.data.key_add_body
        self.assertEqual(m.data.type, proto.MESSAGE_TYPE_KEY_ADD)
        self.assertEqual(body.key, self.pub)
        self.assertEqual(body.key_type, 1)
        self.assertEqual(body.scopes, [proto.MESSAGE_TYPE_CAST_REMOVE])
        self.assertEqual(body.nonce, 1)
        self.assertEqual(body.ttl, 86400)
        self.assertGreater(body.deadline, m.data.timestamp)
        self.assertEqual(body.metadata_type, 1)
        self.assertEqual(len(body.custody_signature), 65)
        # EIP712 values mirror the audited node validator, not a hard-coded digest.
        typed = dict(
            types={
                "EIP712Domain": [
                    dict(name="name", type="string"),
                    dict(name="version", type="string"),
                    dict(name="chainId", type="uint256"),
                ],
                "KeyAdd": [
                    dict(name="fid", type="uint256"),
                    dict(name="key", type="bytes"),
                    dict(name="keyType", type="uint32"),
                    dict(name="scopes", type="uint32[]"),
                    dict(name="ttl", type="uint32"),
                    dict(name="nonce", type="uint32"),
                    dict(name="deadline", type="uint256"),
                ],
            },
            primaryType="KeyAdd",
            domain=dict(name="Farcaster KeyAdd", version="1", chainId=1),
            message=dict(
                fid=12345,
                key=body.key,
                keyType=1,
                scopes=[2],
                ttl=86400,
                nonce=1,
                deadline=body.deadline,
            ),
        )
        recovered = Account.recover_message(
            encode_typed_data(full_message=typed), signature=body.custody_signature
        )
        self.assertEqual(recovered.lower(), wallet.address.lower())
        # Verify the separate SignedKeyRequestMetadata struct against the node's
        # ABI layout and off-chain EIP712 domain, including the dynamic-tuple offset.
        schema = ["(uint256,address,bytes,uint256)"]
        request_fid, requester, request_signature, deadline = decode(
            schema, body.metadata
        )[0]
        self.assertEqual(
            encode(schema, [(request_fid, requester, request_signature, deadline)]),
            body.metadata,
        )
        self.assertEqual(request_fid, 12345)
        self.assertEqual(requester.lower(), wallet.address.lower())
        self.assertEqual(deadline, body.deadline)
        self.assertEqual(len(request_signature), 65)
        request_typed = dict(
            types={
                "EIP712Domain": typed["types"]["EIP712Domain"],
                "SignedKeyRequest": [
                    dict(name="requestFid", type="uint256"),
                    dict(name="key", type="bytes"),
                    dict(name="deadline", type="uint256"),
                ],
            },
            primaryType="SignedKeyRequest",
            domain=typed["domain"],
            message=dict(requestFid=request_fid, key=body.key, deadline=deadline),
        )
        request_recovered = Account.recover_message(
            encode_typed_data(full_message=request_typed), signature=request_signature
        )
        self.assertEqual(request_recovered.lower(), requester.lower())

    def test_custody_selector_matches_contract_abi(self):
        self.assertEqual(keccak(text="custodyOf(uint256)")[:4].hex(), "65269e47")


if __name__ == "__main__":
    unittest.main(verbosity=2)
