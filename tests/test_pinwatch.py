"""Contracts for the pin-watch fetch, OCI, and source-parser modules."""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, replace
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from typing import cast
from unittest.mock import patch as mock_patch

import yaml  # type: ignore[import-untyped]

from gideon.host import models as host_models
from gideon.host.images import (
    BuiltImagePin,
    ImageLock,
    MirroredImagePin,
    PypiWatch,
    compute_inputs_digest,
    load_image_lock,
    load_image_lock_text,
)
from gideon.host.lock import HostLock, load_host_lock
from gideon.host.models import ModelsLock, load_models_lock, load_models_lock_text
from gideon.host.sysio import PathLike
from tools.exportboundary import absent_from_export
from tools.pinwatch import hub, notes, pypi
from tools.pinwatch import patch as patch_module
from tools.pinwatch.cli import main as pinwatch_main
from tools.pinwatch.fetch import FetchError, Response, UrllibFetcher
from tools.pinwatch.oci import (
    UnreadableTagError,
    created_time,
    image_created,
    leading_component_changed,
    list_tags,
    newest_same_shape,
    newest_same_shape_candidates,
    parse_reference,
    resolve_digest,
    same_shape,
    tag_shape,
)
from tools.pinwatch.patch import PatchError, apply_bump, replace_block, replace_scalar
from tools.pinwatch.pins import (
    AptPackagePin,
    Block,
    Bump,
    Change,
    CloudImagePin,
    DriverBranchPin,
    GithubReleasePin,
    MajorFloorPin,
    ModelPin,
    PypiProjectPin,
    RegistryImagePin,
    SkillBranchPin,
    VersionFloorPin,
    pin_registry,
)
from tools.pinwatch.pins import (
    BuiltImagePin as WatchBuiltImagePin,
)
from tools.pinwatch.pins import (
    ImagePin as WatchImagePin,
)
from tools.pinwatch.pr import (
    PullRequest,
    PullRequests,
    body_for,
    branch_state,
    proposed_version,
    push_bump,
    title_for,
    version_token,
    withdraw,
)
from tools.pinwatch.skills import (
    MATT_SOURCE,
    Provenance,
    SkillsRecordError,
    carries,
    parse_provenance,
    patch_provenance,
    record_values,
)
from tools.pinwatch.sources import (
    CloudImage,
    DebianVersion,
    GithubCommit,
    apt_package_versions,
    debian_version_key,
    github_branch_head,
    github_latest_release,
    nvidia_branches,
    runner_sha256,
    ubuntu_cloud_image,
    version_tuple,
)

ROOT = Path(__file__).resolve().parent.parent
# Every test-owned upstream value is visibly fictitious: no upstream can
# publish it for the pin. Committed host.lock expectations derive at import
# time, while the image lock below belongs only to these tests.
CADDY_DIGEST = "sha256:" + "e" * 64
IMAGE_LOCK_TEXT = (
    "# test-owned image lock\n"
    "version: 1\n"
    "images:\n"
    "  caddy:\n"
    "    source: docker.io/library/example:1.0\n"
    f"    digest: {CADDY_DIGEST}\n"
)
BUILT_IMAGE_LOCK_TEXT = (
    "# test-owned built image lock\n"
    "version: 1\n"
    "images:\n"
    "  postgres:\n"
    "    build: images/postgres\n"
    "    base: docker.io/library/example:1.0\n"
    "    base_digest: sha256:" + "a" * 64 + "\n"
    "    build_args:\n"
    "      PGBACKREST_VERSION: 1000.0.0-1.example\n"
    "    watch:\n"
    "      PGBACKREST_VERSION:\n"
    "        apt_index: https://apt.example/dists/fictitious/Packages\n"
    "        package: pgbackrest\n"
    "    inputs_digest: sha256:" + "b" * 64 + "\n"
    "    digest: sha256:" + "c" * 64 + "\n"
)
BUILT_TWO_KIND_IMAGE_LOCK_TEXT = BUILT_IMAGE_LOCK_TEXT.replace(
    "      PGBACKREST_VERSION: 1000.0.0-1.example\n",
    "      PGBACKREST_VERSION: 1000.0.0-1.example\n"
    "      PYPI_VERSION: 1.2.3\n",
).replace(
    "        package: pgbackrest\n",
    "        package: pgbackrest\n"
    "      PYPI_VERSION:\n"
    "        pypi_project: example-project\n",
)
APT_INDEX = "https://apt.example/dists/fictitious/Packages"
# Keep the fixture's repeated-hex commit visibly separate from every committed
# moving value; the lock-coupling tripwire scans this module for those values.
FICTITIOUS_PROVENANCE_TEXT = (
    "Provenance today: Matt Pocock skills at upstream `main` commit `"
    + "d" * 7
    + "` (2099-01-02).\n"
)
FICTITIOUS_MODEL_REPO = "example/fictitious-model"
FICTITIOUS_SECOND_MODEL_REPO = "example/second-model"
FICTITIOUS_MODEL_REVISION = "a" * 40
FICTITIOUS_MODEL_HEAD = "b" * 40
FICTITIOUS_SECOND_MODEL_REVISION = "c" * 40
FICTITIOUS_MODEL_DIGEST = "sha256:" + "1" * 64
FICTITIOUS_SECOND_MODEL_DIGEST = "sha256:" + "2" * 64


def _fictitious_models_lock_text(
    *,
    revision: str = FICTITIOUS_MODEL_REVISION,
    file_size: int = 3,
    memory_role: bool = True,
    two_profiles: bool = False,
) -> str:
    role_line = "        role: generator\n" if memory_role else ""
    second = ""
    if two_profiles:
        second = (
            "  2x2v-2d:\n"
            "    requires:\n"
            "      platform: x86_64\n"
            "      gpu:\n"
            "        architecture: fictitious\n"
            "        compute_capability: \"1.0\"\n"
            "        model: fictitious\n"
            "        count: 1\n"
            "        vram_gb: 2\n"
            "      dram_gb: 2\n"
            "      data_volume_gb: 2\n"
            "    memory:\n"
            "      generator:\n"
            "        gb: 8\n"
            "        role: generator\n"
            "    models:\n"
            "      generator:\n"
            f"        repo: {FICTITIOUS_SECOND_MODEL_REPO}\n"
            f"        revision: {FICTITIOUS_SECOND_MODEL_REVISION}\n"
            "        gpu: 0\n"
            "        serve:\n"
            "          served_name: fictitious-second\n"
            "          env:\n"
            "            MODE: test\n"
            "          flags:\n"
            "            max-model-len: 128\n"
            "        files:\n"
            f"          config.json:\n            sha256: {FICTITIOUS_SECOND_MODEL_DIGEST}\n            size: {file_size}\n"
        )
    return (
        "version: 1\n"
        "reference: 1x1v-1d\n"
        "profiles:\n"
        "  1x1v-1d:\n"
        "    requires:\n"
        "      platform: x86_64\n"
        "      gpu:\n"
        "        architecture: fictitious\n"
        "        compute_capability: \"1.0\"\n"
        "        model: fictitious\n"
        "        count: 1\n"
        "        vram_gb: 1\n"
        "      dram_gb: 1\n"
        "      data_volume_gb: 1\n"
        "    memory:\n"
        "      generator:\n"
        "        gb: 7\n"
        + role_line
        + "    models:\n"
        + "      generator:\n"
        + f"        repo: {FICTITIOUS_MODEL_REPO}\n"
        + f"        revision: {revision}\n"
        + "        gpu: 0\n"
        + "        serve:\n"
        + "          served_name: fictitious-generator\n"
        + "          env:\n"
        + "            MODE: test\n"
        + "          flags:\n"
        + "            max-model-len: 128\n"
        + "        files:\n"
        + f"          config.json:\n            sha256: {FICTITIOUS_MODEL_DIGEST}\n            size: {file_size}\n"
        + second
    )


FICTITIOUS_MODELS_LOCK_TEXT = _fictitious_models_lock_text()
# The proposal whose completion the branch tests judge: its new value is the
# one BUILT_IMAGE_LOCK_TEXT already carries, so that text doubles as `patched`.
PGBACKREST_BUMP = Bump(
    "images.postgres.build_args.PGBACKREST_VERSION",
    "images.lock",
    (
        Change(
            "images.postgres.build_args.PGBACKREST_VERSION",
            "999.0.0-1.example",
            "1000.0.0-1.example",
        ),
    ),
    APT_INDEX,
    True,
)
APT_PACKAGES = (
    "Package: unrelated\n"
    "Version: 9999.0.0-1.example\n\n"
    "Package: pgbackrest\n"
    "Version: 1000.0.0-1.example\n\n"
    "Package: pgbackrest\n"
    "Version: 1000.0.1-1.example\n"
)
_COMMITTED_HOST_LOCK = load_host_lock(ROOT / "host.lock").lock
assert _COMMITTED_HOST_LOCK is not None, "the committed host.lock must load"
DRIVER_BRANCH = _COMMITTED_HOST_LOCK.driver.branch
NEXT_BRANCH = str(int(DRIVER_BRANCH) + 1)


def response(
    body: object = b"",
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    if isinstance(body, str):
        body = body.encode()
    elif not isinstance(body, bytes):
        body = json.dumps(body).encode()
    return Response(status, headers or {}, body)


def _pypi_reply(project: str, current: str, candidate: str) -> dict[str, object]:
    return {
        "meta": {"api-version": "1.0"},
        "versions": [current, candidate],
        "files": [
            {"filename": f"{project}-{current}.tar.gz", "yanked": False},
            {"filename": f"{project}-{candidate}.tar.gz", "yanked": False},
        ],
    }


class DictFetcher:
    """A dict-backed fetcher with optional per-URL response queues."""

    def __init__(self, responses: Mapping[str, Response | list[Response]]) -> None:
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
        answer = self.responses[url]
        if isinstance(answer, list):
            return answer.pop(0)
        return answer


class UnreachableFetcher(DictFetcher):
    """A dict-backed fetcher whose named URLs fail in transport."""

    def __init__(
        self,
        responses: Mapping[str, Response | list[Response]],
        unreachable: frozenset[str],
    ) -> None:
        super().__init__(responses)
        self.unreachable = unreachable

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        method: str = "GET",
    ) -> Response:
        if url in self.unreachable:
            self.calls.append((url, headers, method))
            raise FetchError(url, "connection refused")
        return super().get(url, headers=headers, method=method)


def _committed_tooling_text() -> str:
    path = Path("docs/agents/tooling.md")
    if absent_from_export(path, ROOT):
        raise unittest.SkipTest("docs/agents/tooling.md is excluded from the public export")
    return (ROOT / path).read_text(encoding="utf-8")


def _committed_provenance() -> Provenance:
    return parse_provenance(_committed_tooling_text())


def _add_committed_skill_sources(fetcher: DictFetcher) -> DictFetcher:
    """Add the current skill-source answer derived from the committed record."""

    provenance = _committed_provenance()
    full_sha = (provenance.matt_commit * 40)[:40]
    fetcher.responses.setdefault(
        f"https://api.github.com/repos/{MATT_SOURCE}/commits/main",
        response(
            {
                "sha": full_sha,
                "commit": {
                    "committer": {"date": f"{provenance.matt_date}T00:00:00Z"}
                },
            }
        ),
    )
    return fetcher


def _fictitious_skill_fetcher(
    *,
    matt_sha: str | None = None,
    matt_date: str | None = None,
) -> DictFetcher:
    """Answer the skill-source request from the fictitious record."""

    provenance = parse_provenance(FICTITIOUS_PROVENANCE_TEXT)
    full_sha = matt_sha or (provenance.matt_commit * 40)[:40]
    return DictFetcher(
        {
            f"https://api.github.com/repos/{MATT_SOURCE}/commits/main": response(
                {
                    "sha": full_sha,
                    "commit": {
                        "committer": {
                            "date": f"{matt_date or provenance.matt_date}T00:00:00Z"
                        }
                    },
                }
            ),
        }
    )


def _fictitious_model_pin(
    *, memory_role: bool = True, revision: str = FICTITIOUS_MODEL_REVISION
) -> ModelPin:
    result = load_models_lock_text(
        _fictitious_models_lock_text(
            revision=revision,
            memory_role=memory_role,
        )
    )
    assert result.ok
    assert result.lock is not None
    profile = result.lock.profiles[0]
    model = profile.models[0]
    return ModelPin(
        "models.generator",
        "models.lock",
        profile.name,
        model,
        tuple(row for row in profile.memory if row.role == model.role),
    )


def _fictitious_model_fetcher(
    *,
    head: str = FICTITIOUS_MODEL_HEAD,
    tree: list[dict[str, object]] | None = None,
    file_body: bytes = b"file",
) -> DictFetcher:
    tree_url = f"https://huggingface.co/api/models/{FICTITIOUS_MODEL_REPO}/tree/{head}?recursive=true"
    files = tree if tree is not None else [
        {
            "type": "file",
            "path": "config.json",
            "size": 3,
            "lfs": {"oid": "1" * 64},
        },
        {"type": "file", "path": "nested/file.txt", "size": len(file_body)},
    ]
    return DictFetcher(
        {
            f"https://huggingface.co/api/models/{FICTITIOUS_MODEL_REPO}": response(
                {"sha": head}
            ),
            tree_url: response(files),
            f"https://huggingface.co/{FICTITIOUS_MODEL_REPO}/resolve/{head}/nested/file.txt": response(
                file_body
            ),
        }
    )


class FetchContracts(unittest.TestCase):
    def test_response_is_frozen_and_urllib_returns_http_errors(self) -> None:
        from urllib.error import HTTPError

        error = HTTPError(
            "https://example.invalid",
            503,
            "busy",
            Message(),
            BytesIO(b"temporarily unavailable"),
        )
        with mock_patch("tools.pinwatch.fetch.urlopen", side_effect=error):
            result = UrllibFetcher().get("https://example.invalid")
        self.assertEqual(result.status, 503)
        self.assertEqual(result.body, b"temporarily unavailable")
        with self.assertRaises(FrozenInstanceError):
            result.status = 200  # type: ignore[misc]

    def test_truncated_body_is_a_fetch_error(self) -> None:
        from http.client import IncompleteRead

        with (
            mock_patch(
                "tools.pinwatch.fetch.urlopen", side_effect=IncompleteRead(b"partial")
            ),
            self.assertRaises(FetchError) as caught,
        ):
            UrllibFetcher().get("https://example.invalid")
        self.assertIn("example.invalid", str(caught.exception))


class FetcherProxyContract(unittest.TestCase):
    def test_proxies_route_through_a_proxy_handler(self) -> None:
        from urllib.request import urlopen

        self.assertIs(UrllibFetcher()._open, urlopen)
        proxied = UrllibFetcher(proxies={"https": "http://proxy.example:3128"})
        self.assertIsNot(proxied._open, urlopen)


class OciContracts(unittest.TestCase):
    def test_reference_normalization(self) -> None:
        self.assertEqual(
            parse_reference("docker.io/library/example:1.0"),
            parse_reference("registry-1.docker.io/library/example:1.0"),
        )
        self.assertEqual(
            parse_reference("example:1@sha256:" + "e" * 64).repository,
            "library/example",
        )
        self.assertEqual(
            parse_reference("example:1@sha256:" + "e" * 64).digest,
            "sha256:" + "e" * 64,
        )
        with self.assertRaises(ValueError):
            parse_reference("caddy:latest@sha256:not-a-digest")

    def test_tag_shape_truth_table_and_leading_component_proposal(self) -> None:
        cases = (
            ("2.11", ("2.12", "2.12.0-beta.1", "3.0"), "3.0"),
            ("18", ("19", "19beta1", "18.1"), "19"),
            ("v0.11.0", ("v0.11.3", "v0.12.0-rc1", "0.11.4"), "v0.11.3"),
            ("2.12", ("2.11", "2.12.0-beta.1"), "2.12"),
        )
        for current, tags, expected in cases:
            with self.subTest(current=current):
                self.assertEqual(newest_same_shape(tags, current), expected)
        self.assertTrue(leading_component_changed("18", "19"))
        self.assertTrue(leading_component_changed("2.11", "3.12"))
        self.assertFalse(leading_component_changed("v0.11.0", "v0.11.3"))

    def test_compound_tag_shape_keeps_suffix_and_separator_pattern(self) -> None:
        current = "4.6.0-4.8.3-distroless"
        self.assertEqual(
            newest_same_shape(
                (
                    "4.7.0-4.9.0-distroless",
                    "4.8.0-4.10.0-distroless",
                    "4.9.0-4.11.0",
                    "4.10.0-4.12.0-alpine",
                    "4-7-0-4-9-0-distroless",
                ),
                current,
            ),
            "4.8.0-4.10.0-distroless",
        )
        shape = tag_shape(current)
        self.assertIsNotNone(shape)
        assert shape is not None
        self.assertEqual(shape.components, (4, 6, 0, 4, 8, 3))
        self.assertEqual(shape.suffix, "distroless")
        self.assertEqual(shape.separators, (".", ".", "-", ".", "."))
        self.assertTrue(same_shape(current, "4.7.0-4.9.0-distroless"))
        self.assertFalse(same_shape(current, "4.7.0-4.9.0"))
        self.assertFalse(same_shape(current, "4-7-0-4-9-0-distroless"))

    def test_dash_joined_suffix_is_one_same_shape(self) -> None:
        current = "9.8-slim-example"
        shape = tag_shape(current)
        self.assertIsNotNone(shape)
        assert shape is not None
        self.assertEqual(shape.components, (9, 8))
        self.assertEqual(shape.suffix, "slim-example")
        self.assertEqual(shape.separators, (".",))

        candidates = (
            "9.9-slim-example",
            "9.9.1-slim-example",
            "9.9-slim",
            "9.9-slim-example-extra",
            "9.9",
            "9.9-rc1-slim-example",
        )
        self.assertEqual(newest_same_shape(candidates, current), "9.9-slim-example")
        self.assertEqual(
            newest_same_shape_candidates(candidates, current), ("9.9-slim-example",)
        )

    def test_existing_pin_shapes_remain_regressions_and_unknown_tags_fail_closed(self) -> None:
        regressions = (
            ("2.11", ("2.12", "3.0"), "3.0"),
            ("v9.8.7", ("v9.8.8", "v9.9.0", "9.9.0"), "v9.9.0"),
            ("18", ("19", "20.0"), "19"),
        )
        for current, tags, expected in regressions:
            with self.subTest(current=current):
                self.assertEqual(newest_same_shape(tags, current), expected)
        with self.assertRaises(UnreadableTagError) as caught:
            newest_same_shape(("4.7.0-4.9.0-distroless",), "latest")
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.tag, "latest")
        self.assertIn("tag_shape", error.fix)
        self.assertIn("tools/pinwatch/oci.py", error.fix)
        self.assertNotIn("images.lock", error.fix)
        with self.assertRaises(UnreadableTagError):
            newest_same_shape_candidates(("9.9-slim-example",), "latest")

    def test_bearer_challenge_is_anonymous_and_retried_once(self) -> None:
        tags_url = "https://registry-1.docker.io/v2/library/example/tags/list?n=1000"
        token_url = (
            "https://auth.example/token?service=registry.example&scope="
            "repository%3Alibrary%2Fexample%3Apull"
        )
        fetcher = DictFetcher(
            {
                tags_url: [
                    response(
                        status=401,
                        headers={
                            "WWW-Authenticate": (
                                'Bearer realm="https://auth.example/token", '
                                'service="registry.example", '
                                'scope="repository:library/example:pull"'
                            )
                        },
                    ),
                    response({"tags": ["1.0"]}),
                ],
                token_url: response({"token": "anonymous-token"}),
            }
        )
        reference = parse_reference("docker.io/library/example:1.0")
        self.assertEqual(list_tags(fetcher, reference), ("1.0",))
        self.assertEqual(fetcher.calls[1][1], None)
        self.assertEqual(
            fetcher.calls[2][1], {"Authorization": "Bearer anonymous-token"}
        )

    def test_second_401_after_the_token_is_a_refusal(self) -> None:
        tags_url = "https://registry-1.docker.io/v2/library/example/tags/list?n=1000"
        challenge = response(
            status=401,
            headers={"WWW-Authenticate": 'Bearer realm="https://auth.example/token"'},
        )
        fetcher = DictFetcher(
            {
                tags_url: [challenge, challenge],
                "https://auth.example/token": response({"token": "t"}),
            }
        )
        with self.assertRaises(FetchError) as caught:
            list_tags(fetcher, parse_reference("docker.io/library/example:1.0"))
        self.assertIn("401", str(caught.exception))
        self.assertEqual(len(fetcher.calls), 3)

    def test_link_pagination(self) -> None:
        first = "https://ghcr.io/v2/example-org/example/tags/list?n=1000"
        second = "https://ghcr.io/v2/example-org/example/tags/list?last=old&n=1000"
        fetcher = DictFetcher(
            {
                first: response(
                    {"tags": ["v1.0"]},
                    headers={"Link": f'<{second}>; rel="next"'},
                ),
                second: response({"tags": ["v1.1"]}),
            }
        )
        self.assertEqual(
            list_tags(fetcher, parse_reference("ghcr.io/example-org/example:v1.0")),
            ("v1.0", "v1.1"),
        )

    def test_digest_head_and_refusals(self) -> None:
        digest = "sha256:" + "b" * 64
        reference = parse_reference("docker.io/library/example:1.0")
        url = "https://registry-1.docker.io/v2/library/example/manifests/1.1"
        fetcher = DictFetcher(
            {url: response(headers={"Docker-Content-Digest": digest})}
        )
        self.assertEqual(resolve_digest(fetcher, reference, "1.1"), digest)
        self.assertEqual(fetcher.calls[0][2], "HEAD")
        request_headers = cast(Mapping[str, str], fetcher.calls[0][1])
        self.assertIn(
            "application/vnd.oci.image.index.v1+json", request_headers["Accept"]
        )
        for header in (None, "sha256:short"):
            with self.subTest(header=header):
                bad = DictFetcher(
                    {
                        url: response(
                            headers={}
                            if header is None
                            else {"Docker-Content-Digest": header}
                        )
                    }
                )
                with self.assertRaises(FetchError):
                    resolve_digest(bad, reference, "1.1")


class SourceContracts(unittest.TestCase):
    def test_github_release_and_runner_marker(self) -> None:
        url = "https://api.github.com/repos/actions/runner/releases/latest"
        token = "test-token"
        body = "f" * 64
        fetcher = DictFetcher(
            {
                url: response(
                    {
                        "tag_name": "v1000.0.0",
                        "body": f"<!-- BEGIN SHA linux-x64 -->\n{body}\n<!-- END SHA linux-x64 -->",
                    }
                )
            }
        )
        with mock_patch.dict(os.environ, {}, clear=True):
            release = github_latest_release(fetcher, "actions/runner", token=token)
        self.assertEqual(
            (release.tag, release.body.splitlines()[0]),
            ("v1000.0.0", "<!-- BEGIN SHA linux-x64 -->"),
        )
        self.assertEqual(runner_sha256(release.body), body)
        self.assertIsNone(runner_sha256("release without the marker"))
        self.assertEqual(
            fetcher.calls[0][1],
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
            },
        )

    def test_github_branch_head_shape_and_token(self) -> None:
        url = "https://api.github.com/repos/example/skills/commits/main"
        token = "test-token"
        sha = "a" * 40
        date = "2099-01-02T03:04:05Z"
        fetcher = DictFetcher(
            {
                url: response(
                    {
                        "sha": sha,
                        "commit": {"committer": {"date": date}},
                        "message": "ignored",
                    }
                )
            }
        )
        with mock_patch.dict(os.environ, {}, clear=True):
            head = github_branch_head(
                fetcher, "example/skills", "main", token=token
            )
        self.assertEqual(head, GithubCommit(sha, date))
        self.assertEqual(
            fetcher.calls[0][1],
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
            },
        )

    def test_github_branch_head_rejects_malformed_sha(self) -> None:
        url = "https://api.github.com/repos/example/skills/commits/main"
        for sha in ("a" * 39, "a" * 41, "not-a-sha"):
            with self.subTest(sha=sha):
                fetcher = DictFetcher(
                    {
                        url: response(
                            {
                                "sha": sha,
                                "commit": {"committer": {"date": "2099-01-02"}},
                            }
                        )
                    }
                )
                with self.assertRaises(FetchError):
                    github_branch_head(fetcher, "example/skills", "main")

    def test_github_release_with_null_body_is_empty(self) -> None:
        url = "https://api.github.com/repos/example/skills/releases/latest"
        release = github_latest_release(
            DictFetcher({url: response({"tag_name": "v9.8.7", "body": None})}),
            "example/skills",
        )
        self.assertEqual(release.body, "")

    def test_version_tuples_and_docker_release_tag(self) -> None:
        self.assertEqual(version_tuple("docker-v29.7.2", r"^docker-v"), (29, 7, 2))
        self.assertEqual(version_tuple("v0.11.3", r"^v"), (0, 11, 3))
        self.assertIsNone(version_tuple("v0.11.3-rc1", r"^v"))

    def test_nvidia_branches_ignore_point_release_pinning_packages(self) -> None:
        url = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2604/x86_64/Packages"
        excerpt = (
            "Package: nvidia-driver-pinning-1000\n"
            "Version: 1000.1.1\n\n"
            "Package: nvidia-driver-pinning-1000.1.1\n"
            "Version: 1000.1.1\n\n"
            "Package: nvidia-driver-pinning-1010\n"
            "Version: 1010.1.1\n"
        )
        fetcher = DictFetcher({url: response(excerpt)})
        self.assertEqual(nvidia_branches(fetcher, "ubuntu2604/x86_64"), (1000, 1010))
        empty = DictFetcher({url: response("Package: cuda-toolkit\nVersion: 13.1\n")})
        with self.assertRaises(FetchError) as caught:
            nvidia_branches(empty, "ubuntu2604/x86_64")
        self.assertIn("nvidia-driver-pinning", str(caught.exception))

    def test_apt_package_versions_requires_the_exact_package_stanza(self) -> None:
        fetcher = DictFetcher({APT_INDEX: response(APT_PACKAGES)})
        self.assertEqual(
            apt_package_versions(fetcher, APT_INDEX, "pgbackrest"),
            ("1000.0.0-1.example", "1000.0.1-1.example"),
        )
        with self.assertRaises(FetchError) as caught:
            apt_package_versions(fetcher, APT_INDEX, "missing-package")
        self.assertIn("missing-package", str(caught.exception))

    def test_debian_version_ordering(self) -> None:
        cases = (
            ("1.0~rc1", "1.0"),
            ("2.0", "1:0.9"),
            ("1.pgdg13+1", "1.pgdg13+2"),
            ("1a", "1+"),
        )
        for lower, higher in cases:
            with self.subTest(lower=lower, higher=higher):
                self.assertLess(debian_version_key(lower), debian_version_key(higher))
        parsed = DebianVersion.parse("1:1000.0.0-1.example")
        self.assertEqual((parsed.epoch, parsed.upstream, parsed.revision), (1, "1000.0.0", "1.example"))

    def test_cloud_image_listing_and_sha256sums(self) -> None:
        pinned = "https://cloud-images.ubuntu.com/fictitious/20990101/fictitious-server-cloudimg-amd64.img"
        listing_url = "https://cloud-images.ubuntu.com/fictitious/"
        sums_url = "https://cloud-images.ubuntu.com/fictitious/20990201/SHA256SUMS"
        checksum = "c" * 64
        fetcher = DictFetcher(
            {
                listing_url: response(
                    '<a href="20990101/">old</a> <a href="20990201/">new</a>'
                ),
                sums_url: response(f"{checksum} *fictitious-server-cloudimg-amd64.img\n"),
            }
        )
        image = ubuntu_cloud_image(fetcher, pinned)
        self.assertIsNotNone(image)
        image = cast(CloudImage, image)
        self.assertEqual(
            image.url,
            "https://cloud-images.ubuntu.com/fictitious/20990201/fictitious-server-cloudimg-amd64.img",
        )
        self.assertEqual(image.sha256, checksum)

    def test_cloud_image_current_and_missing_checksum(self) -> None:
        pinned = "https://cloud-images.ubuntu.com/fictitious/20990101/fictitious-server-cloudimg-amd64.img"
        listing_url = "https://cloud-images.ubuntu.com/fictitious/"
        current = DictFetcher({listing_url: response('<a href="20990101/">only</a>')})
        self.assertIsNone(ubuntu_cloud_image(current, pinned))
        sums_url = "https://cloud-images.ubuntu.com/fictitious/20990201/SHA256SUMS"
        missing = DictFetcher(
            {
                listing_url: response('<a href="20990201/">new</a>'),
                sums_url: response(f"{'d' * 64} *fictitious-server-cloudimg-arm64.img\n"),
            }
        )
        with self.assertRaises(FetchError) as caught:
            ubuntu_cloud_image(missing, pinned)
        self.assertIn("SHA256SUMS", str(caught.exception))


class ModelPinContracts(unittest.TestCase):
    def test_current_uses_one_head_request_and_bump_records_files(self) -> None:
        pin = _fictitious_model_pin()
        current_fetcher = _fictitious_model_fetcher(head=pin.model.revision)
        self.assertIsNone(pin.resolve(current_fetcher))
        self.assertEqual(len(current_fetcher.calls), 1)

        fetcher = _fictitious_model_fetcher(file_body=b"file")
        bump = pin.resolve(fetcher)
        self.assertIsNotNone(bump)
        bump = cast(Bump, bump)
        self.assertEqual(
            bump.changes,
            (Change(
                f"profiles.{pin.profile}.models.{pin.model.role}.revision",
                pin.model.revision,
                FICTITIOUS_MODEL_HEAD,
            ),),
        )
        self.assertEqual(len(bump.blocks), 1)
        block = bump.blocks[0]
        self.assertEqual(
            block.key_path,
            f"profiles.{pin.profile}.models.{pin.model.role}.files",
        )
        self.assertEqual(set(block.new), {"config.json", "nested/file.txt"})
        new_files = cast(Mapping[str, Mapping[str, object]], block.new)
        self.assertEqual(new_files["config.json"]["sha256"], FICTITIOUS_MODEL_DIGEST)
        self.assertEqual(
            new_files["nested/file.txt"]["sha256"],
            "sha256:" + hashlib.sha256(b"file").hexdigest(),
        )
        self.assertTrue(bump.proposal)
        self.assertIn(f"/tree/{FICTITIOUS_MODEL_HEAD}", bump.upstream_url)

    def test_model_proposal_requires_a_matching_memory_row_and_size_change(self) -> None:
        no_row = _fictitious_model_pin(memory_role=False)
        no_row_bump = no_row.resolve(_fictitious_model_fetcher(file_body=b"file"))
        self.assertIsNotNone(no_row_bump)
        self.assertFalse(cast(Bump, no_row_bump).proposal)

        same_size = _fictitious_model_pin()
        same_size_bump = same_size.resolve(_fictitious_model_fetcher(file_body=b""))
        self.assertIsNotNone(same_size_bump)
        self.assertFalse(cast(Bump, same_size_bump).proposal)

    def test_model_hub_refusals_are_fetch_errors_with_their_urls(self) -> None:
        pin = _fictitious_model_pin()
        head_url = f"https://huggingface.co/api/models/{FICTITIOUS_MODEL_REPO}"
        cases = (
            response(status=401),
            response(status=429),
            response("{"),
            response({"sha": "not-a-revision"}),
        )
        for answer in cases:
            with self.subTest(answer=answer.status):
                with self.assertRaises(FetchError) as caught:
                    pin.resolve(DictFetcher({head_url: answer}))
                self.assertIn(head_url, str(caught.exception))

        malformed_path = _fictitious_model_fetcher(
            tree=[{"type": "file", "path": "bad path", "size": 1}]
        )
        with self.assertRaises(FetchError) as caught:
            pin.resolve(malformed_path)
        self.assertIn("bad path", str(caught.exception))

        wrong_length = _fictitious_model_fetcher(
            tree=[{"type": "file", "path": "nested/file.txt", "size": 9}],
            file_body=b"file",
        )
        with self.assertRaises(FetchError) as caught:
            pin.resolve(wrong_length)
        self.assertIn("bytes", str(caught.exception))

        empty = _fictitious_model_fetcher(tree=[])
        with self.assertRaises(FetchError) as caught:
            pin.resolve(empty)
        self.assertIn("no usable", str(caught.exception))

    def test_tree_walk_skips_directories_and_dot_files_and_follows_next(self) -> None:
        first_url = (
            f"https://huggingface.co/api/models/{FICTITIOUS_MODEL_REPO}/tree/"
            f"{FICTITIOUS_MODEL_HEAD}?recursive=true"
        )
        second_url = "https://huggingface.co/api/models/example/fictitious/tree?page=2"
        fetcher = DictFetcher(
            {
                first_url: response(
                    [
                        {"type": "directory", "path": "nested"},
                        {"type": "file", "path": ".gitattributes", "size": 1},
                        {"type": "file", "path": "nested/kept.txt", "size": 2},
                        {
                            "type": "file",
                            "path": "weights.safetensors",
                            "size": 3,
                            "lfs": {"oid": "3" * 64},
                        },
                    ],
                    headers={"Link": f'<{second_url}>; rel="next"'},
                ),
                second_url: response(
                    [{"type": "file", "path": "second.json", "size": 0}]
                ),
            }
        )
        entries = hub.list_tree(fetcher, FICTITIOUS_MODEL_REPO, FICTITIOUS_MODEL_HEAD)
        self.assertEqual(
            [entry.path for entry in entries],
            ["nested/kept.txt", "weights.safetensors", "second.json"],
        )
        self.assertEqual(entries[1].sha256, "sha256:" + "3" * 64)
        self.assertIsNone(entries[0].sha256)
        self.assertEqual([call[0] for call in fetcher.calls], [first_url, second_url])

    def test_cli_keeps_running_with_a_failed_model_row(self) -> None:
        host = PinWatchHost(models_text=FICTITIOUS_MODELS_LOCK_TEXT)
        fetcher = DictFetcher(
            {
                f"https://huggingface.co/api/models/{FICTITIOUS_MODEL_REPO}": response(
                    status=401
                )
            }
        )
        result, stdout, stderr = _cli_output(
            host, fetcher, ["--only", "models.generator", "--dry-run"]
        )
        self.assertEqual(result, 1)
        self.assertIn("models.generator: failed", stdout)
        self.assertIn("huggingface.co", stdout)
        self.assertEqual(stderr, "")

    def test_recorded_fixtures_define_their_own_model_and_tree_facts(self) -> None:
        model = json.loads(
            (ROOT / "tests" / "fixtures" / "pinwatch" / "hub" / "model.json").read_text()
        )
        tree = json.loads(
            (ROOT / "tests" / "fixtures" / "pinwatch" / "hub" / "tree.json").read_text()
        )
        self.assertTrue(host_models.REVISION.fullmatch(model["sha"]))
        entries = [
            entry
            for entry in tree
            if entry.get("type") == "file"
            and not any(part.startswith(".") for part in entry["path"].split("/"))
        ]
        file_entries = [entry for entry in tree if entry.get("type") == "file"]
        lfs_entries = [entry for entry in entries if "lfs" in entry]
        plain_entries = [entry for entry in entries if "lfs" not in entry]
        self.assertEqual(
            len(file_entries) - len(entries),
            sum(
                any(part.startswith(".") for part in entry["path"].split("/"))
                for entry in file_entries
            ),
        )
        self.assertEqual(len(lfs_entries) + len(plain_entries), len(entries))
        total_size = sum(entry["size"] for entry in entries)
        self.assertEqual(
            total_size,
            sum(
                entry["lfs"]["size"] if "lfs" in entry else entry["size"]
                for entry in entries
            ),
        )

    def test_recorded_fixtures_parse_through_the_hub_module(self) -> None:
        """The recorded excerpts replay through model_head and list_tree, every expectation the fixture's own."""

        fixtures = ROOT / "tests" / "fixtures" / "pinwatch" / "hub"
        model_bytes = (fixtures / "model.json").read_bytes()
        tree_bytes = (fixtures / "tree.json").read_bytes()
        model = json.loads(model_bytes)
        tree = json.loads(tree_bytes)
        repo = model["id"]
        fetcher = DictFetcher(
            {
                f"https://huggingface.co/api/models/{repo}": Response(200, {}, model_bytes),
                f"https://huggingface.co/api/models/{repo}/tree/{model['sha']}?recursive=true": Response(
                    200, {}, tree_bytes
                ),
            }
        )
        self.assertEqual(hub.model_head(fetcher, repo), model["sha"])
        entries = hub.list_tree(fetcher, repo, model["sha"])
        expected = [
            entry
            for entry in tree
            if entry["type"] == "file"
            and not any(part.startswith(".") for part in entry["path"].split("/"))
        ]
        self.assertEqual([entry.path for entry in entries], [entry["path"] for entry in expected])
        for entry, raw in zip(entries, expected, strict=True):
            with self.subTest(path=entry.path):
                self.assertEqual(entry.size, raw["size"])
                self.assertEqual(
                    entry.sha256, f"sha256:{raw['lfs']['oid']}" if "lfs" in raw else None
                )
        self.assertEqual(len(fetcher.calls), 2)


class PinContracts(unittest.TestCase):
    def _image_pin(self, source: str = "docker.io/library/example:1.0") -> WatchImagePin:
        image = MirroredImagePin("caddy", CADDY_DIGEST, source)
        return WatchImagePin("images.caddy", "images.lock", image)

    def _registry_responses(
        self,
        source: str,
        tags: list[str],
        digest: str,
        manifest_tag: str | None = None,
    ) -> DictFetcher:
        reference = parse_reference(source)
        tags_url = (
            f"https://{reference.registry}/v2/{reference.repository}/tags/list?n=1000"
        )
        selected_tag = manifest_tag or tags[-1]
        manifest_url = f"https://{reference.registry}/v2/{reference.repository}/manifests/{selected_tag}"
        return DictFetcher(
            {
                tags_url: response({"tags": tags}),
                manifest_url: response(headers={"Docker-Content-Digest": digest}),
            }
        )

    def test_image_pin_tag_digest_current_and_proposal(self) -> None:
        new_digest = "sha256:" + "b" * 64
        pin = self._image_pin()
        fetcher = self._registry_responses(
            pin.image.source, ["1.0", "1.1"], new_digest
        )
        bump = pin.resolve(fetcher)
        self.assertIsNotNone(bump)
        bump = cast(Bump, bump)
        self.assertEqual(
            bump.changes,
            (
                Change(
                    "images.caddy.source",
                    pin.image.source,
                    "docker.io/library/example:1.1",
                ),
                Change("images.caddy.digest", pin.image.digest, new_digest),
            ),
        )
        self.assertFalse(bump.proposal)

        major_fetcher = self._registry_responses(
            pin.image.source, ["1.0", "2.0"], new_digest
        )
        major = cast(Bump, pin.resolve(major_fetcher))
        self.assertTrue(major.proposal)

        digest_only = self._registry_responses(pin.image.source, ["1.0"], new_digest)
        digest_bump = cast(Bump, pin.resolve(digest_only))
        self.assertEqual(
            digest_bump.changes,
            (Change("images.caddy.digest", pin.image.digest, new_digest),),
        )

        current = self._registry_responses(
            pin.image.source,
            ["1.0", "1.0.0-beta.1", "0.9"],
            pin.image.digest,
            "1.0",
        )
        self.assertIsNone(pin.resolve(current))

    def _built_pin(self) -> WatchBuiltImagePin:
        result = load_image_lock_text(BUILT_IMAGE_LOCK_TEXT)
        self.assertTrue(result.ok)
        image = result.lock.images[0] if result.lock is not None else None
        self.assertIsInstance(image, BuiltImagePin)
        assert isinstance(image, BuiltImagePin)
        return WatchBuiltImagePin("images.postgres", "images.lock", image)

    def _pypi_pin(self) -> PypiProjectPin:
        result = load_image_lock_text(BUILT_TWO_KIND_IMAGE_LOCK_TEXT)
        self.assertTrue(result.ok)
        assert result.lock is not None
        image = result.lock.images[0]
        self.assertIsInstance(image, BuiltImagePin)
        assert isinstance(image, BuiltImagePin)
        return PypiProjectPin(
            "images.postgres.build_args.PYPI_VERSION",
            "images.lock",
            image,
            "PYPI_VERSION",
        )

    def test_built_base_pin_is_always_a_proposal(self) -> None:
        pin = self._built_pin()
        reference = parse_reference(pin.image.base)
        tags_url = "https://registry-1.docker.io/v2/library/example/tags/list?n=1000"
        manifest_url = "https://registry-1.docker.io/v2/library/example/manifests/1.1"
        new_digest = "sha256:" + "d" * 64
        fetcher = DictFetcher(
            {
                tags_url: response({"tags": ["1.0", "1.1"]}),
                manifest_url: response(headers={"Docker-Content-Digest": new_digest}),
            }
        )
        bump = pin.resolve(fetcher)
        self.assertIsNotNone(bump)
        bump = cast(Bump, bump)
        self.assertEqual(
            bump.changes,
            (
                Change("images.postgres.base", pin.image.base, "docker.io/library/example:1.1"),
                Change("images.postgres.base_digest", pin.image.base_digest, new_digest),
            ),
        )
        self.assertTrue(bump.proposal)
        current = DictFetcher(
            {
                tags_url: response({"tags": [reference.tag]}),
                f"https://registry-1.docker.io/v2/library/example/manifests/{reference.tag}": response(
                    headers={"Docker-Content-Digest": pin.image.base_digest}
                ),
            }
        )
        self.assertIsNone(pin.resolve(current))

    def test_apt_package_pin_proposes_newest_debian_version(self) -> None:
        pin = AptPackagePin(
            "images.postgres.build_args.PGBACKREST_VERSION",
            "images.lock",
            self._built_pin().image,
            "PGBACKREST_VERSION",
        )
        bump = pin.resolve(DictFetcher({APT_INDEX: response(APT_PACKAGES)}))
        self.assertIsNotNone(bump)
        bump = cast(Bump, bump)
        self.assertEqual(
            bump.changes,
            (
                Change(
                    "images.postgres.build_args.PGBACKREST_VERSION",
                    pin.current(),
                    "1000.0.1-1.example",
                ),
            ),
        )
        self.assertTrue(bump.proposal)
        self.assertEqual(bump.upstream_url, APT_INDEX)
        current = DictFetcher(
            {
                APT_INDEX: response(
                    APT_PACKAGES.replace("1000.0.0-1.example", "1000.0.1-1.example")
                )
            }
        )
        current_result = load_image_lock_text(
            BUILT_IMAGE_LOCK_TEXT.replace(
                "1000.0.0-1.example", "1000.0.1-1.example"
            )
        )
        self.assertTrue(current_result.ok)
        assert current_result.lock is not None
        current_image = current_result.lock.images[0]
        assert isinstance(current_image, BuiltImagePin)
        current_pin = AptPackagePin(
            pin.id,
            pin.lock,
            current_image,
            pin.argument,
        )
        self.assertIsNone(current_pin.resolve(current))

    def test_pypi_project_pin_proposes_newest_release(self) -> None:
        result = load_image_lock_text(BUILT_TWO_KIND_IMAGE_LOCK_TEXT)
        self.assertTrue(result.ok)
        assert result.lock is not None
        image = result.lock.images[0]
        self.assertIsInstance(image, BuiltImagePin)
        assert isinstance(image, BuiltImagePin)
        watch = image.watch["PYPI_VERSION"]
        self.assertIsInstance(watch, PypiWatch)
        assert isinstance(watch, PypiWatch)
        pin = PypiProjectPin(
            "images.postgres.build_args.PYPI_VERSION",
            "images.lock",
            image,
            "PYPI_VERSION",
        )
        candidate = "9000.0.1"
        url = f"{pypi.PYPI_INDEX_ROOT}/{watch.project}/"
        fetcher = DictFetcher(
            {url: response(_pypi_reply(watch.project, pin.current(), candidate))}
        )
        bump = pin.resolve(fetcher)
        self.assertIsNotNone(bump)
        bump = cast(Bump, bump)
        self.assertEqual(
            bump.changes,
            (
                Change(
                    pin.key_paths[0],
                    pin.current(),
                    candidate,
                ),
            ),
        )
        self.assertTrue(bump.proposal)
        self.assertEqual(bump.upstream_url, pypi.project_page(watch.project, candidate))
        self.assertEqual(fetcher.calls, [(url, {"Accept": pypi.PYPI_ACCEPT}, "GET")])

    def test_pypi_project_pin_is_current_at_the_loaded_value(self) -> None:
        pin = self._pypi_pin()
        watch = pin.image.watch[pin.argument]
        self.assertIsInstance(watch, PypiWatch)
        assert isinstance(watch, PypiWatch)
        url = f"{pypi.PYPI_INDEX_ROOT}/{watch.project}/"
        reply = {
            "meta": {"api-version": "1.0"},
            "versions": [pin.current()],
            "files": [{"filename": f"{watch.project}-{pin.current()}.tar.gz"}],
        }
        self.assertIsNone(pin.resolve(DictFetcher({url: response(reply)})))

    def test_registry_image_pin(self) -> None:
        old = "sha256:" + "a" * 64
        new = "sha256:" + "b" * 64
        source = f"example:1@{old}"
        pin = RegistryImagePin("host.registry_image", "host.lock", source)
        fetcher = self._registry_responses(source, ["3", "4"], new)
        bump = cast(Bump, pin.resolve(fetcher))
        self.assertEqual(
            bump.changes,
            (Change("registry_image", source, f"example:4@{new}"),),
        )
        self.assertTrue(bump.proposal)

    def _release_fetcher(
        self, repository: str, tag: str, body: str = ""
    ) -> DictFetcher:
        return DictFetcher(
            {
                f"https://api.github.com/repos/{repository}/releases/latest": response(
                    {"tag_name": tag, "body": body}
                )
            }
        )

    def test_runner_release_and_marker_refusal(self) -> None:
        old_sha = "a" * 64
        new_sha = "b" * 64
        body = f"<!-- BEGIN SHA linux-x64 -->\n{new_sha}\n<!-- END SHA linux-x64 -->"
        pin = GithubReleasePin("host.gh_runner", "host.lock", "1000.0.0", old_sha)
        bump = cast(
            Bump, pin.resolve(self._release_fetcher("actions/runner", "v1000.1.0", body))
        )
        self.assertEqual(
            bump.changes,
            (
                Change("gh_runner.version", "1000.0.0", "1000.1.0"),
                Change("gh_runner.sha256", old_sha, new_sha),
            ),
        )
        missing = self._release_fetcher("actions/runner", "v1000.1.0")
        with self.assertRaises(FetchError) as caught:
            pin.resolve(missing)
        self.assertIn(
            "github.com/actions/runner/releases/tag/v1000.1.0", str(caught.exception)
        )

    def _fictitious_matt_pin(self) -> SkillBranchPin:
        provenance = parse_provenance(FICTITIOUS_PROVENANCE_TEXT)
        return SkillBranchPin(
            "skills.matt-pocock",
            "docs/agents/tooling.md",
            commit=provenance.matt_commit,
            date=provenance.matt_date,
        )

    def test_skill_branch_pin_current_and_newer_head(self) -> None:
        pin = self._fictitious_matt_pin()
        url = f"https://api.github.com/repos/{MATT_SOURCE}/commits/main"
        current_sha = "d" * 40
        current = DictFetcher(
            {
                url: response(
                    {
                        "sha": current_sha,
                        "commit": {"committer": {"date": "2099-01-02T03:04:05Z"}},
                    }
                )
            }
        )
        self.assertIsNone(pin.resolve(current))

        newer_sha = "e" * 40
        newer = DictFetcher(
            {
                url: response(
                    {
                        "sha": newer_sha,
                        "commit": {"committer": {"date": "2099-02-03T04:05:06Z"}},
                    }
                )
            }
        )
        bump = pin.resolve(newer)
        self.assertIsNotNone(bump)
        bump = cast(Bump, bump)
        self.assertEqual(
            bump.changes,
            (
                Change("matt-pocock.commit", "ddddddd", "eeeeeee"),
                Change("matt-pocock.date", "2099-01-02", "2099-02-03"),
            ),
        )
        self.assertEqual(
            bump.upstream_url,
            f"https://github.com/{MATT_SOURCE}/commit/{newer_sha}",
        )
        self.assertTrue(bump.proposal)

    def test_major_and_version_floors(self) -> None:
        docker = MajorFloorPin(
            "host.minimums.docker",
            "host.lock",
            29,
            "moby/moby",
            r"^docker-v",
            "minimums.docker",
        )
        self.assertIsNone(
            docker.resolve(self._release_fetcher("moby/moby", "docker-v29.7.2"))
        )
        docker_bump = cast(
            Bump, docker.resolve(self._release_fetcher("moby/moby", "docker-v30.0.0"))
        )
        self.assertEqual(docker_bump.changes[0], Change("minimums.docker", "29", "30"))
        self.assertTrue(docker_bump.proposal)

        compose = MajorFloorPin(
            "host.minimums.compose",
            "host.lock",
            5,
            "docker/compose",
            r"^v",
            "minimums.compose",
        )
        compose_bump = cast(
            Bump, compose.resolve(self._release_fetcher("docker/compose", "v6.0.0"))
        )
        self.assertEqual(compose_bump.changes[0].new, "6")

        toolkit = VersionFloorPin(
            "host.minimums.toolkit",
            "host.lock",
            "100.0.0",
            "NVIDIA/nvidia-container-toolkit",
        )
        toolkit_bump = cast(
            Bump,
            toolkit.resolve(
                self._release_fetcher("NVIDIA/nvidia-container-toolkit", "v100.1.0")
            ),
        )
        self.assertEqual(toolkit_bump.changes[0].new, "100.1.0")
        self.assertTrue(toolkit_bump.proposal)

    def test_driver_and_cloud_image_pins(self) -> None:
        driver_url = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2604/x86_64/Packages"
        driver = DriverBranchPin(
            "host.driver.branch", "host.lock", "1000", "ubuntu2604/x86_64"
        )
        bump = cast(
            Bump,
            driver.resolve(
                DictFetcher(
                    {
                        driver_url: response(
                            "Package: nvidia-driver-pinning-1000\nPackage: nvidia-driver-pinning-1010\n"
                        )
                    }
                )
            ),
        )
        self.assertEqual(bump.changes, (Change("driver.branch", "1000", "1010"),))
        self.assertTrue(bump.proposal)
        self.assertIsNone(
            driver.resolve(
                DictFetcher(
                    {driver_url: response("Package: nvidia-driver-pinning-1000\n")}
                )
            )
        )

        old_url = "https://cloud-images.ubuntu.com/fictitious/20990101/fictitious-server-cloudimg-amd64.img"
        listing_url = "https://cloud-images.ubuntu.com/fictitious/"
        sums_url = "https://cloud-images.ubuntu.com/fictitious/20990201/SHA256SUMS"
        new_sha = "d" * 64
        cloud = CloudImagePin(
            "host.acceptance_vm_image", "host.lock", old_url, "a" * 64
        )
        fetcher = DictFetcher(
            {
                listing_url: response('<a href="20990201/">new</a>'),
                sums_url: response(f"{new_sha} *fictitious-server-cloudimg-amd64.img\n"),
            }
        )
        cloud_bump = cast(Bump, cloud.resolve(fetcher))
        self.assertEqual(cloud_bump.changes[0].key_path, "acceptance_vm_image.url")
        self.assertEqual(cloud_bump.changes[1].new, new_sha)

    def test_pins_use_committed_locks_in_the_planned_order(self) -> None:
        image_result = load_image_lock(ROOT / "images.lock")
        host_result = load_host_lock(ROOT / "host.lock")
        models_result = load_models_lock(ROOT / "models.lock")
        self.assertTrue(image_result.ok)
        self.assertTrue(host_result.ok)
        self.assertTrue(models_result.ok)
        image_lock = image_result.lock
        host_lock = host_result.lock
        models_lock = models_result.lock
        if image_lock is None or host_lock is None or models_lock is None:
            self.fail("committed locks did not load")
        provenance = _committed_provenance()
        pins = pin_registry(
            cast(ImageLock, image_lock),
            cast(HostLock, host_lock),
            cast(ModelsLock, models_lock),
            provenance,
        )
        # Images in lock order, a built pin followed by its watched arguments,
        # then the host pins — derived from the loaded lock so the contract
        # holds on every bump branch.
        expected_images: list[str] = []
        for image in image_lock.images:
            expected_images.append(f"images.{image.name}")
            if isinstance(image, BuiltImagePin):
                expected_images.extend(
                    f"images.{image.name}.build_args.{argument}" for argument in image.watch
                )
        self.assertEqual(
            [pin.id for pin in pins],
            [
                *expected_images,
                "host.registry_image",
                "host.gh_runner",
                "host.minimums.docker",
                "host.minimums.compose",
                "host.minimums.toolkit",
                "host.driver.branch",
                "host.acceptance_vm_image",
                "models.generator",
                "skills.matt-pocock",
            ],
        )
        current = {pin.id: pin.current() for pin in pins}
        first_image = image_lock.images[0]
        assert isinstance(first_image, MirroredImagePin)
        self.assertTrue(current[f"images.{first_image.name}"].startswith(first_image.source))
        for image in image_lock.images:
            if isinstance(image, BuiltImagePin):
                self.assertTrue(current[f"images.{image.name}"].startswith(image.base))
                for argument, value in image.build_args.items():
                    if argument in image.watch:
                        self.assertEqual(current[f"images.{image.name}.build_args.{argument}"], value)
        self.assertEqual(current["host.registry_image"], host_lock.registry_image)
        self.assertIn(host_lock.gh_runner.version, current["host.gh_runner"])
        self.assertEqual(current["host.driver.branch"], host_lock.driver.branch)
        self.assertIn(host_lock.acceptance_vm_image.url, current["host.acceptance_vm_image"])
        self.assertEqual(
            current["skills.matt-pocock"],
            f"main@{provenance.matt_commit} ({provenance.matt_date})",
        )

    def test_built_pin_registry_places_apt_watches_after_the_image(self) -> None:
        image_result = load_image_lock_text(BUILT_IMAGE_LOCK_TEXT)
        host_result = load_host_lock(ROOT / "host.lock")
        models_result = load_models_lock(ROOT / "models.lock")
        self.assertTrue(image_result.ok)
        self.assertTrue(host_result.ok)
        self.assertTrue(models_result.ok)
        assert image_result.lock is not None
        assert host_result.lock is not None
        assert models_result.lock is not None
        provenance = _committed_provenance()
        pins = pin_registry(
            image_result.lock,
            host_result.lock,
            models_result.lock,
            provenance,
        )
        self.assertEqual(
            [pin.id for pin in pins[:2]],
            ["images.postgres", "images.postgres.build_args.PGBACKREST_VERSION"],
        )
        self.assertEqual(pins[2].id, "host.registry_image")

    def test_built_pin_registry_places_both_watch_kinds_in_lock_order(self) -> None:
        image_result = load_image_lock_text(BUILT_TWO_KIND_IMAGE_LOCK_TEXT)
        host_result = load_host_lock(ROOT / "host.lock")
        models_result = load_models_lock(ROOT / "models.lock")
        self.assertTrue(image_result.ok)
        self.assertTrue(host_result.ok)
        self.assertTrue(models_result.ok)
        assert image_result.lock is not None
        assert host_result.lock is not None
        assert models_result.lock is not None
        pins = pin_registry(
            image_result.lock,
            host_result.lock,
            models_result.lock,
            _committed_provenance(),
        )
        self.assertEqual(
            [pin.id for pin in pins[:3]],
            [
                "images.postgres",
                "images.postgres.build_args.PGBACKREST_VERSION",
                "images.postgres.build_args.PYPI_VERSION",
            ],
        )
        self.assertIsInstance(pins[1], AptPackagePin)
        self.assertIsInstance(pins[2], PypiProjectPin)
        self.assertEqual(pins[3].id, "host.registry_image")

    def test_model_registry_keeps_profiles_in_order_and_ids_distinct(self) -> None:
        image_result = load_image_lock_text(IMAGE_LOCK_TEXT)
        host_result = load_host_lock(ROOT / "host.lock")
        models_result = load_models_lock_text(
            _fictitious_models_lock_text(two_profiles=True)
        )
        self.assertTrue(image_result.ok)
        self.assertTrue(host_result.ok)
        self.assertTrue(models_result.ok)
        assert image_result.lock is not None
        assert host_result.lock is not None
        assert models_result.lock is not None
        provenance = _committed_provenance()
        registry = pin_registry(
            image_result.lock,
            host_result.lock,
            models_result.lock,
            provenance,
        )
        ids = [pin.id for pin in registry]
        model_ids = [pin_id for pin_id in ids if pin_id.startswith("models.")]
        self.assertEqual(
            model_ids,
            ["models.generator", "models.2x2v-2d.generator"],
        )
        first_model = ids.index(model_ids[0])
        first_skill = ids.index("skills.matt-pocock")
        self.assertGreater(first_model, ids.index("host.acceptance_vm_image"))
        self.assertLess(first_model, first_skill)

    def test_every_pin_bump_path_is_owned_by_that_pin(self) -> None:
        image = self._image_pin()
        built = self._built_pin()
        apt = AptPackagePin(
            "images.postgres.build_args.PGBACKREST_VERSION",
            "images.lock",
            built.image,
            "PGBACKREST_VERSION",
        )
        pypi_pin = self._pypi_pin()
        pypi_watch = pypi_pin.image.watch[pypi_pin.argument]
        self.assertIsInstance(pypi_watch, PypiWatch)
        assert isinstance(pypi_watch, PypiWatch)
        pypi_url = f"{pypi.PYPI_INDEX_ROOT}/{pypi_watch.project}/"
        registry = RegistryImagePin(
            "host.registry_image",
            "host.lock",
            "example:9000.0.0@sha256:" + "a" * 64,
        )
        runner = GithubReleasePin("host.gh_runner", "host.lock", "1000.0.0", "a" * 64)
        docker = MajorFloorPin(
            "host.minimums.docker", "host.lock", 29, "example/docker", r"^v", "minimums.docker"
        )
        toolkit = VersionFloorPin(
            "host.minimums.toolkit", "host.lock", "1000.0.0", "example/toolkit"
        )
        driver = DriverBranchPin(
            "host.driver.branch", "host.lock", "1000", "example/driver"
        )
        cloud = CloudImagePin(
            "host.acceptance_vm_image",
            "host.lock",
            "https://cloud-images.ubuntu.com/fictitious/20990101/fictitious-server-cloudimg-amd64.img",
            "a" * 64,
        )
        matt = self._fictitious_matt_pin()
        cases = (
            (image, image.resolve(self._registry_responses(image.image.source, ["1.0", "1.1"], "sha256:" + "b" * 64))),
            (built, built.resolve(self._registry_responses(built.image.base, ["1.0", "1.1"], "sha256:" + "d" * 64))),
            (apt, apt.resolve(DictFetcher({APT_INDEX: response(APT_PACKAGES)}))),
            (
                pypi_pin,
                pypi_pin.resolve(
                    DictFetcher(
                        {
                            pypi_url: response(
                                _pypi_reply(
                                    pypi_watch.project,
                                    pypi_pin.current(),
                                    "9000.0.1",
                                )
                            )
                        }
                    )
                ),
            ),
            (registry, registry.resolve(self._registry_responses(registry.image, ["9000.0.0", "9000.0.1"], "sha256:" + "b" * 64))),
            (runner, runner.resolve(self._release_fetcher(
                "actions/runner",
                "v1000.1.0",
                "<!-- BEGIN SHA linux-x64 -->\n" + "b" * 64 + "\n<!-- END SHA linux-x64 -->",
            ))),
            (docker, docker.resolve(self._release_fetcher("example/docker", "v30.0.0"))),
            (toolkit, toolkit.resolve(self._release_fetcher("example/toolkit", "v1000.1.0"))),
            (driver, driver.resolve(DictFetcher({
                "https://developer.download.nvidia.com/compute/cuda/repos/example/driver/Packages": response(
                    "Package: nvidia-driver-pinning-1010\n"
                )
            }))),
            (cloud, cloud.resolve(DictFetcher({
                "https://cloud-images.ubuntu.com/fictitious/": response('<a href="20990201/">new</a>'),
                "https://cloud-images.ubuntu.com/fictitious/20990201/SHA256SUMS": response(
                    "b" * 64 + " *fictitious-server-cloudimg-amd64.img\n"
                ),
            }))),
            (matt, matt.resolve(_fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"))),
            (_fictitious_model_pin(), _fictitious_model_pin().resolve(_fictitious_model_fetcher())),
        )
        for pin, bump in cases:
            with self.subTest(pin=pin.id):
                self.assertIsNotNone(bump)
                assert bump is not None
                paths = {
                    change.key_path for change in bump.changes
                } | {block.key_path for block in bump.blocks}
                self.assertTrue(paths <= set(pin.key_paths), (pin.id, paths))

    def test_pypi_pin_id_is_in_the_watched_vocabulary(self) -> None:
        pypi_pin = self._pypi_pin()
        vocabulary = notes.build_vocabulary((pypi_pin,), "")
        self.assertEqual(vocabulary.namespace(pypi_pin.id), "watched")


class PatchContracts(unittest.TestCase):
    IMAGE_TEXT = (
        "# lock comment\n"
        "version: 1\n"
        "images:\n"
        "  caddy:\n"
        "    source: docker.io/library/example:1.0 # source comment\n"
        "    digest: sha256:" + "e" * 64 + "\n"
    )

    def test_replace_scalar_preserves_quotes_comments_and_other_bytes(self) -> None:
        text = 'driver:\n  branch: "1000" # keep this comment\n'
        changed = replace_scalar(text, "driver.branch", "1010")
        self.assertEqual(changed, 'driver:\n  branch: "1010" # keep this comment\n')
        self.assertEqual(
            replace_scalar(
                self.IMAGE_TEXT, "images.caddy.source", "docker.io/library/example:1.1"
            ).replace("docker.io/library/example:1.1", ""),
            self.IMAGE_TEXT.replace("docker.io/library/example:1.0", ""),
        )
        with self.assertRaises(PatchError):
            replace_scalar(
                self.IMAGE_TEXT, "images.missing.digest", "sha256:" + "f" * 64
            )

    def test_pypi_bump_changes_one_value_and_inputs_digest(self) -> None:
        text = BUILT_TWO_KIND_IMAGE_LOCK_TEXT
        result = load_image_lock_text(text)
        self.assertTrue(result.ok)
        assert result.lock is not None
        image = result.lock.images[0]
        self.assertIsInstance(image, BuiltImagePin)
        assert isinstance(image, BuiltImagePin)
        old = image.build_args["PYPI_VERSION"]
        new = "9000.0.1"
        path = "images.postgres.build_args.PYPI_VERSION"
        bump = Bump(
            path,
            "images.lock",
            (Change(path, old, new),),
            "https://example.invalid/release",
            True,
        )
        patched = apply_bump(text, bump)
        self.assertEqual(
            patched,
            text.replace(
                f"      PYPI_VERSION: {old}\n",
                f"      PYPI_VERSION: {new}\n",
            ),
        )
        patched_result = load_image_lock_text(patched)
        self.assertTrue(patched_result.ok)
        assert patched_result.lock is not None
        patched_image = patched_result.lock.images[0]
        self.assertIsInstance(patched_image, BuiltImagePin)
        assert isinstance(patched_image, BuiltImagePin)
        dockerfile = b"FROM fictitious-base\n"
        self.assertNotEqual(
            compute_inputs_digest(
                patched_image.base_digest, patched_image.build_args, dockerfile
            ),
            compute_inputs_digest(image.base_digest, image.build_args, dockerfile),
        )

    def test_pypi_two_segment_and_digit_values_are_quoted(self) -> None:
        text = BUILT_TWO_KIND_IMAGE_LOCK_TEXT
        result = load_image_lock_text(text)
        self.assertTrue(result.ok)
        assert result.lock is not None
        image = result.lock.images[0]
        self.assertIsInstance(image, BuiltImagePin)
        assert isinstance(image, BuiltImagePin)
        old = image.build_args["PYPI_VERSION"]
        path = "images.postgres.build_args.PYPI_VERSION"
        for new in ("9000.1", "90001231"):
            with self.subTest(new=new):
                bump = Bump(
                    path,
                    "images.lock",
                    (Change(path, old, new),),
                    "https://example.invalid/release",
                    True,
                )
                patched = apply_bump(text, bump)
                self.assertEqual(
                    patched,
                    text.replace(
                        f"      PYPI_VERSION: {old}\n",
                        f'      PYPI_VERSION: "{new}"\n',
                    ),
                )
                patched_result = load_image_lock_text(patched)
                self.assertTrue(patched_result.ok)
                assert patched_result.lock is not None
                patched_image = patched_result.lock.images[0]
                self.assertIsInstance(patched_image, BuiltImagePin)
                assert isinstance(patched_image, BuiltImagePin)
                self.assertEqual(patched_image.build_args["PYPI_VERSION"], new)

    def test_replace_block_handles_nested_added_removed_and_quoted_paths(self) -> None:
        old_only = (
            "          old-only.txt:\n"
            "            sha256: sha256:" + "4" * 64 + "\n"
            "            size: 5\n"
        )
        text = _fictitious_models_lock_text().replace(
            f"          config.json:\n            sha256: {FICTITIOUS_MODEL_DIGEST}\n            size: 3\n",
            f"          config.json:\n            sha256: {FICTITIOUS_MODEL_DIGEST}\n            size: 3\n{old_only}",
        )
        text = text.replace(
            "        files:\n", "        # retain this comment\n        files:\n", 1
        )
        result = load_models_lock_text(text)
        self.assertTrue(result.ok)
        assert result.lock is not None
        model = result.lock.profiles[0].models[0]
        old_files = {
            file.path: {"sha256": file.sha256, "size": file.size}
            for file in model.files
        }
        new_files = {
            "config.json": {"sha256": FICTITIOUS_MODEL_DIGEST, "size": 3},
            "nested/file.txt": {"sha256": "sha256:" + "5" * 64, "size": 4},
        }
        bump = Bump(
            "models.generator",
            "models.lock",
            (
                Change(
                    "profiles.1x1v-1d.models.generator.revision",
                    model.revision,
                    FICTITIOUS_MODEL_HEAD,
                ),
            ),
            "https://huggingface.co/example/fictitious-model/tree/" + FICTITIOUS_MODEL_HEAD,
            False,
            blocks=(
                Block(
                    "profiles.1x1v-1d.models.generator.files", old_files, new_files
                ),
            ),
        )
        patched = apply_bump(text, bump)
        self.assertIn("        # retain this comment\n        files:\n", patched)
        patched_result = load_models_lock_text(patched)
        assert patched_result.lock is not None
        self.assertEqual(
            patched_result.lock.profiles[0].models[0].files[0].path, "config.json"
        )
        self.assertIn("nested/file.txt", patched)
        self.assertNotIn("old-only.txt", patched)

        quoted = "files:\n  old:\n    sha256: sha256:" + "4" * 64 + "\n    size: 5\n"
        quoted_files = {
            "2": {"sha256": "sha256:" + "6" * 64, "size": 0},
            "nested/file.txt": {"sha256": "sha256:" + "7" * 64, "size": 1},
        }
        quoted_patched = replace_block(
            quoted, "files", hub.render_files_block(0, quoted_files)
        )
        self.assertIn('  "2":\n', quoted_patched)
        self.assertEqual(
            cast(Mapping[str, object], yaml.safe_load(quoted_patched))["files"],
            quoted_files,
        )

    def test_block_patching_is_anchored_and_rejects_scalar_mismatches(self) -> None:
        pin = _fictitious_model_pin()
        block_path = f"profiles.{pin.profile}.models.{pin.model.role}.files"
        revision_path = f"profiles.{pin.profile}.models.{pin.model.role}.revision"
        new_files = {
            "config.json": {"sha256": FICTITIOUS_MODEL_DIGEST, "size": 3},
            "nested/file.txt": {"sha256": "sha256:" + "5" * 64, "size": 4},
        }
        bad = Bump(
            pin.id,
            pin.lock,
            (Change(revision_path, pin.model.revision, FICTITIOUS_MODEL_HEAD),),
            "",
            False,
            blocks=(Block(block_path, {"wrong": {"size": 1}}, new_files),),
        )
        with self.assertRaises(PatchError) as caught:
            apply_bump(FICTITIOUS_MODELS_LOCK_TEXT, bad)
        self.assertIn("anchored", str(caught.exception))
        with self.assertRaises(PatchError):
            replace_block(FICTITIOUS_MODELS_LOCK_TEXT, revision_path, [])
        with self.assertRaises(PatchError):
            replace_scalar(FICTITIOUS_MODELS_LOCK_TEXT, block_path, "scalar")

    def test_apply_bump_revalidates_and_guards_other_values(self) -> None:
        bump = Bump(
            "images.caddy",
            "images.lock",
            (
                Change(
                    "images.caddy.source",
                    "docker.io/library/example:1.0",
                    "docker.io/library/example:1.1",
                ),
                Change(
                    "images.caddy.digest", "sha256:" + "e" * 64, "sha256:" + "f" * 64
                ),
            ),
            "https://hub.docker.com/_/example/tags",
            False,
        )
        changed = apply_bump(self.IMAGE_TEXT, bump)
        self.assertIn("example:1.1", changed)
        self.assertIn("sha256:" + "f" * 64, changed)
        self.assertIn("# lock comment", changed)
        invalid = Bump(
            "images.caddy",
            "images.lock",
            (Change("images.caddy.digest", "sha256:" + "e" * 64, "sha256:bad"),),
            "",
            False,
        )
        with self.assertRaises(PatchError) as caught:
            apply_bump(self.IMAGE_TEXT, invalid)
        self.assertIn("sha256:<64", str(caught.exception))

        real_replace = patch_module.replace_scalar

        def corrupt(text: str, key_path: str, new_value: str) -> str:
            text = text.replace("version: 1", "version: 2")
            return real_replace(text, key_path, new_value)

        guard_bump = Bump(
            "images.caddy",
            "images.lock",
            (
                Change(
                    "images.caddy.digest",
                    "sha256:" + "e" * 64,
                    "sha256:" + "f" * 64,
                ),
            ),
            "",
            False,
        )
        with (
            mock_patch.object(patch_module, "replace_scalar", side_effect=corrupt),
            self.assertRaises(PatchError) as caught,
        ):
            apply_bump(self.IMAGE_TEXT, guard_bump)
        self.assertIn("outside the bump", str(caught.exception))

    def test_provenance_patcher_edits_only_its_line_and_requires_one(self) -> None:
        changes = (
            Change("matt-pocock.commit", "ddddddd", "eeeeeee"),
            Change("matt-pocock.date", "2099-01-02", "2099-02-03"),
        )
        text = "before\n" + FICTITIOUS_PROVENANCE_TEXT + "after\n"
        patched = patch_provenance(text, changes)
        self.assertEqual(
            parse_provenance(patched).matt_commit,
            "eeeeeee",
        )
        self.assertEqual(parse_provenance(patched).matt_date, "2099-02-03")
        self.assertEqual(patched.splitlines()[0], "before")
        self.assertEqual(patched.splitlines()[-1], "after")
        with self.assertRaises(SkillsRecordError):
            patch_provenance(
                text.replace("Provenance today:", "Record:", 1),
                changes,
            )
        with self.assertRaises(SkillsRecordError):
            patch_provenance(
                text + FICTITIOUS_PROVENANCE_TEXT,
                changes,
            )

    def test_apply_bump_dispatches_the_skill_record_and_surfaces_its_errors(self) -> None:
        provenance_bump = Bump(
            "skills.matt-pocock",
            "docs/agents/tooling.md",
            (
                Change("matt-pocock.commit", "ddddddd", "eeeeeee"),
                Change("matt-pocock.date", "2099-01-02", "2099-02-03"),
            ),
            "https://github.com/example/commit/eeeeeee",
            True,
        )
        patched_provenance = apply_bump(FICTITIOUS_PROVENANCE_TEXT, provenance_bump)
        self.assertEqual(parse_provenance(patched_provenance).matt_date, "2099-02-03")
        with self.assertRaises(PatchError) as caught:
            apply_bump(
                FICTITIOUS_PROVENANCE_TEXT.replace("Provenance today:", "Record:", 1),
                provenance_bump,
            )
        self.assertIn("exactly one provenance line", str(caught.exception))

    def test_record_values_and_carries_read_the_skill_record(self) -> None:
        provenance_bump = Bump(
            "skills.matt-pocock",
            "docs/agents/tooling.md",
            (
                Change("matt-pocock.commit", "ddddddd", "eeeeeee"),
                Change("matt-pocock.date", "2099-01-02", "2099-02-03"),
            ),
            "",
            True,
        )
        self.assertEqual(
            record_values(FICTITIOUS_PROVENANCE_TEXT, provenance_bump),
            ("ddddddd", "2099-01-02"),
        )
        self.assertFalse(carries(FICTITIOUS_PROVENANCE_TEXT, provenance_bump))
        self.assertTrue(
            carries(
                apply_bump(FICTITIOUS_PROVENANCE_TEXT, provenance_bump),
                provenance_bump,
            )
        )
        self.assertIsNone(record_values("not a record", provenance_bump))
        self.assertIsNone(
            record_values(
                FICTITIOUS_PROVENANCE_TEXT, replace(provenance_bump, lock="images.lock")
            )
        )


class PinWatchHost:
    """A recording Host serving origin/main's four pin-watch records."""

    def __init__(
        self,
        prs: list[dict[str, object]] | None = None,
        *,
        image_text: str | None = None,
        host_text: str | None = None,
        models_text: str | None = None,
        tooling_text: str | None = None,
        branch_exists: bool = False,
        branch_text: str = "",
        branch_parent: str = "main",
        main_commit: str = "main",
        branch_head: str = "branch-head",
        rev_list_count: int | str = 1,
        shallow: bool = False,
    ) -> None:
        self.prs = json.dumps(prs or [])
        self.image_text = image_text or IMAGE_LOCK_TEXT
        self.host_text = host_text or (ROOT / "host.lock").read_text()
        self.models_text = (
            models_text
            if models_text is not None
            else (ROOT / "models.lock").read_text()
        )
        self.tooling_text = (
            tooling_text
            if tooling_text is not None
            else _committed_tooling_text()
        )
        self.branch_exists = branch_exists
        self.branch_text = branch_text
        self.branch_parent = branch_parent
        self.main_commit = main_commit
        self.branch_head = branch_head
        self.rev_list_count = str(rev_list_count)
        self.shallow = shallow
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.files: dict[str, str] = {
            "/repo/models.lock": (ROOT / "models.lock").read_text(),
            "/repo/requirements-dev.txt": (ROOT / "requirements-dev.txt").read_text(),
        }
        self.fail_once: dict[tuple[str, ...], str] = {}

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, cwd, env, timeout
        command = tuple(argv)
        self.calls.append((command, input))
        for prefix, stderr in list(self.fail_once.items()):
            if command[: len(prefix)] == prefix:
                del self.fail_once[prefix]
                return subprocess.CompletedProcess(command, 1, "", stderr)
        if command[:3] == ("git", "show", "origin/main:images.lock"):
            return subprocess.CompletedProcess(command, 0, self.image_text, "")
        if command[:3] == ("git", "show", "origin/main:host.lock"):
            return subprocess.CompletedProcess(command, 0, self.host_text, "")
        if command[:3] == ("git", "show", "origin/main:models.lock"):
            return subprocess.CompletedProcess(command, 0, self.models_text, "")
        if command[:3] == ("git", "show", "origin/main:docs/agents/tooling.md"):
            return subprocess.CompletedProcess(command, 0, self.tooling_text, "")
        if command[:2] == ("gh", "pr") and command[2:4] == ("list", "--base"):
            return subprocess.CompletedProcess(command, 0, self.prs, "")
        if command[:3] == ("git", "rev-list", "--count"):
            return subprocess.CompletedProcess(
                command, 0, self.rev_list_count + "\n", ""
            )
        if command == ("git", "rev-parse", "--is-shallow-repository"):
            return subprocess.CompletedProcess(
                command, 0, ("true" if self.shallow else "false") + "\n", ""
            )
        if command[:4] == ("git", "rev-parse", "--verify", "--quiet"):
            return subprocess.CompletedProcess(
                command,
                0 if self.branch_exists else 1,
                self.branch_head + "\n" if self.branch_exists else "",
                "",
            )
        if command[:2] == ("git", "show") and command[2].startswith(
            "origin/pin-watch/"
        ):
            return subprocess.CompletedProcess(command, 0, self.branch_text, "")
        if command == ("git", "rev-parse", "origin/main"):
            return subprocess.CompletedProcess(command, 0, self.main_commit + "\n", "")
        if command[:2] == ("git", "rev-parse") and command[2].endswith("^"):
            return subprocess.CompletedProcess(
                command, 0, self.branch_parent + "\n", ""
            )
        if command[:2] == ("gh", "pr") and command[2] == "create":
            return subprocess.CompletedProcess(
                command, 0, "https://github.com/acme/repo/pull/99\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        return self.files[str(path)]

    def write_text(
        self,
        path: PathLike,
        text: str,
        *,
        encoding: str = "utf-8",
        mode: int = 0o644,
    ) -> None:
        del encoding, mode
        self.files[str(path)] = text

    def exists(self, path: PathLike) -> bool:
        return str(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        directory = Path(path)
        return [
            Path(entry).name
            for entry in self.files
            if Path(entry).parent == directory
        ]

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        del missing_ok
        self.files.pop(str(path), None)

    def stat(self, path: PathLike) -> os.stat_result:
        del path
        raise FileNotFoundError

    def chmod(self, path: PathLike, mode: int) -> None:
        del path, mode

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        del path, uid, gid

    def mkdir(
        self,
        path: PathLike,
        *,
        mode: int = 0o755,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        del path, mode, parents, exist_ok

    def geteuid(self) -> int:
        return 1000


def _cli_output(
    host: PinWatchHost, fetcher: DictFetcher, argv: list[str]
) -> tuple[int, str, str]:
    _add_committed_skill_sources(fetcher)
    stdout, stderr = StringIO(), StringIO()
    with mock_patch("sys.stdout", stdout), mock_patch("sys.stderr", stderr):
        result = pinwatch_main(argv, host=host, fetcher=fetcher, root=Path("/repo"))
    return result, stdout.getvalue(), stderr.getvalue()


class PullRequestContracts(unittest.TestCase):
    def test_frontend_bump_body_names_the_two_mode_proof(self) -> None:
        sentence = (
            "Before the merge, this bump is proven on the box with the turn "
            "harness in both modes on `eval/seed/general/frontend-bump.yaml`, "
            "per the weekly review in `docs/runbooks/pin-watch-app-setup.md`."
        )
        frontend = Bump(
            "images.open-webui",
            "images.lock",
            (Change("images.open-webui.digest", "sha256:" + "a" * 64, "sha256:" + "b" * 64),),
            "https://example.invalid/frontend",
            False,
        )
        self.assertIn(sentence, body_for(frontend, ()))

        other_image = Bump(
            "images.caddy",
            "images.lock",
            (Change("images.caddy.digest", "sha256:" + "a" * 64, "sha256:" + "b" * 64),),
            "https://example.invalid/caddy",
            False,
        )
        driver = Bump(
            "host.driver.branch",
            "host.lock",
            (Change("driver.branch", "example-old", "example-new"),),
            "https://example.invalid/driver",
            False,
        )
        self.assertNotIn(sentence, body_for(other_image, ()))
        self.assertNotIn(sentence, body_for(driver, ()))

    def test_pr_helpers_render_and_withdraw_idempotently(self) -> None:
        bump = Bump(
            "images.postgres",
            "images.lock",
            (
                Change(
                    "images.postgres.digest", "sha256:" + "a" * 64, "sha256:" + "b" * 64
                ),
            ),
            "https://hub.docker.com/_/postgres/tags",
            True,
        )
        self.assertEqual(
            title_for(bump),
            "pin watch: images.postgres digest sha256:aaaaaaaaaaaa → sha256:bbbbbbbbbbbb",
        )
        body = body_for(bump, ())
        self.assertIn("`PGDATA`", body)
        self.assertIn("mirror-images", body)
        self.assertNotIn("upstream release", body)
        pr = PullRequest(
            7,
            "https://example/pr/7",
            "[withdrawn] old",
            "pin-watch/x",
            "OPEN",
            None,
            None,
        )
        host = PinWatchHost()
        withdraw(host, Path("/repo"), pr, "no longer current")
        self.assertEqual(
            [call[0] for call in host.calls],
            [
                ("gh", "pr", "edit", "7", "--title", "[withdrawn] old"),
                (
                    "gh",
                    "pr",
                    "close",
                    "7",
                    "--comment",
                    "no longer current",
                    "--delete-branch",
                ),
            ],
        )

    def test_built_proposal_body_describes_the_on_box_completion(self) -> None:
        bump = Bump(
            "images.postgres.build_args.PGBACKREST_VERSION",
            "images.lock",
            (
                Change(
                    "images.postgres.build_args.PGBACKREST_VERSION",
                    "1000.0.0-1.example",
                    "1000.0.1-1.example",
                ),
            ),
            APT_INDEX,
            True,
        )
        body = body_for(bump, ())
        self.assertIn("python3 -m tools.imagebuild <name> --to <registry>", body)
        self.assertIn("inputs_digest", body)
        self.assertIn("watch then reports the branch completed", body)
        self.assertNotIn("An image major needs an upgrade plan", body)

    def test_body_lists_a_bound_research_note(self) -> None:
        """A bound note records its verified and proposed versions in the body."""

        bump = Bump(
            "images.caddy",
            "images.lock",
            (
                Change(
                    "images.caddy.source",
                    "registry.example/fictitious:9000.0.0",
                    "registry.example/fictitious:9000.0.1",
                ),
            ),
            "https://example.invalid/releases",
            True,
        )
        note = notes.NoteBinding(
            "docs/research/example.md", "images.caddy", "9000.0.0"
        )
        body = body_for(bump, (note,))
        self.assertIn("### Research notes verified against this pin", body)
        self.assertIn(
            "These notes were verified against this pin and are read against the proposed version before the merge",
            body,
        )
        self.assertIn("| Note | Verified against | Proposed |", body)
        self.assertIn(
            "| `docs/research/example.md` | 9000.0.0 | 9000.0.1 |", body
        )

    def test_body_says_when_no_research_note_is_bound(self) -> None:
        """A pin without notes still carries the section and its none line."""

        bump = Bump(
            "images.caddy",
            "images.lock",
            (Change("images.caddy.digest", "9000.0.0", "9000.0.1"),),
            "https://example.invalid/releases",
            True,
        )
        body = body_for(bump, ())
        self.assertIn("### Research notes verified against this pin", body)
        self.assertIn("No research note under `docs/research/` names this pin.", body)
        self.assertNotIn("| Note | Verified against | Proposed |", body)

    def test_proposed_version_uses_the_first_version_bearing_change(self) -> None:
        """Version-bearing key paths yield the new token, or the digest-only text."""

        cases = (
            (
                "source tag",
                Change(
                    "images.example.source",
                    "registry.example/fictitious:9000.0.0",
                    "registry.example/fictitious:9000.0.1",
                ),
                "9000.0.1",
            ),
            (
                "registry tag with digest",
                Change(
                    "registry_image",
                    "registry.example/fictitious:9000.0.0@sha256:aaa",
                    "registry.example/fictitious:9000.0.0@sha256:bbb",
                ),
                "unchanged (the digest alone moves)",
            ),
            (
                "digest only",
                Change("images.example.digest", "sha256:aaa", "sha256:bbb"),
                "unchanged (the digest alone moves)",
            ),
            (
                "runner version",
                Change("gh_runner.version", "9000.0.0", "9000.0.1"),
                "9000.0.1",
            ),
            (
                "skill ref",
                Change("skills.example.ref", "9000.0.0", "9000.0.1"),
                "9000.0.1",
            ),
            (
                "build argument",
                Change("images.example.build_args.VERSION", "9000.0.0", "9000.0.1"),
                "9000.0.1",
            ),
        )
        for label, change, expected in cases:
            with self.subTest(change=label):
                bump = Bump(
                    "example",
                    "images.lock",
                    (change,),
                    "https://example.invalid/releases",
                    True,
                )
                self.assertEqual(proposed_version(bump), expected)

    def test_model_revision_title_token_and_body_cover_the_block_and_memory_row(self) -> None:
        old_revision = "a" * 40
        new_revision = "b" * 40
        model_path = "profiles.1x1v-1d.models.generator"
        files_path = f"{model_path}.files"
        bump = Bump(
            "models.generator",
            "models.lock",
            (Change(f"{model_path}.revision", old_revision, new_revision),),
            f"https://huggingface.co/example/fictitious-model/tree/{new_revision}",
            True,
            blocks=(
                Block(
                    files_path,
                    {"old.bin": {"sha256": "sha256:" + "1" * 64, "size": 3}},
                    {
                        "new.bin": {"sha256": "sha256:" + "2" * 64, "size": 7},
                        "nested/file.txt": {
                            "sha256": "sha256:" + "3" * 64,
                            "size": 0,
                        },
                    },
                ),
            ),
        )
        self.assertEqual(
            title_for(bump),
            f"pin watch: models.generator revision {old_revision[:12]} → {new_revision[:12]}",
        )
        self.assertEqual(version_token(f"{model_path}.revision", new_revision), new_revision)
        self.assertEqual(proposed_version(bump), new_revision)
        body = body_for(
            bump,
            (),
            memory_rows=(host_models.MemoryRow("generator", 7, "generator"),),
        )
        self.assertIn(f"| `{files_path}` | 1 files, 3 bytes | 2 files, 7 bytes |", body)
        self.assertIn("only on a box whose site file selects this pin's profile", body)
        self.assertIn("maintenance window from go-live", body)
        self.assertIn("role `generator`", body)
        self.assertIn("7 GB", body)
        self.assertIn("Re-judge the profile's memory row", body)
        self.assertIn("python3 tests/regenerate_render_fixtures.py", body)

    def test_bound_to_preserves_reading_order(self) -> None:
        """The join returns only one pin's bindings in their source order."""

        bindings = (
            notes.NoteBinding("docs/research/first.md", "example.pin", "9000.0.0"),
            notes.NoteBinding("docs/research/other.md", "other.pin", "9000.0.1"),
            notes.NoteBinding("docs/research/second.md", "example.pin", "9000.0.2"),
        )
        self.assertEqual(
            notes.bound_to(bindings, "example.pin"), (bindings[0], bindings[2])
        )
        self.assertEqual(notes.bound_to(bindings, "missing.pin"), ())

    def test_runner_bump_body_names_the_manual_upgrade_path(self) -> None:
        old_sha = "a" * 64
        new_sha = "b" * 64
        runner = Bump(
            "host.gh_runner",
            "host.lock",
            (
                Change("gh_runner.version", "1000.0.0", "1000.1.0"),
                Change("gh_runner.sha256", old_sha, new_sha),
            ),
            "https://github.com/actions/runner/releases/tag/v1000.1.0",
            False,
        )
        body = body_for(runner, ())
        self.assertIn("automatic updates disabled", body)
        self.assertIn("bring the checkout that runs provision to the merged commit", body)
        self.assertIn("sudo python3 -m gideon host provision", body)
        self.assertIn("thirty days", body)
        self.assertIn("ci-runner.md", body)
        driver = Bump(
            "host.driver.branch",
            "host.lock",
            (Change("driver.branch", "1000", "1001"),),
            "https://example.invalid/Packages",
            True,
        )
        self.assertNotIn("automatic updates disabled", body_for(driver, ()))

    def test_skill_proposal_body_names_its_completion_recipe(self) -> None:
        provenance = parse_provenance(FICTITIOUS_PROVENANCE_TEXT)
        matt_pin = SkillBranchPin(
            "skills.matt-pocock",
            "docs/agents/tooling.md",
            commit=provenance.matt_commit,
            date=provenance.matt_date,
        )
        matt_bump = matt_pin.resolve(
            _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03")
        )
        assert matt_bump is not None
        matt_body = body_for(matt_bump, ())
        self.assertIn(
            "per the skills refresh recipe in `docs/agents/tooling.md` before merging",
            matt_body,
        )
        self.assertIn(
            "python3 -m tools.pinwatch.skills --source mattpocock/skills <clone>",
            matt_body,
        )
        self.assertIn("No hosted check can tell", matt_body)
        self.assertNotIn("tests/fixtures/render", matt_body)
        self.assertNotIn("This is a proposal:", matt_body)

    def test_skill_push_adds_only_the_record(self) -> None:
        provenance = parse_provenance(FICTITIOUS_PROVENANCE_TEXT)
        record = "docs/agents/tooling.md"
        resolved = SkillBranchPin(
            "skills.matt-pocock",
            "docs/agents/tooling.md",
            commit=provenance.matt_commit,
            date=provenance.matt_date,
        ).resolve(_fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"))
        assert resolved is not None
        patched = apply_bump(FICTITIOUS_PROVENANCE_TEXT, resolved)
        host = PinWatchHost()
        push_bump(host, Path("/repo"), "pin-watch/test", record, patched, "title")
        self.assertEqual(
            [call[0] for call in host.calls],
            [
                ("git", "checkout", "-B", "pin-watch/test", "origin/main"),
                ("git", "add", record),
                ("git", "commit", "-m", "title"),
                ("git", "push", "--force", "origin", "pin-watch/test"),
                ("git", "checkout", "--detach", "origin/main"),
            ],
        )
        self.assertEqual(
            set(host.files),
            {f"/repo/{record}", "/repo/models.lock", "/repo/requirements-dev.txt"},
        )

    def test_pull_request_closed_lookup_and_branch_state(self) -> None:
        prs = PullRequests(
            (
                PullRequest(1, "open", "open", "pin-watch/x", "OPEN", None, None),
                PullRequest(
                    2,
                    "old",
                    "old",
                    "pin-watch/x",
                    "CLOSED",
                    None,
                    "2026-01-01T00:00:00Z",
                ),
                PullRequest(
                    3,
                    "new",
                    "new",
                    "pin-watch/x",
                    "MERGED",
                    "2026-03-01T00:00:00Z",
                    "2026-03-01T00:00:00Z",
                ),
            )
        )
        opened = prs.open_for("pin-watch/x")
        last = prs.last_closed_for("pin-watch/x")
        self.assertEqual((opened and opened.number, last and last.number), (1, 3))
        host = PinWatchHost(
            branch_exists=True,
            branch_text="patched",
            branch_parent="same",
            main_commit="same",
        )
        self.assertEqual(
            branch_state(host, Path("/repo"), "pin-watch/x", "images.lock", "patched").state,
            "identical",
        )

    def test_completed_branch_is_detected_before_parent_tip(self) -> None:
        old_digest = "sha256:" + "c" * 64
        old_inputs = "sha256:" + "b" * 64
        completed = BUILT_IMAGE_LOCK_TEXT.replace(old_digest, "sha256:" + "d" * 64).replace(
            old_inputs, "sha256:" + "e" * 64
        )
        host = PinWatchHost(
            branch_exists=True,
            branch_text=completed,
            branch_parent="old-main",
            main_commit="new-main",
            branch_head="sha256:branch-head",
        )
        state = branch_state(
            host,
            Path("/repo"),
            "pin-watch/images.postgres.build_args.PGBACKREST_VERSION",
            "images.lock",
            BUILT_IMAGE_LOCK_TEXT,
            bump=PGBACKREST_BUMP,
        )
        self.assertEqual(state.state, "completed")
        self.assertEqual(state.head_sha, "sha256:branch-head")
        self.assertEqual(state.completion_digest, "sha256:" + "d" * 64)

    def test_completion_survives_an_unrelated_lock_change_on_main(self) -> None:
        """Only the bump's own paths decide: main moving elsewhere is the human's rebase."""

        completed = BUILT_IMAGE_LOCK_TEXT.replace("sha256:" + "c" * 64, "sha256:" + "d" * 64).replace(
            "sha256:" + "b" * 64, "sha256:" + "e" * 64
        )
        # main gained a different base digest since the proposal was completed.
        generated_on_new_main = BUILT_IMAGE_LOCK_TEXT.replace("sha256:" + "a" * 64, "sha256:" + "f" * 64)
        host = PinWatchHost(branch_exists=True, branch_text=completed, branch_parent="old-main", main_commit="new-main")
        state = branch_state(
            host,
            Path("/repo"),
            "pin-watch/images.postgres.build_args.PGBACKREST_VERSION",
            "images.lock",
            generated_on_new_main,
            bump=PGBACKREST_BUMP,
        )
        self.assertEqual(state.state, "completed")
        self.assertEqual(state.completion_digest, "sha256:" + "d" * 64)

    def test_unbuilt_or_untouched_digests_are_not_a_completion(self) -> None:
        old_digest = "sha256:" + "c" * 64
        old_inputs = "sha256:" + "b" * 64
        for label, branch_text in (
            ("unbuilt", BUILT_IMAGE_LOCK_TEXT.replace(old_digest, "unbuilt").replace(old_inputs, "unbuilt")),
            ("main's own digests", BUILT_IMAGE_LOCK_TEXT.replace("1000.0.0-1.example", "1000.0.9-1.example")),
        ):
            with self.subTest(branch=label):
                host = PinWatchHost(
                    branch_exists=True,
                    branch_text=branch_text,
                    branch_parent="old-main",
                    main_commit="new-main",
                )
                state = branch_state(
                    host,
                    Path("/repo"),
                    "pin-watch/images.postgres",
                    "images.lock",
                    BUILT_IMAGE_LOCK_TEXT,
                    bump=PGBACKREST_BUMP,
                )
                self.assertEqual(state.state, "stale")
                self.assertIsNone(state.completion_digest)


class CliContracts(unittest.TestCase):
    def _notes_host(self, note_text: str) -> PinWatchHost:
        host = PinWatchHost()
        host.files.update(
            {
                "/repo/images.lock": IMAGE_LOCK_TEXT,
                "/repo/host.lock": (ROOT / "host.lock").read_text(),
                "/repo/docs/agents/tooling.md": _committed_tooling_text(),
                "/repo/docs/research/example.md": note_text,
            }
        )
        return host

    def _caddy_fetcher(self, tag: str, digest: str) -> DictFetcher:
        tags = "https://registry-1.docker.io/v2/library/example/tags/list?n=1000"
        head = f"https://registry-1.docker.io/v2/library/example/manifests/{tag}"
        return DictFetcher(
            {
                tags: response({"tags": ["1.0", tag]}),
                head: response(headers={"Docker-Content-Digest": digest}),
            }
        )

    def _matt_bump(
        self, sha: str = "e" * 40, date: str = "2099-02-03"
    ) -> Bump:
        provenance = parse_provenance(FICTITIOUS_PROVENANCE_TEXT)
        pin = SkillBranchPin(
            "skills.matt-pocock",
            "docs/agents/tooling.md",
            commit=provenance.matt_commit,
            date=provenance.matt_date,
        )
        bump = pin.resolve(_fictitious_skill_fetcher(matt_sha=sha, matt_date=date))
        assert bump is not None
        return bump

    @staticmethod
    def _completed_at(bump: Bump, commit: str, date: str) -> Bump:
        """The bump a person's completion holds: *bump*'s values as the old ones."""

        commit_change, date_change = bump.changes
        return replace(
            bump,
            changes=(
                replace(commit_change, old=commit_change.new, new=commit),
                replace(date_change, old=date_change.new, new=date),
            ),
        )

    def _skill_pr(
        self,
        pin_id: str,
        title: str,
        *,
        state: str = "OPEN",
        number: int = 27,
    ) -> dict[str, object]:
        closed = None if state == "OPEN" else "2026-09-01T00:00:00Z"
        return {
            "number": number,
            "url": f"https://github.com/acme/repo/pull/{number}",
            "title": title,
            "headRefName": f"pin-watch/{pin_id}",
            "state": state,
            "mergedAt": closed if state == "MERGED" else None,
            "closedAt": closed,
        }

    def test_skill_proposal_is_a_dry_run_row(self) -> None:
        host = PinWatchHost(tooling_text=FICTITIOUS_PROVENANCE_TEXT)
        result, stdout, stderr = _cli_output(
            host,
            _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
            ["--only", "skills.matt-pocock", "--dry-run"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn(
            "skills.matt-pocock: would opened — ddddddd → eeeeeee (proposal)", stdout
        )
        self.assertEqual(
            set(host.files),
            {"/repo/models.lock", "/repo/requirements-dev.txt"},
        )

    def test_pypi_project_proposal_is_a_dry_run_row(self) -> None:
        image_result = load_image_lock_text(BUILT_TWO_KIND_IMAGE_LOCK_TEXT)
        assert image_result.lock is not None
        image = image_result.lock.images[0]
        assert isinstance(image, BuiltImagePin)
        watch = image.watch["PYPI_VERSION"]
        assert isinstance(watch, PypiWatch)
        pin_id = "images.postgres.build_args.PYPI_VERSION"
        current = image.build_args["PYPI_VERSION"]
        candidate = "9000.0.1"
        url = f"{pypi.PYPI_INDEX_ROOT}/{watch.project}/"
        fetcher = DictFetcher(
            {url: response(_pypi_reply(watch.project, current, candidate))}
        )
        host = PinWatchHost(image_text=BUILT_TWO_KIND_IMAGE_LOCK_TEXT)
        result, stdout, stderr = _cli_output(
            host,
            fetcher,
            ["--only", pin_id, "--dry-run"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn(
            f"{pin_id}: would opened — {current} → {candidate} (proposal)",
            stdout,
        )

    def test_skill_watch_commit_only_is_unchanged_or_updated_after_main_moves(self) -> None:
        bump = self._matt_bump()
        patched = apply_bump(FICTITIOUS_PROVENANCE_TEXT, bump)
        title = title_for(bump)
        for parent, main, expected, pushes in (
            ("main", "main", "unchanged", False),
            ("old-main", "new-main", "updated", True),
        ):
            with self.subTest(parent=parent):
                host = PinWatchHost(
                    prs=[self._skill_pr("skills.matt-pocock", title)],
                    tooling_text=FICTITIOUS_PROVENANCE_TEXT,
                    branch_exists=True,
                    branch_text=patched,
                    branch_parent=parent,
                    main_commit=main,
                    rev_list_count=1,
                )
                result, stdout, _stderr = _cli_output(
                    host,
                    _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
                    ["--only", "skills.matt-pocock"],
                )
                self.assertEqual(result, 0, _stderr)
                self.assertIn(f"skills.matt-pocock: {expected}", stdout)
                self.assertEqual(
                    any(call[0][:3] == ("git", "push", "--force") for call in host.calls),
                    pushes,
                )

    def test_human_skill_branch_is_completed_without_push_or_edit(self) -> None:
        bump = self._matt_bump()
        patched = apply_bump(FICTITIOUS_PROVENANCE_TEXT, bump)
        title = title_for(bump)
        host = PinWatchHost(
            prs=[self._skill_pr("skills.matt-pocock", title)],
            tooling_text=FICTITIOUS_PROVENANCE_TEXT,
            branch_exists=True,
            branch_text=patched,
            branch_head="human-head",
            rev_list_count=2,
        )
        result, stdout, stderr = _cli_output(
            host,
            _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
            ["--only", "skills.matt-pocock"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("skills.matt-pocock: completed — human-head", stdout)
        self.assertFalse(
            any(call[0][:2] == ("git", "push") or call[0][:3] == ("gh", "pr", "edit") for call in host.calls)
        )

    def test_human_skill_branch_is_retitled_from_its_record(self) -> None:
        bump = self._matt_bump()
        branch_bump = self._completed_at(bump, "fffffff", "2099-03-04")
        branch_text = apply_bump(
            apply_bump(FICTITIOUS_PROVENANCE_TEXT, bump), branch_bump
        )
        pr = self._skill_pr("skills.matt-pocock", "old title")
        host = PinWatchHost(
            prs=[pr],
            tooling_text=FICTITIOUS_PROVENANCE_TEXT,
            branch_exists=True,
            branch_text=branch_text,
            branch_head="human-head",
            rev_list_count=2,
        )
        result, stdout, stderr = _cli_output(
            host,
            _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
            ["--only", "skills.matt-pocock"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn(
            "skills.matt-pocock: completed — human-head — PR "
            "https://github.com/acme/repo/pull/27 retitled to fffffff; upstream now eeeeeee",
            stdout,
        )
        edit_calls = [call for call in host.calls if call[0][:3] == ("gh", "pr", "edit")]
        self.assertEqual(len(edit_calls), 1)
        body = edit_calls[0][1] or ""
        self.assertIn("fffffff", body)
        self.assertNotIn("tests/fixtures/render", body)
        # The body describes what the branch holds, its upstream page included.
        self.assertIn(f"https://github.com/{MATT_SOURCE}/commit/fffffff", body)
        self.assertNotIn("/commit/eeeeeee", body)

        dry_host = PinWatchHost(
            prs=[pr],
            tooling_text=FICTITIOUS_PROVENANCE_TEXT,
            branch_exists=True,
            branch_text=branch_text,
            branch_head="human-head",
            rev_list_count=2,
        )
        result, stdout, stderr = _cli_output(
            dry_host,
            _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
            ["--only", "skills.matt-pocock", "--dry-run"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("would retitled to fffffff; upstream now eeeeeee", stdout)
        self.assertFalse(any(call[0][:3] == ("gh", "pr", "edit") for call in dry_host.calls))

    def test_human_skill_branch_without_a_readable_record_is_left_alone(self) -> None:
        """The commit count is judged before a missing record can read as stale."""

        bump = self._matt_bump()
        host = PinWatchHost(
            prs=[self._skill_pr("skills.matt-pocock", title_for(bump))],
            tooling_text=FICTITIOUS_PROVENANCE_TEXT,
            branch_exists=True,
            branch_head="human-head",
            rev_list_count=3,
        )
        host.fail_once[
            ("git", "show", "origin/pin-watch/skills.matt-pocock:docs/agents/tooling.md")
        ] = "fatal: path 'docs/agents/tooling.md' does not exist"
        result, stdout, stderr = _cli_output(
            host,
            _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
            ["--only", "skills.matt-pocock"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn(
            "skills.matt-pocock: completed — human-head; upstream now eeeeeee",
            stdout,
        )
        self.assertFalse(
            any(call[0][:2] in {("git", "push"), ("git", "checkout")} for call in host.calls)
        )
        self.assertFalse(any(call[0][:3] == ("gh", "pr", "edit") for call in host.calls))

    def test_human_skill_branch_reports_newer_upstream_and_only_edits_bad_title(self) -> None:
        initial = self._matt_bump()
        branch_bump = self._completed_at(initial, "fffffff", "2099-03-04")
        branch_text = apply_bump(
            apply_bump(FICTITIOUS_PROVENANCE_TEXT, initial), branch_bump
        )
        current = self._matt_bump("9" * 40, "2099-04-05")
        branch_title = title_for(
            replace(
                current,
                changes=tuple(
                    replace(change, new=value)
                    for change, value in zip(
                        current.changes, ("fffffff", "2099-03-04"), strict=True
                    )
                ),
            )
        )
        for title, edits in ((branch_title, False), ("wrong title", True)):
            with self.subTest(title=title):
                host = PinWatchHost(
                    prs=[self._skill_pr("skills.matt-pocock", title)],
                    tooling_text=FICTITIOUS_PROVENANCE_TEXT,
                    branch_exists=True,
                    branch_text=branch_text,
                    branch_head="human-head",
                    rev_list_count=2,
                )
                result, stdout, _stderr = _cli_output(
                    host,
                    _fictitious_skill_fetcher(matt_sha="9" * 40, matt_date="2099-04-05"),
                    ["--only", "skills.matt-pocock"],
                )
                self.assertEqual(result, 0, _stderr)
                self.assertIn(
                    "completed — human-head" + (
                        " — PR https://github.com/acme/repo/pull/27 retitled to fffffff"
                        if edits
                        else ""
                    ) + "; upstream now 9999999",
                    stdout,
                )
                self.assertEqual(
                    any(call[0][:3] == ("gh", "pr", "edit") for call in host.calls),
                    edits,
                )

    def test_completed_skill_with_unparseable_or_main_record_is_left_alone(self) -> None:
        for label, branch_text in (
            ("unparseable", "not a skills record\n"),
            ("main value", FICTITIOUS_PROVENANCE_TEXT),
        ):
            with self.subTest(record=label):
                host = PinWatchHost(
                    prs=[self._skill_pr("skills.matt-pocock", "old title")],
                    tooling_text=FICTITIOUS_PROVENANCE_TEXT,
                    branch_exists=True,
                    branch_text=branch_text,
                    branch_head="human-head",
                    rev_list_count=2,
                )
                result, stdout, _stderr = _cli_output(
                    host,
                    _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
                    ["--only", "skills.matt-pocock"],
                )
                self.assertEqual(result, 0, _stderr)
                self.assertIn("skills.matt-pocock: completed — human-head", stdout)
                self.assertFalse(any(call[0][:3] == ("gh", "pr", "edit") for call in host.calls))

    def test_skill_branch_count_failures_are_failed_rows_without_mutation(self) -> None:
        bump = self._matt_bump()
        patched = apply_bump(FICTITIOUS_PROVENANCE_TEXT, bump)
        for label, count, failure in (
            ("command failure", 2, "fatal: bad revision"),
            ("non-integer", "not an integer", None),
        ):
            with self.subTest(result=label):
                host = PinWatchHost(
                    prs=[self._skill_pr("skills.matt-pocock", title_for(bump))],
                    tooling_text=FICTITIOUS_PROVENANCE_TEXT,
                    branch_exists=True,
                    branch_text=patched,
                    rev_list_count=count,
                )
                if failure is not None:
                    host.fail_once[("git", "rev-list", "--count")] = failure
                result, stdout, _stderr = _cli_output(
                    host,
                    _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
                    ["--only", "skills.matt-pocock"],
                )
                self.assertEqual(result, 1)
                self.assertIn("skills.matt-pocock: failed — git rev-list --count", stdout)
                self.assertFalse(any(call[0][:3] == ("git", "push", "--force") for call in host.calls))
                self.assertFalse(any(call[0][:3] == ("gh", "pr", "edit") for call in host.calls))

    def test_skill_pr_after_merge_or_close_follows_the_state_table(self) -> None:
        """A merge, or a close of an older proposal, opens fresh; a close of the
        same proposal is a human's no and reopens nothing (the declined rule is
        the same for a skill record as for every other pin)."""

        current = self._matt_bump()
        earlier = self._matt_bump("c" * 40, "2099-01-15")
        cases = (
            ("MERGED", title_for(current), "opened", True),
            ("CLOSED", title_for(earlier), "opened", True),
            ("CLOSED", title_for(current), "declined", False),
        )
        for state, title, row, creates in cases:
            with self.subTest(state=state, title=title):
                host = PinWatchHost(
                    prs=[self._skill_pr("skills.matt-pocock", title, state=state)],
                    tooling_text=FICTITIOUS_PROVENANCE_TEXT,
                )
                result, stdout, stderr = _cli_output(
                    host,
                    _fictitious_skill_fetcher(matt_sha="e" * 40, matt_date="2099-02-03"),
                    ["--only", "skills.matt-pocock"],
                )
                self.assertEqual(result, 0, stderr)
                self.assertIn(f"skills.matt-pocock: {row}", stdout)
                self.assertEqual(
                    any(call[0][:3] == ("gh", "pr", "create") for call in host.calls),
                    creates,
                )

    def test_bump_pushes_then_creates_with_stdin_body(self) -> None:
        digest = "sha256:" + "b" * 64
        host = PinWatchHost()
        result, stdout, stderr = _cli_output(
            host, self._caddy_fetcher("1.1", digest), ["--only", "images.caddy"]
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("images.caddy: opened", stdout)
        self.assertIn("→", stdout)
        commands = [call[0] for call in host.calls]
        self.assertEqual(
            commands[7:14],
            [
                ("git", "checkout", "-B", "pin-watch/images.caddy", "origin/main"),
                (sys.executable, "tests/regenerate_render_fixtures.py"),
                ("git", "add", "images.lock", "tests/fixtures/render"),
                (
                    "git",
                    "commit",
                    "-m",
                    "pin watch: images.caddy docker.io/library/example:1.0 → docker.io/library/example:1.1",
                ),
                ("git", "push", "--force", "origin", "pin-watch/images.caddy"),
                ("git", "checkout", "--detach", "origin/main"),
                (
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    "main",
                    "--head",
                    "pin-watch/images.caddy",
                    "--title",
                    "pin watch: images.caddy docker.io/library/example:1.0 → docker.io/library/example:1.1",
                    "--body-file",
                    "-",
                ),
            ],
        )
        body = host.calls[-1][1] or ""
        self.assertIn("### Research notes verified against this pin", body)
        self.assertIn("| `images.caddy.source` |", body)
        self.assertIn("tests/fixtures/render", body)
        self.assertEqual(host.files["/repo/images.lock"].count("example:1.1"), 1)

    def test_bump_body_includes_a_fake_bound_note(self) -> None:
        """The created pull request body carries a note bound to its pin."""

        host = self._notes_host(
            "---\n"
            "verified_against:\n"
            "  - pin: images.caddy\n"
            "    version: 9000.0.0\n"
            "---\n"
            "# Example\n"
        )
        digest = "sha256:" + "b" * 64
        result, _stdout, stderr = _cli_output(
            host, self._caddy_fetcher("1.1", digest), ["--only", "images.caddy"]
        )
        self.assertEqual(result, 0, stderr)
        body = host.calls[-1][1] or ""
        self.assertIn(
            "| `docs/research/example.md` | 9000.0.0 | 1.1 |", body
        )

    def test_malformed_or_unknown_note_refuses_before_any_row(self) -> None:
        """A bad note stops the CLI before rows, git pushes, or GitHub calls."""

        cases = (
            "# Example\n",
            (
                "---\n"
                "verified_against:\n"
                "  - pin: example.unknown\n"
                "    version: 9000.0.0\n"
                "---\n"
                "# Example\n"
            ),
        )
        for note_text in cases:
            with self.subTest(note=note_text):
                host = self._notes_host(note_text)
                result, stdout, stderr = _cli_output(
                    host,
                    self._caddy_fetcher("1.1", "sha256:" + "b" * 64),
                    ["--only", "images.caddy"],
                )
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(len(stderr.splitlines()), 1)
                self.assertTrue(stderr.startswith("pin watch:"))
                self.assertIn("Fix:", stderr)
                self.assertFalse(any(call[0][0] == "gh" for call in host.calls))
                self.assertFalse(any(call[0][:2] == ("git", "push") for call in host.calls))

    def test_notes_listing_shows_watched_and_recorded_ids_in_order(self) -> None:
        """The listing joins fake notes and labels dev records."""

        host = self._notes_host(
            "---\n"
            "verified_against:\n"
            "  - pin: images.caddy\n"
            "    version: 9000.0.0\n"
            "  - pin: models.generator\n"
            "    version: 9000.0.1\n"
            "  - pin: dev.playwright\n"
            "    version: 9000.0.2\n"
            "---\n"
            "# Example\n"
        )
        stdout, stderr = StringIO(), StringIO()
        with mock_patch("sys.stdout", stdout), mock_patch("sys.stderr", stderr):
            result = notes.main([], host=host, root=Path("/repo"))
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        lines = stdout.getvalue().splitlines()
        caddy = "images.caddy: 1 — docs/research/example.md (9000.0.0)"
        model = (
            "models.generator: 1 — "
            "docs/research/example.md (9000.0.1)"
        )
        dev = (
            "dev.playwright (recorded, not watched): 1 — "
            "docs/research/example.md (9000.0.2)"
        )
        self.assertIn(caddy, lines)
        self.assertIn("host.registry_image: none", lines)
        self.assertIn(model, lines)
        self.assertIn(dev, lines)
        self.assertLess(lines.index(caddy), lines.index(model))
        self.assertLess(lines.index(model), lines.index(dev))

    def test_notes_listing_refuses_a_malformed_note(self) -> None:
        """The listing returns one refusal and no rows for malformed notes."""

        host = self._notes_host("# Example\n")
        stdout, stderr = StringIO(), StringIO()
        with mock_patch("sys.stdout", stdout), mock_patch("sys.stderr", stderr):
            result = notes.main([], host=host, root=Path("/repo"))
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)
        self.assertTrue(stderr.getvalue().startswith("pin watch:"))
        self.assertIn("Fix:", stderr.getvalue())

    def test_completed_built_branch_is_left_alone_after_main_moves(self) -> None:
        image_result = load_image_lock_text(BUILT_IMAGE_LOCK_TEXT)
        assert image_result.lock is not None
        image = image_result.lock.images[0]
        assert isinstance(image, BuiltImagePin)
        pin = AptPackagePin(
            "images.postgres.build_args.PGBACKREST_VERSION",
            "images.lock",
            image,
            "PGBACKREST_VERSION",
        )
        fetcher = DictFetcher({APT_INDEX: response(APT_PACKAGES)})
        bump = pin.resolve(fetcher)
        assert bump is not None
        patched = apply_bump(BUILT_IMAGE_LOCK_TEXT, bump)
        completed = patched.replace("sha256:" + "c" * 64, "sha256:" + "d" * 64).replace(
            "sha256:" + "b" * 64, "sha256:" + "e" * 64
        )
        title = title_for(bump)
        host = PinWatchHost(
            prs=[
                {
                    "number": 18,
                    "url": "https://github.com/acme/repo/pull/18",
                    "title": title,
                    "headRefName": "pin-watch/images.postgres.build_args.PGBACKREST_VERSION",
                    "state": "OPEN",
                    "mergedAt": None,
                    "closedAt": None,
                }
            ],
            image_text=BUILT_IMAGE_LOCK_TEXT,
            branch_exists=True,
            branch_text=completed,
            branch_parent="old-main",
            main_commit="new-main",
            branch_head="completion-head",
        )
        result, stdout, stderr = _cli_output(host, DictFetcher({APT_INDEX: response(APT_PACKAGES)}), ["--only", pin.id])
        self.assertEqual(result, 0, stderr)
        self.assertIn("images.postgres.build_args.PGBACKREST_VERSION: completed — sha256:" + "d" * 64, stdout)
        self.assertEqual(
            set(host.files), {"/repo/models.lock", "/repo/requirements-dev.txt"}
        )
        self.assertFalse(any(call[0][0] in {"gh", "git"} and call[0][1] in {"push", "checkout"} for call in host.calls))

    def test_superseded_completion_is_updated_and_named(self) -> None:
        """A completion for an older bump is replaced, and the row names its head."""

        image_result = load_image_lock_text(BUILT_IMAGE_LOCK_TEXT)
        assert image_result.lock is not None
        image = image_result.lock.images[0]
        assert isinstance(image, BuiltImagePin)
        pin = AptPackagePin(
            "images.postgres.build_args.PGBACKREST_VERSION",
            "images.lock",
            image,
            "PGBACKREST_VERSION",
        )
        newer_index = APT_PACKAGES.replace("1000.0.1-1.example", "1000.0.2-1.example")
        bump = pin.resolve(DictFetcher({APT_INDEX: response(newer_index)}))
        assert bump is not None
        # The branch completed the *previous* proposal (1000.0.1) with a rebuild.
        previous = apply_bump(
            BUILT_IMAGE_LOCK_TEXT,
            pin.resolve(DictFetcher({APT_INDEX: response(APT_PACKAGES)})) or bump,
        )
        completed = previous.replace("sha256:" + "c" * 64, "sha256:" + "d" * 64).replace(
            "sha256:" + "b" * 64, "sha256:" + "e" * 64
        )
        host = PinWatchHost(
            prs=[
                {
                    "number": 19,
                    "url": "https://github.com/acme/repo/pull/19",
                    "title": "pin watch: images.postgres.build_args.PGBACKREST_VERSION 1000.0.0-1.example → 1000.0.1-1.example",
                    "headRefName": "pin-watch/images.postgres.build_args.PGBACKREST_VERSION",
                    "state": "OPEN",
                    "mergedAt": None,
                    "closedAt": None,
                }
            ],
            image_text=BUILT_IMAGE_LOCK_TEXT,
            branch_exists=True,
            branch_text=completed,
            branch_head="old-completion-head",
        )
        result, stdout, stderr = _cli_output(
            host, DictFetcher({APT_INDEX: response(newer_index)}), ["--only", pin.id]
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("updated", stdout)
        self.assertIn("superseded completion old-completion-head", stdout)
        self.assertTrue(any(call[0][:3] == ("git", "push", "--force") for call in host.calls))

    def test_open_pr_unchanged_and_updated_when_base_moved(self) -> None:
        digest = "sha256:" + "b" * 64
        title = "pin watch: images.caddy docker.io/library/example:1.0 → docker.io/library/example:1.1"
        pr = [
            {
                "number": 8,
                "url": "https://github.com/acme/repo/pull/8",
                "title": title,
                "headRefName": "pin-watch/images.caddy",
                "state": "OPEN",
                "mergedAt": None,
                "closedAt": None,
            }
        ]
        fetcher = self._caddy_fetcher("1.1", digest)
        host = PinWatchHost(prs=pr)
        lock = cast(ImageLock, load_image_lock_text(IMAGE_LOCK_TEXT).lock)
        image = lock.images[0]
        assert isinstance(image, MirroredImagePin)
        bump = WatchImagePin("images.caddy", "images.lock", image).resolve(
            fetcher
        )
        patched = apply_bump(host.image_text, cast(Bump, bump))
        host.branch_exists = True
        host.branch_text = patched
        result, stdout, stderr = _cli_output(
            host, self._caddy_fetcher("1.1", digest), ["--only", "images.caddy"]
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("unchanged", stdout)
        host.branch_parent = "old"
        host.main_commit = "new"
        result, stdout, stderr = _cli_output(
            host, self._caddy_fetcher("1.1", digest), ["--only", "images.caddy"]
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("updated", stdout)
        self.assertTrue(
            any(call[0][:3] == ("git", "push", "--force") for call in host.calls)
        )

    def test_current_open_is_withdrawn_and_dry_run_does_not_write(self) -> None:
        pr = [
            {
                "number": 8,
                "url": "https://github.com/acme/repo/pull/8",
                "title": "old",
                "headRefName": "pin-watch/images.caddy",
                "state": "OPEN",
                "mergedAt": None,
                "closedAt": None,
            }
        ]
        digest = (
            "sha256:"
            + "e" * 64
        )
        host = PinWatchHost(prs=pr)
        result, stdout, stderr = _cli_output(
            host,
            self._caddy_fetcher("1.0", digest),
            ["--only", "images.caddy", "--dry-run"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("would closed", stdout)
        self.assertEqual(
            set(host.files), {"/repo/models.lock", "/repo/requirements-dev.txt"}
        )
        self.assertFalse(
            any(
                call[0][0] == "gh" and call[0][2] in {"edit", "close"}
                for call in host.calls
            )
        )

    CADDY_TITLE = "pin watch: images.caddy docker.io/library/example:1.0 → docker.io/library/example:1.1"
    CURRENT_DIGEST = CADDY_DIGEST

    def _pr(self, title: str, state: str, number: int = 8) -> dict[str, object]:
        closed = None if state == "OPEN" else "2026-09-01T00:00:00Z"
        return {
            "number": number,
            "url": f"https://github.com/acme/repo/pull/{number}",
            "title": title,
            "headRefName": "pin-watch/images.caddy",
            "state": state,
            "mergedAt": closed if state == "MERGED" else None,
            "closedAt": closed,
        }

    def test_nothing_new_makes_only_the_read_calls(self) -> None:
        host = PinWatchHost()
        result, stdout, _ = _cli_output(
            host,
            self._caddy_fetcher("1.0", self.CURRENT_DIGEST),
            ["--only", "images.caddy"],
        )
        self.assertEqual(result, 0)
        self.assertIn("images.caddy: current — docker.io/library/example:1.0@", stdout)
        self.assertEqual(
            [call[0][:2] for call in host.calls],
            [
                ("git", "rev-parse"),
                ("git", "fetch"),
                ("git", "show"),
                ("git", "show"),
                ("git", "show"),
                ("git", "show"),
                ("gh", "pr"),
            ],
        )
        self.assertEqual(
            host.calls[0][0], ("git", "rev-parse", "--is-shallow-repository")
        )
        self.assertEqual(
            [
                call[0][2]
                for call in host.calls
                if call[0][:2] == ("git", "show")
            ],
            [
                "origin/main:images.lock",
                "origin/main:host.lock",
                "origin/main:models.lock",
                "origin/main:docs/agents/tooling.md",
            ],
        )
        self.assertEqual(
            host.calls[1][0],
            (
                "git",
                "fetch",
                "origin",
                "main",
                "+refs/heads/pin-watch/*:refs/remotes/origin/pin-watch/*",
            ),
        )

    def test_shallow_checkout_is_completed_by_the_one_fetch(self) -> None:
        # The hosted runner's depth-1 checkout: over a cut history
        # ``origin/main..origin/<branch>`` counts the branch's whole fetched
        # ancestry and read an untouched proposal as completed.
        host = PinWatchHost(shallow=True)
        result, _, _ = _cli_output(
            host,
            self._caddy_fetcher("1.0", self.CURRENT_DIGEST),
            ["--only", "images.caddy"],
        )
        self.assertEqual(result, 0)
        fetches = [call[0] for call in host.calls if call[0][:2] == ("git", "fetch")]
        self.assertEqual(
            fetches,
            [
                (
                    "git",
                    "fetch",
                    "--unshallow",
                    "origin",
                    "main",
                    "+refs/heads/pin-watch/*:refs/remotes/origin/pin-watch/*",
                )
            ],
        )

    def test_unreadable_shallow_state_refuses_before_the_fetch(self) -> None:
        host = PinWatchHost()
        host.fail_once[("git", "rev-parse", "--is-shallow-repository")] = "not a repo"
        result, _, stderr = _cli_output(
            host,
            self._caddy_fetcher("1.0", self.CURRENT_DIGEST),
            ["--only", "images.caddy"],
        )
        self.assertEqual(result, 1)
        self.assertIn("git rev-parse --is-shallow-repository", stderr)
        self.assertFalse(
            [call for call in host.calls if call[0][:2] == ("git", "fetch")]
        )

    def test_declined_versus_reopened(self) -> None:
        digest = "sha256:" + "b" * 64
        cases = (
            (self._pr(self.CADDY_TITLE, "CLOSED"), "declined", False),
            (self._pr(self.CADDY_TITLE, "MERGED"), "opened", True),
            (
                self._pr("pin watch: images.caddy something else", "CLOSED"),
                "opened",
                True,
            ),
            (self._pr("[withdrawn] " + self.CADDY_TITLE, "CLOSED"), "opened", True),
        )
        for pr, expected, created in cases:
            with self.subTest(state=pr["state"], title=pr["title"]):
                host = PinWatchHost(prs=[pr])
                result, stdout, stderr = _cli_output(
                    host,
                    self._caddy_fetcher("1.1", digest),
                    ["--only", "images.caddy"],
                )
                self.assertEqual(result, 0, stderr)
                self.assertIn(f"images.caddy: {expected}", stdout)
                self.assertEqual(
                    any(call[0][:3] == ("gh", "pr", "create") for call in host.calls),
                    created,
                )
        _, declined, _ = _cli_output(
            PinWatchHost(prs=[self._pr(self.CADDY_TITLE, "CLOSED")]),
            self._caddy_fetcher("1.1", digest),
            ["--only", "images.caddy"],
        )
        self.assertIn(
            "closed by a human in PR https://github.com/acme/repo/pull/8; nothing reopened",
            declined,
        )

    def test_edit_then_close_failure_recovers_without_a_double_prefix(self) -> None:
        host = PinWatchHost(prs=[self._pr("old", "OPEN")])
        host.fail_once[("gh", "pr", "close")] = "gh: HTTP 502"
        result, stdout, _ = _cli_output(
            host,
            self._caddy_fetcher("1.0", self.CURRENT_DIGEST),
            ["--only", "images.caddy"],
        )
        self.assertEqual(result, 1)
        self.assertIn(
            "images.caddy: failed — gh pr close exited 1: gh: HTTP 502. Fix: Check the App token",
            stdout,
        )
        self.assertIn(
            ("gh", "pr", "edit", "8", "--title", "[withdrawn] old"),
            [c[0] for c in host.calls],
        )
        again = PinWatchHost(prs=[self._pr("[withdrawn] old", "OPEN")])
        result, stdout, _ = _cli_output(
            again,
            self._caddy_fetcher("1.0", self.CURRENT_DIGEST),
            ["--only", "images.caddy"],
        )
        self.assertEqual(result, 0)
        self.assertIn("images.caddy: closed — main carries", stdout)
        edits = [c[0] for c in again.calls if c[0][:3] == ("gh", "pr", "edit")]
        self.assertEqual(
            edits, [("gh", "pr", "edit", "8", "--title", "[withdrawn] old")]
        )
        self.assertIn(
            ("gh", "pr", "close", "8", "--comment"), [c[0][:5] for c in again.calls]
        )

    def test_fixture_regeneration_failure_names_the_script(self) -> None:
        host = PinWatchHost()
        host.fail_once[(sys.executable,)] = (
            "Traceback (most recent call last):\n  …\nRuntimeError: fixture inputs could not be loaded"
        )
        result, stdout, _ = _cli_output(
            host,
            self._caddy_fetcher("1.1", "sha256:" + "f" * 64),
            ["--only", "images.caddy"],
        )
        self.assertEqual(result, 1)
        self.assertIn(
            "images.caddy: failed — regenerate_render_fixtures.py exited 1: RuntimeError: "
            "fixture inputs could not be loaded. Fix: Run python3 tests/regenerate_render_fixtures.py",
            stdout,
        )
        self.assertFalse(any(call[0][:2] == ("git", "push") for call in host.calls))

    def test_title_only_difference_updates(self) -> None:
        digest = "sha256:" + "b" * 64
        fetcher = self._caddy_fetcher("1.1", digest)
        host = PinWatchHost(prs=[self._pr("[withdrawn] " + self.CADDY_TITLE, "OPEN")])
        lock = cast(ImageLock, load_image_lock_text(IMAGE_LOCK_TEXT).lock)
        image = lock.images[0]
        assert isinstance(image, MirroredImagePin)
        bump = WatchImagePin("images.caddy", "images.lock", image).resolve(
            fetcher
        )
        host.branch_exists = True
        host.branch_text = apply_bump(host.image_text, cast(Bump, bump))
        result, stdout, stderr = _cli_output(
            host, self._caddy_fetcher("1.1", digest), ["--only", "images.caddy"]
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("images.caddy: updated", stdout)
        self.assertIn(
            ("gh", "pr", "edit", "8", "--title", self.CADDY_TITLE, "--body-file", "-"),
            [c[0] for c in host.calls],
        )

    def test_resolver_failure_is_a_row_and_the_run_continues(self) -> None:
        tags = "https://registry-1.docker.io/v2/library/example/tags/list?n=1000"
        index = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2604/x86_64/Packages"
        fetcher = DictFetcher(
            {
                tags: response(status=503),
                index: response(f"Package: nvidia-driver-pinning-{NEXT_BRANCH}\n"),
            }
        )
        host = PinWatchHost()
        result, stdout, _ = _cli_output(
            host, fetcher, ["--only", "images.caddy", "--only", "host.driver.branch"]
        )
        self.assertEqual(result, 1)
        self.assertIn(
            "images.caddy: failed — HTTP status 503. Fix: Check " + tags, stdout
        )
        self.assertIn(
            f"host.driver.branch: opened — {DRIVER_BRANCH} → {NEXT_BRANCH} (proposal) — PR",
            stdout,
        )
        body = next(c[1] for c in host.calls if c[0][:3] == ("gh", "pr", "create"))
        self.assertIn("This is a proposal", body or "")

    def test_unreadable_tag_row_names_the_tag_rule(self) -> None:
        image_text = IMAGE_LOCK_TEXT.replace(
            "docker.io/library/example:1.0",
            "docker.io/library/example:latest-fictitious",
        )
        tags = "https://registry-1.docker.io/v2/library/example/tags/list?n=1000"
        host = PinWatchHost(image_text=image_text)
        result, stdout, stderr = _cli_output(
            host,
            DictFetcher({tags: response({"tags": ["latest-fictitious"]})}),
            ["--only", "images.caddy"],
        )
        self.assertEqual(result, 1, stderr)
        self.assertEqual(stdout.count("images.caddy: failed —"), 1)
        self.assertIn("tag_shape", stdout)
        self.assertIn("tools/pinwatch/oci.py", stdout)
        self.assertNotIn("images.lock", stdout)

    def test_unstable_pypi_version_keeps_the_lock_fix(self) -> None:
        image_text = BUILT_TWO_KIND_IMAGE_LOCK_TEXT.replace(
            "      PYPI_VERSION: 1.2.3\n",
            "      PYPI_VERSION: 9000.0.0rc1\n",
        )
        pin_id = "images.postgres.build_args.PYPI_VERSION"
        host = PinWatchHost(image_text=image_text)
        result, stdout, stderr = _cli_output(
            host,
            DictFetcher({}),
            ["--only", pin_id],
        )
        self.assertEqual(result, 1, stderr)
        self.assertIn(
            f"{pin_id}: failed — current PyPI version '9000.0.0rc1' is not stable. "
            f"Fix: Correct {pin_id} in images.lock, then re-run the pin watch.",
            stdout,
        )

    def test_pypi_failure_is_a_row_and_the_run_continues(self) -> None:
        image_result = load_image_lock_text(BUILT_TWO_KIND_IMAGE_LOCK_TEXT)
        assert image_result.lock is not None
        image = image_result.lock.images[0]
        assert isinstance(image, BuiltImagePin)
        watch = image.watch["PYPI_VERSION"]
        assert isinstance(watch, PypiWatch)
        pypi_url = f"{pypi.PYPI_INDEX_ROOT}/{watch.project}/"
        driver_url = (
            "https://developer.download.nvidia.com/compute/cuda/repos/"
            f"{cast(HostLock, _COMMITTED_HOST_LOCK).driver.repo}/Packages"
        )
        driver_reply = response(f"Package: nvidia-driver-pinning-{NEXT_BRANCH}\n")
        for label, fetcher in (
            (
                "unreachable",
                UnreachableFetcher({driver_url: driver_reply}, frozenset({pypi_url})),
            ),
            (
                "malformed",
                DictFetcher(
                    {pypi_url: response(b"{not JSON"), driver_url: driver_reply}
                ),
            ),
        ):
            with self.subTest(reply=label):
                host = PinWatchHost(image_text=BUILT_TWO_KIND_IMAGE_LOCK_TEXT)
                result, stdout, stderr = _cli_output(
                    host,
                    fetcher,
                    [
                        "--only",
                        "images.postgres.build_args.PYPI_VERSION",
                        "--only",
                        "host.driver.branch",
                        "--dry-run",
                    ],
                )
                self.assertEqual(result, 1, stderr)
                self.assertIn(
                    "images.postgres.build_args.PYPI_VERSION: failed —", stdout
                )
                self.assertIn(
                    f"host.driver.branch: would opened — {DRIVER_BRANCH} → "
                    f"{NEXT_BRANCH} (proposal)",
                    stdout,
                )

    def test_loader_refusal_and_unknown_only(self) -> None:
        bad = PinWatchHost(
            image_text="version: 1\nimages:\n  caddy:\n    source: docker.io/library/example:1.0\n    digest: sha256:bad\n"
        )
        fetcher = DictFetcher({})
        result, _stdout, stderr = _cli_output(bad, fetcher, [])
        self.assertEqual(result, 1)
        self.assertIn("sha256:<64", stderr)
        self.assertEqual(fetcher.calls, [])
        good = PinWatchHost()
        result, _stdout, stderr = _cli_output(
            good, DictFetcher({}), ["--only", "pin.nope"]
        )
        self.assertEqual(result, 1)
        self.assertIn("Known ids:", stderr)

    def test_model_rows_cover_current_dry_run_and_fixture_backed_open(self) -> None:
        pin = _fictitious_model_pin()
        current_fetcher = _fictitious_model_fetcher(head=pin.model.revision)
        current_host = PinWatchHost(models_text=FICTITIOUS_MODELS_LOCK_TEXT)
        result, stdout, stderr = _cli_output(
            current_host,
            current_fetcher,
            ["--only", "models.generator", "--dry-run"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("models.generator: current —", stdout)
        self.assertEqual(len(current_fetcher.calls), 1)

        dry_host = PinWatchHost(models_text=FICTITIOUS_MODELS_LOCK_TEXT)
        result, stdout, stderr = _cli_output(
            dry_host,
            _fictitious_model_fetcher(),
            ["--only", "models.generator", "--dry-run"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("models.generator: would opened", stdout)
        self.assertFalse(any(call[0][:2] == ("git", "push") for call in dry_host.calls))

        open_host = PinWatchHost(models_text=FICTITIOUS_MODELS_LOCK_TEXT)
        result, stdout, stderr = _cli_output(
            open_host,
            _fictitious_model_fetcher(),
            ["--only", "models.generator"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("models.generator: opened", stdout)
        self.assertIn("tests/regenerate_render_fixtures.py", [
            word for call in open_host.calls for word in call[0]
        ])
        self.assertIn(FICTITIOUS_MODEL_HEAD, open_host.files["/repo/models.lock"])
        self.assertNotIn(FICTITIOUS_MODEL_REVISION, open_host.files["/repo/models.lock"])
        self.assertTrue(any(call[0] == ("git", "add", "models.lock", "tests/fixtures/render") for call in open_host.calls))

    def test_model_completion_is_left_alone_and_reports_stale_upstream(self) -> None:
        pin = _fictitious_model_pin()
        bump = pin.resolve(_fictitious_model_fetcher())
        assert bump is not None
        patched = apply_bump(FICTITIOUS_MODELS_LOCK_TEXT, bump)
        title = title_for(bump)
        host = PinWatchHost(
            models_text=FICTITIOUS_MODELS_LOCK_TEXT,
            prs=[self._skill_pr("models.generator", title)],
            branch_exists=True,
            branch_text=patched,
            branch_head="human-model-head",
            rev_list_count=2,
        )
        result, stdout, stderr = _cli_output(
            host,
            _fictitious_model_fetcher(),
            ["--only", "models.generator"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("models.generator: completed — human-model-head", stdout)
        self.assertFalse(any(call[0][:2] == ("git", "push") for call in host.calls))
        self.assertFalse(any(call[0][:3] == ("gh", "pr", "edit") for call in host.calls))

        older_bump = pin.resolve(
            _fictitious_model_fetcher(head=FICTITIOUS_SECOND_MODEL_REVISION)
        )
        assert older_bump is not None
        older_branch = apply_bump(FICTITIOUS_MODELS_LOCK_TEXT, older_bump)
        older_host = PinWatchHost(
            models_text=FICTITIOUS_MODELS_LOCK_TEXT,
            prs=[self._skill_pr("models.generator", title)],
            branch_exists=True,
            branch_text=older_branch,
            branch_head="older-model-head",
            rev_list_count=2,
        )
        result, stdout, stderr = _cli_output(
            older_host,
            _fictitious_model_fetcher(),
            ["--only", "models.generator"],
        )
        self.assertEqual(result, 0, stderr)
        self.assertIn("models.generator: completed — older-model-head; upstream now", stdout)

        stale_files = replace_scalar(
            FICTITIOUS_MODELS_LOCK_TEXT,
            "profiles.1x1v-1d.models.generator.revision",
            FICTITIOUS_MODEL_HEAD,
        )
        state = branch_state(
            PinWatchHost(
                branch_exists=True,
                branch_text=stale_files,
                rev_list_count=2,
            ),
            Path("/repo"),
            "pin-watch/models.generator",
            "models.lock",
            patched,
            bump=bump,
        )
        self.assertEqual(state.state, "completed")
        self.assertFalse(state.carries)


class DatedCommitTags(unittest.TestCase):
    """SearXNG's tag shape: a date and a commit hash.

    The hash is never part of the shape, a date rollover is never a major
    bump, and a same-date tie is broken by the registry's own creation time
    or not at all.  Every tag here is fictitious (the lock-value rule).
    """

    SOURCE = "docker.io/example-org/example:2001.2.3-0000aaa"

    def test_shape_kind_components_and_comparisons(self) -> None:
        shape = tag_shape("2001.2.3-0000aaa")
        self.assertIsNotNone(shape)
        assert shape is not None
        self.assertEqual(shape.kind, "dated-commit")
        self.assertEqual(shape.components, (2001, 2, 3))
        self.assertEqual((shape.prefix, shape.suffix, shape.separators), ("", "", (".", ".")))
        release = tag_shape("2.11")
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.kind, "release")
        self.assertIsNone(tag_shape("latest"))
        self.assertTrue(same_shape("2001.2.3-0000aaa", "2002.1.1-ffffff0"))
        self.assertFalse(same_shape("2001.2.3-0000aaa", "2001.2.3"))
        self.assertFalse(same_shape("2001.2.3", "2001.2.3-0000aaa"))
        tags = ("latest", "2001.2.4-0000bbb", "2001.2.3-0000aaa", "2001.2.4-0000ccc", "2001.1.9-0000ddd")
        self.assertEqual(
            newest_same_shape_candidates(tags, "2001.2.3-0000aaa"),
            ("2001.2.4-0000bbb", "2001.2.4-0000ccc"),
        )
        self.assertEqual(newest_same_shape(tags, "2001.2.3-0000aaa"), "2001.2.4-0000bbb")
        self.assertEqual(newest_same_shape_candidates(("2001.2.3-0000aaa",), "2001.2.3-0000aaa"), ())
        self.assertEqual(newest_same_shape_candidates(("2.12", "2.11"), "2.11"), ("2.12",))
        self.assertFalse(leading_component_changed("2001.2.3-0000aaa", "2002.1.1-ffffff0"))
        self.assertEqual(
            created_time("2001-02-03T04:05:06.123456789Z"),
            created_time("2001-02-03T04:05:06.123456+00:00"),
        )
        with self.assertRaises(FetchError):
            created_time("2001-02-03T04:05:06")
        with self.assertRaises(FetchError):
            created_time("yesterday")

    def _chain(self, tag: str, created: object, *, amd64: bool = True, index_digest: str | None = None) -> dict[str, Response]:
        """One tag's index, its amd64 manifest, and its configuration blob."""

        base = "https://registry-1.docker.io/v2/example-org/example"
        digest = "sha256:" + tag[-7:].rjust(64, "1")
        config = "sha256:" + tag[-7:].rjust(64, "2")
        arm = {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "arm", "variant": "v7"}}
        entries = [arm] + ([{"digest": digest, "platform": {"os": "linux", "architecture": "amd64"}}] if amd64 else [])
        return {
            f"{base}/manifests/{tag}": response(
                {"manifests": entries}, headers={"Docker-Content-Digest": index_digest or "sha256:" + tag[-7:].rjust(64, "3")}
            ),
            f"{base}/manifests/{digest}": response({"config": {"digest": config}}),
            f"{base}/blobs/{config}": response({"created": created} if created is not None else {}),
        }

    def test_image_created_walks_index_manifest_and_configuration(self) -> None:
        reference = parse_reference(self.SOURCE)
        fetcher = DictFetcher(self._chain("2001.2.3-0000aaa", "2001-02-03T04:05:06Z"))
        self.assertEqual(image_created(fetcher, reference, "2001.2.3-0000aaa"), "2001-02-03T04:05:06Z")
        self.assertEqual([method for _, _, method in fetcher.calls], ["GET", "GET", "GET"])
        for chain, detail in (
            (self._chain("2001.2.3-0000aaa", "2001-02-03T04:05:06Z", amd64=False), "linux/amd64"),
            (self._chain("2001.2.3-0000aaa", None), "created"),
        ):
            with self.subTest(detail=detail), self.assertRaises(FetchError) as caught:
                image_created(DictFetcher(chain), reference, "2001.2.3-0000aaa")
            self.assertIn(detail, str(caught.exception))

    def _tie_pin(self) -> WatchImagePin:
        image = MirroredImagePin("searxng", CADDY_DIGEST, self.SOURCE)
        return WatchImagePin("images.searxng", "images.lock", image)

    def _tie_fetcher(self, created: Mapping[str, object]) -> DictFetcher:
        tags_url = "https://registry-1.docker.io/v2/example-org/example/tags/list?n=1000"
        responses: dict[str, Response | list[Response]] = {
            tags_url: response({"tags": ["2001.2.3-0000aaa", *created]})
        }
        for tag, when in created.items():
            responses.update(self._chain(tag, when))
        return DictFetcher(responses)

    def test_same_date_tie_is_broken_by_the_configuration_time(self) -> None:
        pin = self._tie_pin()
        fetcher = self._tie_fetcher(
            {
                "2001.2.4-0000bbb": "2001-02-04T10:00:00Z",
                "2001.2.4-0000ccc": "2001-02-04T16:30:00Z",
                "2001.2.4-0000ddd": "2001-02-04T12:00:00Z",
            }
        )
        bump = cast(Bump, pin.resolve(fetcher))
        self.assertEqual(bump.changes[0].new, "docker.io/example-org/example:2001.2.4-0000ccc")
        self.assertFalse(bump.proposal)
        single = self._tie_fetcher({"2001.2.5-0000eee": "2001-02-05T00:00:00Z"})
        bump = cast(Bump, pin.resolve(single))
        self.assertEqual(bump.changes[0].new, "docker.io/example-org/example:2001.2.5-0000eee")
        # A single candidate never reads a configuration: the tag and its digest alone.
        self.assertFalse(any("/blobs/" in url for url, _, _ in single.calls))

    def test_a_build_on_the_pin_s_own_date_is_weighed_against_the_pin(self) -> None:
        pin = self._tie_pin()
        pinned = "2001.2.3-0000aaa"
        later = self._tie_fetcher({"2001.2.3-0000fff": "2001-02-03T18:00:00Z"})
        later.responses.update(self._chain(pinned, "2001-02-03T09:00:00Z", index_digest=CADDY_DIGEST))
        bump = cast(Bump, pin.resolve(later))
        self.assertEqual(bump.changes[0].new, "docker.io/example-org/example:2001.2.3-0000fff")
        earlier = self._tie_fetcher({"2001.2.3-0000eee": "2001-02-03T03:00:00Z"})
        earlier.responses.update(self._chain(pinned, "2001-02-03T09:00:00Z", index_digest=CADDY_DIGEST))
        self.assertIsNone(pin.resolve(earlier))
        self.assertEqual(
            newest_same_shape_candidates(("2001.2.3-0000aaa", "2001.2.3-0000eee"), pinned),
            ("2001.2.3-0000eee",),
        )
        self.assertEqual(newest_same_shape_candidates(("2.11",), "2.11"), ())

    def test_an_unbroken_tie_fails_closed_naming_the_tags(self) -> None:
        pin = self._tie_pin()
        for created in (
            {"2001.2.4-0000bbb": "2001-02-04T10:00:00Z", "2001.2.4-0000ccc": "2001-02-04T10:00:00Z"},
            {"2001.2.4-0000bbb": "2001-02-04T10:00:00Z", "2001.2.4-0000ccc": None},
        ):
            with self.subTest(created=created), self.assertRaises(FetchError) as caught:
                pin.resolve(self._tie_fetcher(created))
            message = str(caught.exception)
            self.assertIn("2001.2.4-0000bbb", message)
            self.assertIn("2001.2.4-0000ccc", message)
            self.assertIn("tie", message)


if __name__ == "__main__":
    unittest.main()
