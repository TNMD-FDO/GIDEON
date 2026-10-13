"""Count embedding tokens from verified files in the pinned weights snapshot."""

import hashlib
import json
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import tiktoken

from gideon.worker.pipeline import TokenizerPin, tokenizer_paths

type Counter = Callable[[str], int]


type ProblemKind = Literal["absent", "unreadable", "mismatch", "malformed"]


class TokenizerProblem(Exception):
    """A pinned tokenizer file that cannot be used, named without its contents."""

    def __init__(self, file: str, kind: ProblemKind) -> None:
        self.file = file
        self.kind = kind
        super().__init__(f"{file}: {kind}")


def mergeable_ranks(vocab: bytes, merges: bytes) -> dict[bytes, int]:
    """Read GPT-2-style vocabulary and merges as tiktoken's data-gym loader does.

    The loader itself reads by path through a cache under the temporary
    directory, which a read-only root lacks; this restatement reads the bytes
    already verified, with its one-byte tokens taken from the vocabulary, and a
    test holds it equal to the loader.
    """

    rank_to_byte = [byte for byte in range(256) if chr(byte).isprintable() and chr(byte) != " "]
    data_gym_byte = {chr(byte): byte for byte in rank_to_byte}
    shifted = 0
    for byte in range(256):
        if byte not in rank_to_byte:
            rank_to_byte.append(byte)
            data_gym_byte[chr(256 + shifted)] = byte
            shifted += 1

    def decode(value: str) -> bytes:
        return bytes(data_gym_byte[character] for character in value)

    ranks = {bytes([byte]): rank for rank, byte in enumerate(rank_to_byte)}
    for rank, line in enumerate(merges.decode("utf-8").split("\n")[1:-1], start=len(ranks)):
        first, second = line.split()
        ranks[decode(first) + decode(second)] = rank
    encoder = {decode(token): rank for token, rank in json.loads(vocab).items()}
    encoder.pop(b"<|endoftext|>", None)
    encoder.pop(b"<|startoftext|>", None)
    for token, rank in encoder.items():
        if len(token) == 1:
            ranks[token] = rank
    if ranks != encoder:
        raise ValueError("the vocabulary and merges disagree")
    return ranks


def load_counter(pin: TokenizerPin, root: Path) -> Counter:
    """Verify the snapshot and construct the embedding model's bare counter."""

    paths = {path.name: path for path in tokenizer_paths(pin, root)}
    pinned = {filename: digest.removeprefix("sha256:") for filename, digest in pin.files}
    verified: dict[str, bytes] = {}
    for filename in pinned:
        path = paths[filename]
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise TokenizerProblem(filename, "absent") from exc
        except OSError as exc:
            raise TokenizerProblem(filename, "unreadable") from exc
        if hashlib.sha256(data).hexdigest() != pinned[filename]:
            raise TokenizerProblem(filename, "mismatch")
        verified[filename] = data

    try:
        tokenizer = json.loads(verified["tokenizer.json"])
        pattern = tokenizer["pre_tokenizer"]["pretokenizers"][0]["pattern"]["Regex"]
        added = tokenizer["added_tokens"]
        if not isinstance(pattern, str) or not isinstance(added, list):
            raise ValueError("invalid tokenizer structure")
        special_tokens = {token["content"]: token["id"] for token in added}
        if len(special_tokens) != len(added) or not all(
            isinstance(name, str) and isinstance(identifier, int) and not isinstance(identifier, bool)
            for name, identifier in special_tokens.items()
        ):
            raise ValueError("invalid added tokens")
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError) as exc:
        raise TokenizerProblem("tokenizer.json", "malformed") from exc

    try:
        ranks = mergeable_ranks(verified["vocab.json"], verified["merges.txt"])
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise TokenizerProblem("vocab.json", "malformed") from exc

    try:
        encoding = tiktoken.Encoding(
            name="gideon-embed",
            pat_str=pattern,
            mergeable_ranks=ranks,
            special_tokens=special_tokens,
        )
    except Exception as exc:
        raise TokenizerProblem("tokenizer.json", "malformed") from exc

    allowed = frozenset(special_tokens)

    def count(text: str) -> int:
        normalized = unicodedata.normalize("NFC", text)
        return len(encoding.encode(normalized, allowed_special=allowed))

    return count
