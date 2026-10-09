"""The one model id the service lists and accepts, and the engine's name behind it.

The service is the only place that knows the engine's served name: it lists
General's id alone, refuses a body naming any other before the instruction, the
judge, or the engine, and sends an admitted body upstream under the served
name. The engine publishes no port and its key is the service's alone, so the
refusal is what withholds the raw model from every client of the connection.
The refusal names no id, so a caller's value never reaches a response.
"""

import json
from typing import Final

from .errors import error_body

MODEL_NOT_FOUND_STATUS: Final[int] = 404
MODEL_NOT_FOUND_ERROR: Final[dict[str, dict[str, object]]] = error_body(
    "The requested model was not found.",
    error_type="invalid_request_error",
    code="model_not_found",
    param="model",
)
# The listed entry is the service's own, so no engine extra rides it.
MODEL_OWNER: Final[str] = "gideon"


def admits_model(body: bytes, model_id: str) -> bool:
    """Accept only a JSON object naming the service's model id."""

    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("model"), str) and (
        parsed["model"] == model_id
    )


def address_completion_body(body: bytes, engine_model: str) -> bytes:
    """Replace only the model of a readable request with the engine's name."""

    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return body
    if not isinstance(parsed, dict):
        return body
    parsed["model"] = engine_model
    return json.dumps(parsed, separators=(",", ":")).encode("utf-8")


def model_listing(body: bytes, engine_model: str, model_id: str) -> bytes | None:
    """List General only when the engine lists its configured served name."""

    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
        return None
    for entry in parsed["data"]:
        if isinstance(entry, dict) and entry.get("id") == engine_model:
            created = entry.get("created")
            listing = {
                "object": "list",
                "data": [{
                    "id": model_id,
                    "object": "model",
                    "created": created if type(created) is int else 0,
                    "owned_by": MODEL_OWNER,
                }],
            }
            return json.dumps(listing, separators=(",", ":")).encode("utf-8")
    return None
