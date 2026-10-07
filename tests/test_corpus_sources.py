"""The case-law source selects a dated dump from bounded S3 listings."""

import unittest

from gideon.host.corpus.sources import (
    CASELAW_BASE_URL,
    CASELAW_LISTING_URL,
    MAX_LISTING_BYTES,
    SOURCES,
    source_by_name,
)
from gideon.host.report import Problem

NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"


def listing(*keys: str, truncated: str = "false") -> bytes:
    body = "".join(
        "<Contents>"
        f"<Key>{key}</Key>"
        "<LastModified>2099-01-01T00:00:00.000Z</LastModified>"
        "<ETag>\"fixture\"</ETag><Size>7</Size><StorageClass>STANDARD</StorageClass>"
        "</Contents>"
        for key in keys
    )
    return (
        f'<ListBucketResult xmlns="{NAMESPACE}">'
        "<Name>fixture-bucket</Name><Prefix>bulk-data/opinions-</Prefix>"
        f"<KeyCount>{len(keys)}</KeyCount><MaxKeys>1000</MaxKeys>"
        f"<IsTruncated>{truncated}</IsTruncated>{body}</ListBucketResult>"
    ).encode()


class Caselaw(unittest.TestCase):
    """The registered source pins the newest whole opinions dump."""

    def test_registry_and_newest_dated_key(self) -> None:
        self.assertEqual(tuple(source.name for source in SOURCES), ("caselaw",))
        source = source_by_name("caselaw")
        assert source is not None
        self.assertIsNone(source_by_name("fictitious"))
        self.assertEqual(source.base_url, CASELAW_BASE_URL)
        self.assertTrue(source.carries_courts)
        self.assertEqual(source.first_courts, tuple(sorted(source.first_courts)))
        self.assertEqual(
            [(item.name, item.url) for item in source.index_documents()],
            [("listing.xml", CASELAW_LISTING_URL)],
        )
        result = source.read_index({"listing.xml": listing(
            "bulk-data/opinions-2099-01-02.csv.bz2",
            "bulk-data/opinions-2099-04-03.csv.bz2",
            "bulk-data/opinions-2099-02-04.csv.bz2",
            "bulk-data/opinions-impossible.csv.bz2",
        )})
        self.assertNotIsInstance(result, Problem)
        assert not isinstance(result, Problem)
        self.assertEqual(result.snapshot_date, "2099-04-03")
        self.assertEqual(len(result.entries), 8)
        self.assertEqual(tuple(entry.path for entry in result.entries), (
            "opinions-2099-04-03.csv.bz2",
            "opinion-clusters-2099-04-03.csv.bz2",
            "dockets-2099-04-03.csv.bz2",
            "citations-2099-04-03.csv.bz2",
            "citation-map-2099-04-03.csv.bz2",
            "courts-2099-04-03.csv.bz2",
            "schema-2099-04-03.sql",
            "load-bulk-data-2099-04-03.sh",
        ))
        self.assertTrue(all(entry.url == CASELAW_BASE_URL + entry.path for entry in result.entries))

    def test_malformed_listings_refuse_by_name_with_a_command_fix(self) -> None:
        source = SOURCES[0]
        cases = {
            "missing": ({}, "missing"),
            "truncated": ({"listing.xml": listing(
                "bulk-data/opinions-2099-01-02.csv.bz2", truncated="true"
            )}, "truncated"),
            "empty": ({"listing.xml": listing()}, "no dated opinions key"),
            "wrong root": ({"listing.xml": (
                f'<Other xmlns="{NAMESPACE}"/>'.encode()
            )}, "not a ListBucketResult"),
            "wrong namespace": ({"listing.xml": b"<ListBucketResult/>"},
                                "not a ListBucketResult"),
            "oversize": ({"listing.xml": b"x" * (MAX_LISTING_BYTES + 1)}, "size limit"),
            "invalid XML": ({"listing.xml": b"<ListBucketResult"}, "malformed XML"),
        }
        for name, (documents, detail) in cases.items():
            with self.subTest(name=name):
                result = source.read_index(documents)
                self.assertIsInstance(result, Problem)
                assert isinstance(result, Problem)
                self.assertIn("caselaw listing.xml", result.problem)
                self.assertIn(detail, result.problem)
                self.assertEqual(result.fix, "Check the upstream listing, then retry.")
