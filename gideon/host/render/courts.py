"""Render the API's release court names and lockfile court membership."""

import json
from collections.abc import Mapping, Sequence

from gideon.host.courts import CourtMap
from gideon.host.render import Artifact, RenderInputs
from gideon.host.render.api import API_COURTS_PATH, API_SERVICE_NAME, api_enabled


def api_courts_text(courts: CourtMap, lockfile_courts: Mapping[str, Sequence[str]]) -> str:
    """Emit the court file read by the turn service."""

    document = {
        "format": 1,
        "courts": {
            identifier: {
                "level": court.level, "circuit": court.circuit, "state": court.state, "name": court.name,
            }
            for identifier, court in courts.courts.items()
        },
        "lockfiles": {label: sorted(ids) for label, ids in lockfile_courts.items()},
    }
    return json.dumps(document, sort_keys=True, indent=2) + "\n"


class ApiCourtsArtifact(Artifact):
    """The mounted court file, whose content changes recreate the API."""

    name = "api-courts"
    relative_path = API_COURTS_PATH
    mode = 0o644
    owners = (API_SERVICE_NAME,)

    def applies(self, inputs: RenderInputs) -> bool:
        return api_enabled(inputs.no_gpu)

    def emit(self, inputs: RenderInputs) -> str:
        if inputs.courts is None:
            raise ValueError(
                "Render input court map is missing. Restore courts.yaml and re-run render from a release checkout."
            )
        return api_courts_text(inputs.courts, inputs.lockfile_courts or {})
