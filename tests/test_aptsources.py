"""Checks for apt source parsing and repository scans."""

import unittest
from typing import cast

from gideon.host.aptsources import (
    Entry,
    Scan,
    entries_for,
    parse_deb822,
    parse_list,
    same_repository,
)
from gideon.host.sysio import Host

_REPOSITORY = "https://packages.example.test/linux/ubuntu"
_DIR = "/etc/apt/sources.list.d"


class FakeHost:
    """The three read operations the source scanner uses."""

    def __init__(
        self,
        files: dict[str, str],
        *,
        unreadable: dict[str, Exception] | None = None,
        directory_exists: bool = True,
    ) -> None:
        self.files = files
        self.unreadable = unreadable or {}
        self.directory_exists = directory_exists

    def exists(self, path: str) -> bool:
        return path in self.files

    def listdir(self, path: str) -> list[str]:
        if not self.directory_exists:
            raise FileNotFoundError(path)
        return [
            name.removeprefix(path + "/")
            for name in self.files
            if name.startswith(path + "/")
        ]

    def read_text(self, path: str) -> str:
        if path in self.unreadable:
            raise self.unreadable[path]
        return self.files[path]


class ListParserTests(unittest.TestCase):
    def test_options_and_source_types(self) -> None:
        path = f"{_DIR}/example.list"
        entries = parse_list(
            f"deb [arch=amd64,arm64 signed-by=/keys/example.asc] {_REPOSITORY} stable main\n"
            f"deb-src {_REPOSITORY}/ stable main extras\n",
            path,
        )
        self.assertEqual(
            entries,
            (
                Entry(
                    path,
                    "list",
                    ("deb",),
                    (_REPOSITORY,),
                    ("stable",),
                    ("main",),
                    ("amd64", "arm64"),
                    "/keys/example.asc",
                    True,
                ),
                Entry(
                    path,
                    "list",
                    ("deb-src",),
                    (_REPOSITORY + "/",),
                    ("stable",),
                    ("main", "extras"),
                    (),
                    None,
                    True,
                ),
            ),
        )

    def test_comments_blanks_and_malformed_lines_are_skipped(self) -> None:
        path = f"{_DIR}/example.list"
        entries = parse_list(
            "\n  # disabled\n"
            f"deb {_REPOSITORY} stable main\n"
            f"deb [arch] {_REPOSITORY} stable main\n"
            f"deb [arch=amd64 {_REPOSITORY} stable main\n"
            f"deb {_REPOSITORY} stable\n",
            path,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].uris, (_REPOSITORY,))


class Deb822ParserTests(unittest.TestCase):
    def test_stanzas_multi_values_continuations_and_case_insensitive_fields(
        self,
    ) -> None:
        path = f"{_DIR}/example.sources"
        entries = parse_deb822(
            "# source entries\n"
            "tYpEs: deb deb-src\n"
            f"uRiS: {_REPOSITORY}\n"
            " https://mirror.example.test/linux/ubuntu\n"
            "Suites: stable testing\n"
            "Components: main extras\n"
            "Architectures: amd64 arm64\n"
            "Signed-By:\n"
            " -----BEGIN PGP PUBLIC KEY BLOCK-----\n"
            " abcdef\n"
            " -----END PGP PUBLIC KEY BLOCK-----\n"
            "\n\n"
            "Types: deb\n"
            f"URIs: {_REPOSITORY}\n"
            "Enabled: NO\n"
            "\n"
            "Types: deb\n"
            f"URIs: {_REPOSITORY}\n"
            "Enabled: false\n"
            "\n"
            "Types: deb\n"
            "Suites: stable\n",
            path,
        )
        self.assertEqual(len(entries), 3)
        first, second, third = entries
        self.assertFalse(third.enabled)
        self.assertEqual(first.types, ("deb", "deb-src"))
        self.assertEqual(
            first.uris, (_REPOSITORY, "https://mirror.example.test/linux/ubuntu")
        )
        self.assertEqual(first.suites, ("stable", "testing"))
        self.assertEqual(first.components, ("main", "extras"))
        self.assertEqual(first.architectures, ("amd64", "arm64"))
        self.assertEqual(
            first.signed_by,
            "\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n abcdef\n -----END PGP PUBLIC KEY BLOCK-----",
        )
        self.assertTrue(first.enabled)
        self.assertFalse(second.enabled)

    def test_malformed_content_does_not_raise(self) -> None:
        self.assertEqual(
            parse_deb822(" orphan\nBroken\nTypes: deb\n", "bad.sources"), ()
        )


class RepositoryComparisonTests(unittest.TestCase):
    def test_trailing_slash_and_host_case(self) -> None:
        self.assertTrue(
            same_repository("HTTPS://PACKAGES.EXAMPLE.TEST/linux/ubuntu/", _REPOSITORY)
        )
        self.assertFalse(
            same_repository("https://other.example.test/linux/ubuntu", _REPOSITORY)
        )
        self.assertFalse(same_repository("https://[broken", _REPOSITORY))


class ScanTests(unittest.TestCase):
    def _scan(self, fake: FakeHost) -> Scan:
        return entries_for(cast(Host, fake), _REPOSITORY)

    def test_source_list_and_sorted_filtered_directory(self) -> None:
        root = "/etc/apt/sources.list"
        first = f"{_DIR}/a.sources"
        second = f"{_DIR}/b.list"
        fake = FakeHost(
            {
                second: f"deb-src {_REPOSITORY}/ stable main\n",
                f"{_DIR}/ignored.sources.curtin.orig": f"Types: deb\nURIs: {_REPOSITORY}\n",
                f"{_DIR}/unrelated.list": "deb https://other.example.test stable main\n",
                first: f"Types: deb\nURIs: {_REPOSITORY}\nEnabled: no\n\n"
                f"Types: deb\nURIs: {_REPOSITORY}\n",
                root: f"# comment\ndeb {_REPOSITORY} stable main\n",
            }
        )
        scan = self._scan(fake)
        self.assertEqual([entry.file for entry in scan.entries], [root, first, second])
        self.assertEqual(scan.unreadable, ())

    def test_absent_source_list_and_directory(self) -> None:
        self.assertEqual(self._scan(FakeHost({})), Scan((), ()))
        self.assertEqual(self._scan(FakeHost({}, directory_exists=False)), Scan((), ()))

    def test_unreadable_file_is_reported_and_other_entries_survive(self) -> None:
        broken = f"{_DIR}/a.sources"
        good = f"{_DIR}/b.list"
        fake = FakeHost(
            {broken: "", good: f"deb {_REPOSITORY} stable main\n"},
            unreadable={broken: UnicodeError("cannot decode")},
        )
        scan = self._scan(fake)
        self.assertEqual(scan.unreadable, ((broken, "cannot decode"),))
        self.assertEqual([entry.file for entry in scan.entries], [good])

    def test_unreadable_source_list_is_reported(self) -> None:
        root = "/etc/apt/sources.list"
        scan = self._scan(
            FakeHost({root: ""}, unreadable={root: OSError("cannot read")})
        )
        self.assertEqual(scan.unreadable, ((root, "cannot read"),))


if __name__ == "__main__":
    unittest.main()
