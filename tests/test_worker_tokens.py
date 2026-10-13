"""The pinned embedding counter over a fictitious byte-level vocabulary."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tiktoken.load import data_gym_to_mergeable_bpe_ranks

from gideon.worker.pipeline import (
    PIPELINE_VERSION,
    PIPELINES,
    TokenizerPin,
    tokenizer_paths,
)
from gideon.worker.tokens import TokenizerProblem, load_counter, mergeable_ranks


def _loader_ranks(vocab: Path, merges: Path) -> dict[bytes, int]:
    """tiktoken's own data-gym loader, its path cache switched off."""

    with mock.patch.dict("os.environ", {"TIKTOKEN_CACHE_DIR": ""}):
        return data_gym_to_mergeable_bpe_ranks(str(merges), str(vocab), clobber_one_byte_tokens=True)


def _data_gym_bytes() -> dict[int, str]:
    first = [byte for byte in range(256) if chr(byte).isprintable() and chr(byte) != " "]
    encoded = {byte: chr(byte) for byte in first}
    extra = 0
    for byte in range(256):
        if byte not in encoded:
            encoded[byte] = chr(256 + extra)
            extra += 1
    return encoded


class FictitiousTokenizer(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pin = TokenizerPin("Example/Fictitious", "revision", (
            ("tokenizer.json", ""), ("vocab.json", ""), ("merges.txt", ""),
        ))
        self.paths = dict(zip(
            (name for name, _digest in self.pin.files), tokenizer_paths(self.pin, self.root), strict=True,
        ))
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        byte_chars = _data_gym_bytes()
        rank_order = [byte for byte in range(256) if chr(byte).isprintable() and chr(byte) != " "]
        rank_order.extend(byte for byte in range(256) if byte not in rank_order)
        vocab = {byte_chars[byte]: rank for rank, byte in enumerate(rank_order)}
        vocab[byte_chars[ord("a")] + byte_chars[ord("b")]] = 256
        self.paths["vocab.json"].write_text(json.dumps(vocab), encoding="utf-8")
        self.paths["merges.txt"].write_text("#version: 0.2\na b\n", encoding="utf-8")
        self.paths["tokenizer.json"].write_text(json.dumps({
            "normalizer": {"type": "NFC"},
            "pre_tokenizer": {"type": "Sequence", "pretokenizers": [
                {"type": "Split", "pattern": {"Regex": r"\w+|\s+|[^\w\s]+"},
                 "behavior": "Isolated", "invert": False},
                {"type": "ByteLevel", "use_regex": False},
            ]},
            "added_tokens": [{"content": "<|special|>", "id": 257, "special": True}],
        }), encoding="utf-8")
        self.refresh_pin()

    def refresh_pin(self) -> None:
        self.pin = replace(self.pin, files=tuple(
            (name, f"sha256:{hashlib.sha256(self.paths[name].read_bytes()).hexdigest()}")
            for name, _digest in self.pin.files
        ))

    def test_counts_merges_normalization_and_literal_special_tokens(self) -> None:
        count = load_counter(self.pin, self.root)
        self.assertEqual(count(""), 0)
        self.assertEqual(count("ab"), 1)
        self.assertEqual(count("a b"), 3)
        self.assertEqual(count("<|special|>"), 1)
        self.assertEqual(count("ab<|special|>"), 2)
        self.assertEqual(count("e\u0301"), count("é"))

    def test_ranks_equal_tiktokens_loader(self) -> None:
        self.assertEqual(
            mergeable_ranks(self.paths["vocab.json"].read_bytes(),
                            self.paths["merges.txt"].read_bytes()),
            _loader_ranks(self.paths["vocab.json"], self.paths["merges.txt"]),
        )

    def test_counts_with_no_usable_temporary_directory(self) -> None:
        refusal = FileNotFoundError("no usable temporary directory")
        with mock.patch("tempfile.gettempdir", side_effect=refusal):
            count = load_counter(self.pin, self.root)
        self.assertEqual(count("ab"), 1)

    def test_absent_file_refuses_by_name(self) -> None:
        self.paths["merges.txt"].unlink()
        with self.assertRaises(TokenizerProblem) as raised:
            load_counter(self.pin, self.root)
        self.assertEqual((raised.exception.file, raised.exception.kind), ("merges.txt", "absent"))

    def test_unreadable_file_refuses_by_name(self) -> None:
        original = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            if path == self.paths["vocab.json"]:
                raise PermissionError("denied")
            return original(path)

        with mock.patch.object(Path, "read_bytes", read_bytes), self.assertRaises(TokenizerProblem) as raised:
            load_counter(self.pin, self.root)
        self.assertEqual((raised.exception.file, raised.exception.kind), ("vocab.json", "unreadable"))

    def test_digest_mismatch_refuses_before_parsing(self) -> None:
        self.paths["tokenizer.json"].write_bytes(b"private text")
        with self.assertRaises(TokenizerProblem) as raised:
            load_counter(self.pin, self.root)
        self.assertEqual((raised.exception.file, raised.exception.kind), ("tokenizer.json", "mismatch"))
        self.assertNotIn("private text", str(raised.exception))

    def test_malformed_tokenizer_refuses(self) -> None:
        self.paths["tokenizer.json"].write_text("{}", encoding="utf-8")
        self.refresh_pin()
        with self.assertRaises(TokenizerProblem) as raised:
            load_counter(self.pin, self.root)
        self.assertEqual((raised.exception.file, raised.exception.kind), ("tokenizer.json", "malformed"))

    def test_malformed_vocab_refuses(self) -> None:
        self.paths["vocab.json"].write_text("{}", encoding="utf-8")
        self.refresh_pin()
        with self.assertRaises(TokenizerProblem) as raised:
            load_counter(self.pin, self.root)
        self.assertEqual((raised.exception.file, raised.exception.kind), ("vocab.json", "malformed"))


class PinnedTokenizer(unittest.TestCase):
    def test_pinned_snapshot_counts_when_mounted(self) -> None:
        pin = PIPELINES[PIPELINE_VERSION].tokenizer
        root = Path("/data/models")
        if any(not path.is_file() for path in tokenizer_paths(pin, root)):
            self.skipTest("pinned tokenizer files are absent from /data/models")
        count = load_counter(pin, root)
        files = dict(zip((name for name, _digest in pin.files), tokenizer_paths(pin, root), strict=True))
        self.assertEqual(
            mergeable_ranks(files["vocab.json"].read_bytes(), files["merges.txt"].read_bytes()),
            _loader_ranks(files["vocab.json"], files["merges.txt"]),
        )
        tokenizer = json.loads(files["tokenizer.json"].read_bytes())
        for token in tokenizer["added_tokens"]:
            with self.subTest(token=token["id"]):
                self.assertEqual(count(token["content"]), 1)
        self.assertEqual(count("e\u0301"), count("é"))


if __name__ == "__main__":
    unittest.main()
