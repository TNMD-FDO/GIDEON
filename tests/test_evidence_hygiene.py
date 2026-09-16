"""Keep acceptance evidence free of print-once secrets and age identities."""

import tempfile
import unittest
from pathlib import Path

from gideon.host.apply import PRINT_ONCE_LINE
from gideon.host.steps.site_dirs import (
    AGE_IDENTITY,
    AGE_IDENTITY_LINE,
    AGE_SECRET_PREFIX,
)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".scratch"


def evidence_findings(root: Path) -> list[str]:
    """Return rendered hygiene findings after one read of each text file."""

    findings: list[str] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if AGE_IDENTITY.search(line) is not None:
                findings.append(
                    f"{path}:{line_number}: complete age secret key in evidence. "
                    "Fix: redact the age identity before adding the evidence asset"
                )
            for pattern, label in (
                (PRINT_ONCE_LINE, "print-once password"),
                (AGE_IDENTITY_LINE, "age identity"),
            ):
                match = pattern.search(line)
                value = match.group("value") if match is not None else ""
                age_marker = AGE_SECRET_PREFIX + "<redacted>"
                illustrative_key = AGE_SECRET_PREFIX + "…"
                if match is not None and value not in {
                    "<redacted>",
                    age_marker,
                    illustrative_key,
                }:
                    findings.append(
                        f"{path}:{line_number}: unredacted {label} in evidence. "
                        "Fix: redact the value before adding the evidence asset"
                    )
    return findings


class EvidenceHygieneTests(unittest.TestCase):
    def test_real_scratch_evidence_is_clean(self) -> None:
        self.assertEqual(evidence_findings(SCRATCH), [])

    def test_findings_report_each_secret_with_path_and_line(self) -> None:
        key = "AGE-SECRET-KEY-1" + ("ACDEFGHJKLMNPQRSTUVWXYZ023456789" * 2)[:58]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.txt"
            evidence.write_text(
                "safe\n"
                f"secret: {key}\n"
                "admin (break-glass): password — into the office password manager now (§1.7).\n"
                "age identity (store it in the office password manager now): identity\n",
                encoding="utf-8",
            )

            findings = evidence_findings(root)

        self.assertEqual(len(findings), 3)
        self.assertTrue(all(str(evidence) in finding for finding in findings))
        self.assertIn(":2: complete age secret key in evidence.", findings[0])
        self.assertIn(":3: unredacted print-once password in evidence.", findings[1])
        self.assertIn(":4: unredacted age identity in evidence.", findings[2])


if __name__ == "__main__":
    unittest.main()
