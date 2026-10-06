"""Read case texts as JSON lines and write their exact objects as JSON lines."""

import json
import logging
import sys
import warnings

from gideon.extraction.contract import object_to_wire


def main() -> int:
    """Process each request in order, keeping diagnostics free of case text."""

    previous_logging_level = logging.root.manager.disable
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            logging.disable(logging.CRITICAL)
            try:
                from gideon.casecite.adapter import extract_case_cites
            except ImportError:
                print(
                    "casecite: adapter unavailable. "
                    "Fix: run in the pinned gideon image after its build check.",
                    file=sys.stderr,
                )
                return 1

            for line_number, line in enumerate(sys.stdin, start=1):
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    request = None
                if (
                    not isinstance(request, dict)
                    or set(request) != {"id", "text"}
                    or not isinstance(request["id"], str)
                    or not request["id"]
                    or not isinstance(request["text"], str)
                ):
                    print(
                        f"casecite: line {line_number}: invalid request. "
                        'Fix: send a JSON object with string "id" and "text" fields.',
                        file=sys.stderr,
                    )
                    return 2
                try:
                    objects = extract_case_cites(request["text"])
                # A library exception may include the source text in its message.
                except Exception:  # noqa: BLE001
                    print(
                        f"casecite: line {line_number}: extraction failed. "
                        "Fix: check the pinned gideon image with tools.imagebuild --check.",
                        file=sys.stderr,
                    )
                    return 1
                print(
                    json.dumps(
                        {
                            "id": request["id"],
                            "objects": [object_to_wire(obj) for obj in objects],
                        }
                    ),
                    flush=True,
                )
    finally:
        logging.disable(previous_logging_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
