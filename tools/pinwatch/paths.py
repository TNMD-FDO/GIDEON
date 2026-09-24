"""The development checkout's paths the pin watch reads.

The pin watch runs on hosted CI from the development repository alone, where
the skills' provenance record and lock, the vendored skills, the research
notes, and the two runbooks its pull requests point at all exist. The public
export leaves every one of them behind, so this module is the one place in the
package that names them, and the rule that no kept file cites what the export
omits exempts it as it exempts the export boundary's own files.
"""

from typing import Final, Literal

PROVENANCE_PATH: Final = "docs/agents/tooling.md"
SKILLS_LOCK_PATH: Final = "skills-lock.json"
SKILLS_ROOT: Final = ".claude/skills"
RESEARCH_NOTES_PATH: Final = "docs/research"
APP_SETUP_RUNBOOK_PATH: Final = "docs/runbooks/pin-watch-app-setup.md"
CI_RUNNER_RUNBOOK_PATH: Final = "docs/runbooks/ci-runner.md"

LockName = Literal[
    "images.lock",
    "host.lock",
    "models.lock",
    "docs/agents/tooling.md",
]
