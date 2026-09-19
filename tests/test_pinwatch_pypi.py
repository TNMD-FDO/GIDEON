"""Contracts for the PyPI Index API reader used by the pin watch."""

import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from tools.pinwatch import pypi
from tools.pinwatch.fetch import FetchError, Response

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "pinwatch" / "pypi" / "starlette.json"
PROJECT = "fictitious-project"


class DictFetcher:
    """Return recorded responses and retain every request made by the reader."""

    def __init__(self, responses: Mapping[str, Response]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, Mapping[str, str] | None, str]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        method: str = "GET",
    ) -> Response:
        self.calls.append((url, headers, method))
        return self.responses[url]


def response(body: object, *, status: int = 200) -> Response:
    if isinstance(body, bytes):
        encoded = body
    elif isinstance(body, str):
        encoded = body.encode()
    else:
        encoded = json.dumps(body).encode()
    return Response(status, {}, encoded)


def file_entry(
    version: str,
    *,
    yanked: object = False,
    include_yanked: bool = True,
    suffix: str = ".tar.gz",
) -> dict[str, object]:
    entry: dict[str, object] = {"filename": f"{PROJECT}-{version}{suffix}"}
    if include_yanked:
        entry["yanked"] = yanked
    return entry


def index_reply(
    versions: list[object],
    files: list[object],
    *,
    api_version: object = "1.0",
    status: object | None = None,
) -> dict[str, object]:
    reply: dict[str, object] = {
        "meta": {"api-version": api_version},
        "versions": versions,
        "files": files,
    }
    if status is not None:
        reply["project-status"] = status
    return reply


class StableVersionContracts(unittest.TestCase):
    def test_order_and_pep_440_normalization(self) -> None:
        texts = ("1.0", "1.0.post1", "2.0", "1!0.0")
        parsed = [pypi.StableVersion.parse(text) for text in texts]
        self.assertTrue(all(version is not None for version in parsed))
        ordered = sorted(
            zip(texts, parsed, strict=True),
            key=lambda item: cast(pypi.StableVersion, item[1]),
        )
        self.assertEqual(
            [text for text, _version in ordered],
            ["1.0", "1.0.post1", "2.0", "1!0.0"],
        )
        self.assertEqual(
            pypi.StableVersion.parse("1.0"),
            pypi.StableVersion.parse("1.0.0"),
        )
        self.assertEqual(
            pypi.StableVersion.parse("V01.00"),
            pypi.StableVersion.parse("1.0"),
        )
        self.assertEqual(
            pypi.StableVersion.parse("1.0-post1"),
            pypi.StableVersion.parse("1.0.POST1"),
        )
        self.assertEqual(
            pypi.StableVersion.parse("1.0_rev1"),
            pypi.StableVersion.parse("1.0.post1"),
        )

    def test_pre_dev_local_post_pre_and_junk_are_not_stable(self) -> None:
        for text in (
            "1.0a1",
            "1.0b1",
            "1.0rc1",
            "1.0.dev1",
            "1.0+local",
            "1.0rc1.post1",
            "not-a-version",
        ):
            with self.subTest(version=text):
                self.assertIsNone(pypi.StableVersion.parse(text))


class AttributionContracts(unittest.TestCase):
    def test_distribution_filename_attribution(self) -> None:
        cases = (
            ("two-word-9000.1.0-py3-none-any.whl", "9000.1.0"),
            ("two-word-9000.1.0-1-py3-none-any.whl", "9000.1.0"),
            ("two-word-9000.1.0.tar.gz", "9000.1.0"),
            ("two-word-9000.1.0.zip", "9000.1.0"),
            ("two-word-9000.1.tar.gz", "9000.1"),
            ("two-word-9000.10.tar.gz", "9000.10"),
            ("two_word-9000.1.0.tar.gz", "9000.1.0"),
            ("two.word-9000.1.0.tar.gz", "9000.1.0"),
            ("TWO_WORD-9000.1.0.TAR.GZ", "9000.1.0"),
            ("foreign-project-9000.1.0.tar.gz", None),
            ("two-word-9000.1.0.exe", None),
        )
        for filename, expected in cases:
            with self.subTest(filename=filename):
                self.assertEqual(
                    pypi.version_from_filename(filename, "two-word"), expected
                )


class IndexReaderContracts(unittest.TestCase):
    def _resolve(
        self,
        reply: object,
        current: str = "8999.0",
        *,
        status: int = 200,
    ) -> tuple[str | None, DictFetcher]:
        url = f"{pypi.PYPI_INDEX_ROOT}/{PROJECT}/"
        fetcher = DictFetcher({url: response(reply, status=status)})
        result = pypi.pypi_version(fetcher, PROJECT, current)
        return result, fetcher

    def test_fixture_replay_uses_the_recorded_shape_and_values(self) -> None:
        """Replay 2026-09-19's reply with its Accept header and kept keys.

        The header is ``application/vnd.pypi.simple.v1+json``; the kept keys
        are ``meta``, ``name``, ``project-status``, ``versions``, and
        ``files`` with ``filename``, ``yanked``, and ``upload-time``.
        """

        document = cast(dict[str, object], json.loads(FIXTURE.read_text()))
        project = cast(str, document["name"])
        versions = cast(list[str], document["versions"])
        files = cast(list[dict[str, object]], document["files"])
        yanks: dict[str, list[bool]] = {}
        for item in files:
            version = pypi.version_from_filename(cast(str, item["filename"]), project)
            assert version is not None, item["filename"]
            yanks.setdefault(version, []).append(bool(item["yanked"]))
        stable = {
            version: parsed
            for version in versions
            if (parsed := pypi.StableVersion.parse(version)) is not None
        }
        installable = [
            version
            for version in stable
            if any(not yanked for yanked in yanks.get(version, ()))
        ]
        excluded = [version for version in versions if version not in installable]
        self.assertTrue(any(version not in stable for version in excluded))
        self.assertTrue(any(version in stable for version in excluded))

        lowest = min(stable, key=lambda version: stable[version])
        expected = max(installable, key=lambda version: stable[version])
        url = f"{pypi.PYPI_INDEX_ROOT}/{project}/"
        fetcher = DictFetcher({url: response(document)})
        self.assertEqual(pypi.pypi_version(fetcher, project, lowest), expected)
        self.assertEqual(fetcher.calls, [(url, {"Accept": pypi.PYPI_ACCEPT}, "GET")])

        for current in stable:
            with self.subTest(current=current):
                answer = pypi.pypi_version(fetcher, project, current)
                self.assertNotIn(answer, excluded)
        self.assertIsNone(pypi.pypi_version(fetcher, project, expected))

    def test_newest_prerelease_is_ignored(self) -> None:
        reply = index_reply(
            ["9000.0", "9001.0rc1"],
            [file_entry("9000.0"), file_entry("9001.0rc1")],
        )
        result, _fetcher = self._resolve(reply)
        self.assertEqual(result, "9000.0")

    def test_newest_post_release_wins(self) -> None:
        reply = index_reply(
            ["9000.0", "9000.0.post1"],
            [file_entry("9000.0"), file_entry("9000.0.post1")],
        )
        result, _fetcher = self._resolve(reply)
        self.assertEqual(result, "9000.0.post1")

    def test_yanked_releases_and_partly_yanked_files(self) -> None:
        for yanked in ("security reason", True):
            with self.subTest(yanked=yanked):
                reply = index_reply(
                    ["9000.0", "9000.1"],
                    [
                        file_entry("9000.0"),
                        file_entry("9000.1", yanked=yanked),
                    ],
                )
                result, _fetcher = self._resolve(reply)
                self.assertEqual(result, "9000.0")

        reply = index_reply(
            ["9000.0"],
            [
                file_entry("9000.0", yanked="one file"),
                file_entry("9000.0", yanked=False, suffix=".zip"),
            ],
        )
        result, _fetcher = self._resolve(reply)
        self.assertEqual(result, "9000.0")

    def test_missing_yanked_key_is_unyanked_and_missing_release_files_are_skipped(
        self,
    ) -> None:
        reply = index_reply(
            ["9000.0", "9000.1"],
            [file_entry("9000.0", include_yanked=False)],
        )
        result, _fetcher = self._resolve(reply)
        self.assertEqual(result, "9000.0")

    def test_unattributed_file_guards_a_pass_over_but_not_no_bump(self) -> None:
        unattributed = "fictitious-project-9000.1.exe"
        reply = index_reply(
            ["9000.0", "9000.1"],
            [file_entry("9000.0"), {"filename": unattributed, "yanked": False}],
        )
        with self.assertRaises(FetchError) as caught:
            self._resolve(reply)
        self.assertIn(unattributed, caught.exception.reason)

        answered = index_reply(
            ["9000.0", "9000.1"],
            [file_entry("9000.1"), {"filename": unattributed, "yanked": False}],
        )
        result, _fetcher = self._resolve(answered)
        self.assertEqual(result, "9000.1")

        no_bump = index_reply(
            ["9000.0"], [{"filename": unattributed, "yanked": False}]
        )
        result, _fetcher = self._resolve(no_bump, current="9000.0")
        self.assertIsNone(result)

    def test_unlisted_file_is_refused_before_an_older_candidate(self) -> None:
        reply = index_reply(
            ["9000.0"], [file_entry("9000.0"), file_entry("9000.1")]
        )
        with self.assertRaises(FetchError) as caught:
            self._resolve(reply)
        self.assertIn("9000.1", caught.exception.reason)

    def test_quarantine_and_api_version_rules(self) -> None:
        quarantined = index_reply(
            [], [], status={"status": "quarantined"}
        )
        with self.assertRaises(FetchError) as caught:
            self._resolve(quarantined)
        self.assertIn("quarantined", caught.exception.reason)

        unknown_major = index_reply(["9000.0"], [file_entry("9000.0")], api_version="2.0")
        with self.assertRaises(FetchError):
            self._resolve(unknown_major)
        greater_minor = index_reply(
            ["9000.0"], [file_entry("9000.0")], api_version="1.99"
        )
        result, _fetcher = self._resolve(greater_minor)
        self.assertEqual(result, "9000.0")

    def test_html_and_malformed_json_are_refused(self) -> None:
        for body in (b"<html>not JSON</html>", b"{not JSON"):
            with self.subTest(body=body), self.assertRaises(FetchError):
                self._resolve(body)

    def test_each_index_shape_type_is_refused(self) -> None:
        valid = index_reply(["9000.0"], [file_entry("9000.0")])
        cases: dict[str, object] = {
            "top level": [],
            "meta": {**valid, "meta": []},
            "api version": {**valid, "meta": {"api-version": 1}},
            "versions": {**valid, "versions": {}},
            "version item": {**valid, "versions": [9000]},
            "files": {**valid, "files": {}},
            "file item": {**valid, "files": [[]]},
            "filename": {**valid, "files": [{"filename": 9000}]},
            "yanked": {**valid, "files": [{"filename": "x", "yanked": []}]},
            "project status": {**valid, "project-status": []},
            "status value": {
                **valid,
                "project-status": {"status": []},
            },
        }
        for label, invalid in cases.items():
            with self.subTest(shape=label), self.assertRaises(FetchError):
                self._resolve(invalid)

    def test_404_duplicate_identity_invalid_current_and_equal_spelling(self) -> None:
        with self.assertRaises(FetchError) as caught:
            self._resolve({}, status=404)
        self.assertIn("404", caught.exception.reason)

        duplicate = index_reply(
            ["9000.1", "9000.1.0"],
            [file_entry("9000.1")],
        )
        with self.assertRaises(FetchError) as caught:
            self._resolve(duplicate)
        self.assertIn("duplicate", caught.exception.reason)

        fetcher = DictFetcher({})
        with self.assertRaises(ValueError):
            pypi.pypi_version(fetcher, PROJECT, "9000.0rc1")
        self.assertEqual(fetcher.calls, [])

        equal = index_reply(["9000.1.0"], [file_entry("9000.1.0")])
        result, _fetcher = self._resolve(equal, current="v9000.1")
        self.assertIsNone(result)
