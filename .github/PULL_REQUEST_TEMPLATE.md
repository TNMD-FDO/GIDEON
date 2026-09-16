By submitting this pull request you dedicate your contribution to the public domain under the CC0 1.0 Universal dedication, waiving all copyright and related rights you hold in it, worldwide, as described in [CONTRIBUTING.md](https://github.com/TNMD-FDO/GIDEON/blob/main/CONTRIBUTING.md).

## Checklist

- [ ] I ran `python3 -m tools.gate` (ruff, mypy, then pytest); the pinned toolchain and the gate are documented in [TESTING.md](https://github.com/TNMD-FDO/GIDEON/blob/main/docs/4-unit-tests/TESTING.md).
- [ ] The change contains no office value and no secret.
- [ ] I treated the tests as contracts and did not weaken one.
- [ ] A pin change follows its designated route: the pin watch for `host.lock`, `images.lock`, and `models.lock` (a model proposal is completed on the watch's branch by re-judging the memory row and regenerating the render fixtures); the rebuild runbook named by [CONTRIBUTING.md](https://github.com/TNMD-FDO/GIDEON/blob/main/CONTRIBUTING.md) for a built pin; or Dependabot for a dev-toolchain pin in `requirements-dev.txt` or an action major in a workflow.
- [ ] I completed every coupled pin copy on the proposal branch, including the Playwright constant or the `pin-watch.yml` install line where applicable.
- [ ] This is not a vulnerability fix disclosed in the open; a vulnerability goes through [SECURITY.md](https://github.com/TNMD-FDO/GIDEON/security/policy) first.

A merged pull request appears in the next tagged release, credited in its release note.
