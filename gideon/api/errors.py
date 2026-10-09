"""Build the OpenAI-shaped error objects the service answers with.

The four fixed bodies use this builder; key order is part of their wire contract.
"""


def error_body(
    message: str, *, error_type: str, code: str, param: str | None = None
) -> dict[str, dict[str, object]]:
    """Return one error with message, type, param, and code in wire order."""

    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }
