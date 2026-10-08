"""Contracts for the committed models lock and its refusal shapes."""

import json
import os
import re
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, is_dataclass
from fractions import Fraction
from pathlib import Path

from gideon.host.images import DIGEST
from gideon.host.models import (
    EMBEDDING_SPACE_ID,
    ENV_NAME,
    FILE_PATH,
    FLAG_NAME,
    HF_REPO,
    PROFILE_NAME,
    REVISION,
    ROLE_NAME,
    SERVICE_NAME,
    CandidatePin,
    CotenantReserve,
    EmbeddingSpace,
    GpuRequirements,
    HardwareProfile,
    MemoryRow,
    ModelPin,
    ModelsLock,
    ModelsLockLoadResult,
    PinnedFile,
    ProfileRequirements,
    ServeBaseline,
    load_models_lock,
    load_models_lock_text,
    parse_models_lock,
    render_errors,
    select_profile,
)
from gideon.host.report import Problem
from gideon.host.sysio import Command, PathLike
from tools.exportboundary import absent_from_export

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "models.lock"
FAKE_REVISION = "a" * 40
FAKE_DIGEST = "sha256:" + "b" * 64

VALID = f"""version: 1
reference: 2x96v-256d
profiles:
  2x96v-256d:
    requires:
      platform: x86_64
      gpu:
        architecture: Blackwell
        compute_capability: "12.0"
        model: NVIDIA RTX PRO 6000 Blackwell Server Edition
        count: 2
        vram_gb: 96
      dram_gb: 256
      data_volume_gb: 4000
    memory:
      caddy:
        gb: 1
      prometheus:
        gb: 2
      node-exporter:
        gb: 1
      grafana:
        gb: 3
      postgres:
        gb: 20
      open-webui:
        gb: 4
      gideon-generator:
        gb: 32
        role: generator
      searxng:
        gb: 1
      dcgm-exporter:
        gb: 2
      postgres-exporter:
        gb: 1
      cadvisor:
        gb: 5
      blackbox-exporter:
        gb: 1
      gideon-api:
        gb: 1
      gideon-worker:
        gb: 12
    models:
      generator:
        repo: example/fixture
        revision: "{FAKE_REVISION}"
        gpu: 0
        serve:
          served_name: gideon-generator
          env:
            VLLM_USE_DEEP_GEMM: "0"
            HF_HUB_OFFLINE: "1"
            HF_HOME: /data/models
          flags:
            max-model-len: 262144
            max-num-seqs: 96
            max-num-batched-tokens: 8192
            enable-chunked-prefill: true
            long-prefill-token-threshold: 4096
            scheduling-policy: priority
            kv-cache-dtype: fp8
            gpu-memory-utilization: 0.92
            reasoning-parser: qwen3
        files:
          config.json:
            sha256: {FAKE_DIGEST}
            size: 1
          nested/tokenizer.json:
            sha256: {FAKE_DIGEST}
            size: 0
"""

SPACE = """    embedding_space:
      id: example-space-17
      role: generator
      dimensions: 17
"""
VALID_WITH_SPACE = VALID.replace("    models:\n", SPACE + "    models:\n")
RESERVE = """    cotenant_reserve_gb:
      0: 3
      1: 5
"""
VALID_WITH_RESERVE = VALID.replace("    memory:\n", RESERVE + "    memory:\n", 1)
CANDIDATE = f"""candidates:
  alternate-embed:
    role: embed
    repo: example/candidate
    revision: "{FAKE_REVISION}"
    serve:
      served_name: alternate-embed
      env:
        HF_HOME: /data/models
      flags:
        max-model-len: 1024
    files:
      config.json:
        sha256: {FAKE_DIGEST}
        size: 1
      nested/tokenizer.json:
        sha256: {FAKE_DIGEST}
        size: 0
"""


class FakeHost:
    """Minimal file-backed Host for read-path refusal tests."""

    def __init__(self, *, files: Mapping[str, str] | None = None, error: BaseException | None = None) -> None:
        self.files = dict(files or {})
        self.error = error

    def run(
        self,
        argv: Command,
        *,
        check: bool = False,
        input: str | None = None,
        cwd: PathLike | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        passthrough: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, input, cwd, env, timeout, passthrough
        return subprocess.CompletedProcess(list(argv), 127, "", "")

    def read_text(self, path: PathLike, *, encoding: str = "utf-8") -> str:
        del encoding
        if self.error is not None:
            raise self.error
        key = os.fspath(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    def write_text(self, path: PathLike, text: str, *, encoding: str = "utf-8", mode: int = 0o644) -> None:
        del encoding, mode
        self.files[os.fspath(path)] = text

    def exists(self, path: PathLike) -> bool:
        return os.fspath(path) in self.files

    def listdir(self, path: PathLike) -> list[str]:
        raise NotImplementedError

    def unlink(self, path: PathLike, *, missing_ok: bool = False) -> None:
        raise NotImplementedError

    def stat(self, path: PathLike) -> os.stat_result:
        raise NotImplementedError

    def chmod(self, path: PathLike, mode: int) -> None:
        raise NotImplementedError

    def chown(self, path: PathLike, uid: int, gid: int) -> None:
        raise NotImplementedError

    def mkdir(self, path: PathLike, *, mode: int = 0o755, parents: bool = False, exist_ok: bool = False) -> None:
        raise NotImplementedError

    def geteuid(self) -> int:
        return 0


def load_text(text: str) -> ModelsLockLoadResult:
    return load_models_lock_text(text)


class CommittedLock(unittest.TestCase):
    def test_committed_reserves_and_model_budgets_fit_each_card(self) -> None:
        result = load_models_lock(LOCK)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lock is not None and result.document is not None
        profiles_document = result.document["profiles"]
        assert isinstance(profiles_document, Mapping)
        for profile in result.lock.profiles:
            with self.subTest(profile=profile.name):
                profile_document = profiles_document[profile.name]
                assert isinstance(profile_document, Mapping)
                reserve_document = profile_document.get("cotenant_reserve_gb")
                self.assertIsInstance(reserve_document, Mapping)
                assert isinstance(reserve_document, Mapping)
                positions = set(range(profile.requires.gpu.count))
                self.assertEqual(set(reserve_document), positions)
                self.assertEqual({row.gpu for row in profile.cotenant_reserve}, positions)
                fractions: dict[int, list[Fraction]] = {position: [] for position in positions}
                for model in profile.models:
                    with self.subTest(role=model.role):
                        self.assertIn("gpu-memory-utilization", model.serve.flags)
                        value = model.serve.flags["gpu-memory-utilization"]
                        self.assertNotIsInstance(value, bool)
                        fraction = Fraction(str(value))
                        self.assertGreater(fraction, 0)
                        self.assertLessEqual(fraction, 1)
                        fractions[model.gpu].append(fraction)
                for position in positions:
                    reserve = profile.cotenant_reserve_gb(position)
                    self.assertEqual(reserve, reserve_document[position])
                    self.assertLessEqual(
                        sum(fractions[position], Fraction()) * profile.requires.gpu.vram_gb
                        + reserve,
                        profile.requires.gpu.vram_gb,
                    )

    def test_gpu_one_reserve_matches_shared_ledger(self) -> None:
        if absent_from_export("docs/box-ledger.md", ROOT):
            self.skipTest("shared GPU memory record is absent from the exported tree")
        text = (ROOT / "docs/box-ledger.md").read_text(encoding="utf-8")
        row = text.split("| GIDEON's plan", 1)[1].split("\n", 1)[0]
        match = re.search(r"([0-9]+) GB co-tenant reserve on GPU ([0-9]+)", row)
        self.assertIsNotNone(match)
        assert match is not None
        result = load_models_lock(LOCK)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lock is not None
        profile = result.lock.profile(result.lock.reference)
        assert profile is not None
        self.assertEqual(profile.cotenant_reserve_gb(int(match.group(2))), int(match.group(1)))

    def test_committed_lock_has_exact_release_facts_and_grammar_pins(self) -> None:
        result = load_models_lock(LOCK)
        self.assertTrue(result.ok, render_errors(result.errors))
        self.assertIsInstance(result.lock, ModelsLock)
        assert result.lock is not None
        self.assertEqual(result.lock.version, 1)
        self.assertEqual(result.lock.reference, "2x96v-256d")
        self.assertEqual(len(result.lock.profiles), 1)
        self.assertEqual(result.lock.candidates, ())
        self.assertIsNone(result.lock.candidate("alternate-embed"))
        assert result.document is not None
        self.assertNotIn("candidates", result.document)

        profile = result.lock.profiles[0]
        self.assertEqual(profile.name, result.lock.reference)
        self.assertEqual(
            profile.requires,
            ProfileRequirements(
                platform="x86_64",
                gpu=GpuRequirements(
                    architecture="Blackwell",
                    compute_capability="12.0",
                    model="NVIDIA RTX PRO 6000 Blackwell Server Edition",
                    count=2,
                    vram_gb=96,
                ),
                dram_gb=256,
                data_volume_gb=4000,
            ),
        )
        self.assertTrue(profile.memory)
        names = [row.service for row in profile.memory]
        self.assertEqual(len(names), len(set(names)))
        for row in profile.memory:
            with self.subTest(service=row.service):
                self.assertIs(type(row.gb), int)
                self.assertGreater(row.gb, 0)
                if row.role is not None:
                    self.assertIsNotNone(profile.model(row.role))
        self.assertEqual(profile.memory_row("gideon-generator"), MemoryRow("gideon-generator", 32, "generator"))
        self.assertIsNone(profile.memory_row("missing-service"))
        self.assertEqual({pin.role for pin in profile.models}, {"generator", "embed"})
        model = profile.model("generator")
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.role, "generator")
        self.assertEqual(model.gpu, 0)
        self.assertEqual(model.serve.served_name, "gideon-generator")
        self.assertEqual(
            dict(model.serve.env),
            {"VLLM_USE_DEEP_GEMM": "0", "HF_HUB_OFFLINE": "1", "HF_HOME": "/data/models"},
        )
        self.assertEqual(
            dict(model.serve.flags),
            {
                "max-model-len": 262144,
                "max-num-seqs": 96,
                "max-num-batched-tokens": 8192,
                "enable-chunked-prefill": True,
                "long-prefill-token-threshold": 4096,
                "scheduling-policy": "priority",
                "kv-cache-dtype": "fp8",
                "gpu-memory-utilization": 0.92,
                "reasoning-parser": "qwen3",
            },
        )

        self.assertIsNotNone(HF_REPO.fullmatch(model.repo))
        self.assertIsNotNone(REVISION.fullmatch(model.revision))
        paths = [file.path for file in model.files]
        self.assertEqual(paths, sorted(paths))
        for file in model.files:
            with self.subTest(path=file.path):
                self.assertIsNotNone(FILE_PATH.fullmatch(file.path))
                self.assertIsNotNone(DIGEST.fullmatch(file.sha256))
                self.assertGreaterEqual(file.size, 0)

    def test_committed_embedding_space_matches_its_model_flags(self) -> None:
        result = load_models_lock(LOCK)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lock is not None
        profile = result.lock.profile(result.lock.reference)
        assert profile is not None
        space = profile.embedding_space
        self.assertIsNotNone(space)
        assert space is not None
        self.assertIsNotNone(EMBEDDING_SPACE_ID.fullmatch(space.id))
        pin = profile.model(space.role)
        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertEqual(space.role, "embed")
        self.assertIs(type(space.dimensions), int)
        self.assertGreater(space.dimensions, 0)
        self.assertTrue(pin.serve.served_name)
        self.assertIn("HF_HOME", pin.serve.env)
        self.assertTrue(pin.files)
        hf_overrides = pin.serve.flags["hf-overrides"]
        pooler_config = pin.serve.flags["pooler-config"]
        self.assertIsInstance(hf_overrides, str)
        self.assertIsInstance(pooler_config, str)
        assert isinstance(hf_overrides, str) and isinstance(pooler_config, str)
        self.assertEqual(json.loads(hf_overrides)["matryoshka_dimensions"], [space.dimensions])
        self.assertEqual(json.loads(pooler_config)["dimensions"], space.dimensions)

    def test_models_dataclasses_are_frozen_and_slotted(self) -> None:
        for value in (
            PinnedFile("config.json", FAKE_DIGEST, 1),
            ServeBaseline("example", {}, {}),
            ModelPin("generator", "example/fixture", FAKE_REVISION, 0, ServeBaseline("example", {}, {}), ()),
            CandidatePin("alternate-embed", "embed", "example/candidate", FAKE_REVISION, ServeBaseline("example", {}, {}), ()),
            GpuRequirements("Example", "1.0", "Example GPU", 1, 1),
            ProfileRequirements("x86_64", GpuRequirements("Example", "1.0", "Example GPU", 1, 1), 1, 1),
            MemoryRow("example-service", 1, None),
            CotenantReserve(0, 3),
            EmbeddingSpace("example-space", "generator", 17),
            HardwareProfile("2x1v-1d", ProfileRequirements("x86_64", GpuRequirements("Example", "1.0", "Example GPU", 1, 1), 1, 1), (MemoryRow("example-service", 1, None),), ()),
            ModelsLock(1, "2x1v-1d", ()),
        ):
            self.assertTrue(is_dataclass(value))
            self.assertTrue(hasattr(value, "__slots__"))
            field = fields(value)[0]
            with self.assertRaises(FrozenInstanceError):
                setattr(value, field.name, getattr(value, field.name))

    def test_parse_then_load_split(self) -> None:
        parsed = parse_models_lock(VALID)
        self.assertFalse(parsed.ok)
        self.assertIsNotNone(parsed.document)
        self.assertIsNone(parsed.lock)
        loaded = load_models_lock_text(VALID)
        self.assertTrue(loaded.ok)
        self.assertIsNotNone(loaded.lock)

    def test_select_profile_returns_problem_with_available_profiles(self) -> None:
        result = load_models_lock_text(VALID)
        assert result.lock is not None
        selected = select_profile(result.lock, "9x9v-9d")
        self.assertIsInstance(selected, Problem)
        assert isinstance(selected, Problem)
        self.assertIn("9x9v-9d", selected.problem)
        self.assertIn("2x96v-256d", selected.problem)
        self.assertIn("hardware_profile", selected.fix)
        self.assertIn("2x96v-256d", selected.fix)


class Refusals(unittest.TestCase):
    def assert_refused(self, text: str, fragment: str, *, key_path: str | None = None) -> None:
        result = load_text(text)
        self.assertIsNone(result.lock)
        self.assertTrue(result.errors)
        self.assertTrue(any(fragment in error.problem for error in result.errors), result.errors)
        if key_path is not None:
            self.assertIn(key_path, [error.key_path for error in result.errors])
        for error in result.errors:
            self.assertTrue(error.fix)
        self.assertEqual(render_errors(result.errors).count("Fix:"), len(result.errors))

    def assert_space_refused(self, text: str, fragment: str, key_path: str) -> None:
        self.assert_refused(text, fragment, key_path=key_path)
        result = load_text(text)
        self.assertTrue(all(error.fix == "Edit models.lock; consult docs/runbooks/release-files.md §4." for error in result.errors))

    def test_candidate_loads_with_nested_file(self) -> None:
        result = load_text(VALID + CANDIDATE)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lock is not None
        self.assertEqual(len(result.lock.candidates), 1)
        candidate = result.lock.candidate("alternate-embed")
        self.assertIsInstance(candidate, CandidatePin)
        assert candidate is not None
        self.assertEqual(candidate.name, "alternate-embed")
        self.assertEqual(candidate.role, "embed")
        self.assertEqual(candidate.repo, "example/candidate")
        self.assertEqual(candidate.revision, FAKE_REVISION)
        self.assertEqual(candidate.serve.served_name, "alternate-embed")
        self.assertEqual(candidate.serve.env, {"HF_HOME": "/data/models"})
        self.assertEqual(candidate.serve.flags, {"max-model-len": 1024})
        self.assertEqual(candidate.files, (
            PinnedFile("config.json", FAKE_DIGEST, 1),
            PinnedFile("nested/tokenizer.json", FAKE_DIGEST, 0),
        ))
        self.assertIsNone(result.lock.candidate("missing"))
        empty = load_text(VALID + "candidates: {}\n")
        self.assertTrue(empty.ok, render_errors(empty.errors))
        assert empty.lock is not None
        self.assertEqual(empty.lock.candidates, ())

    def test_candidate_shape_refusals(self) -> None:
        path = "candidates.alternate-embed"
        no_serve = CANDIDATE[:CANDIDATE.index("    serve:\n")] + CANDIDATE[CANDIDATE.index("    files:\n"):]
        no_files = CANDIDATE[:CANDIDATE.index("    files:\n")]
        cases = (
            (VALID + "candidates: []\n", "expected a mapping", "candidates"),
            (VALID + CANDIDATE.replace("alternate-embed:\n", "bad.name:\n", 1), "candidate name", "candidates.bad.name"),
            (VALID + CANDIDATE.replace("alternate-embed:\n", "generator:\n", 1), "is a model role of profile", "candidates.generator"),
            (VALID + "candidates:\n  alternate-embed: []\n", "expected a mapping", path),
            (VALID + CANDIDATE.replace("    role: embed\n", ""), "missing required key", f"{path}.role"),
            (VALID + CANDIDATE.replace("    role: embed", "    role: bad.name"), "role name", f"{path}.role"),
            (VALID + CANDIDATE.replace("repo: example/candidate", "repo: invalid"), "owner/name repository", f"{path}.repo"),
            (VALID + CANDIDATE.replace(FAKE_REVISION, "invalid"), "40-character lowercase", f"{path}.revision"),
            (VALID + no_serve, "missing required key", f"{path}.serve"),
            (VALID + no_files, "missing required key", f"{path}.files"),
            (VALID + no_files + "    files: {}\n", "non-empty mapping", f"{path}.files"),
            (VALID + CANDIDATE.replace("      config.json:", "      z.json:", 1), "sorted path order", f"{path}.files"),
        )
        for text, fragment, key_path in cases:
            with self.subTest(fragment=fragment, key_path=key_path):
                self.assert_refused(text, fragment, key_path=key_path)
                result = load_text(text)
                self.assertTrue(all("docs/runbooks/release-files.md §4" in error.fix for error in result.errors))

        clash = load_text(VALID + CANDIDATE.replace("alternate-embed:\n", "generator:\n", 1))
        self.assertTrue(any("2x96v-256d" in error.problem and "Rename the candidate" in error.fix for error in clash.errors))

        gpu = load_text(VALID + CANDIDATE.replace("    role: embed\n", "    role: embed\n    gpu: 0\n"))
        self.assertFalse(gpu.ok)
        unknown = next(error for error in gpu.errors if error.key_path == f"{path}.gpu")
        self.assertIn("Unknown key", unknown.problem)
        self.assertIn("nearest valid key is", unknown.problem)
        self.assertNotIn("nearest valid key is 'gpu'", unknown.problem)
        self.assertIn("docs/runbooks/release-files.md §4", unknown.fix)

    def test_embedding_space_is_optional(self) -> None:
        result = load_text(VALID)
        self.assertTrue(result.ok, render_errors(result.errors))
        assert result.lock is not None
        self.assertIsNone(result.lock.profiles[0].embedding_space)

        with_space = load_text(VALID_WITH_SPACE)
        self.assertTrue(with_space.ok, render_errors(with_space.errors))
        assert with_space.lock is not None
        self.assertEqual(with_space.lock.profiles[0].embedding_space, EmbeddingSpace("example-space-17", "generator", 17))

    def test_cotenant_reserve_is_optional(self) -> None:
        absent = load_text(VALID)
        self.assertTrue(absent.ok, render_errors(absent.errors))
        assert absent.lock is not None
        self.assertEqual(absent.lock.profiles[0].cotenant_reserve, ())
        self.assertEqual(absent.lock.profiles[0].cotenant_reserve_gb(0), 0)

        present = load_text(VALID_WITH_RESERVE)
        self.assertTrue(present.ok, render_errors(present.errors))
        assert present.lock is not None
        profile = present.lock.profiles[0]
        self.assertEqual(profile.cotenant_reserve, (CotenantReserve(0, 3), CotenantReserve(1, 5)))
        self.assertEqual(profile.cotenant_reserve_gb(1), 5)
        self.assertEqual(profile.cotenant_reserve_gb(2), 0)

    def test_cotenant_reserve_shape_refusals(self) -> None:
        path = "profiles.2x96v-256d.cotenant_reserve_gb"
        cases = (
            (VALID_WITH_RESERVE.replace(RESERVE, "    cotenant_reserve_gb: []\n"), "expected a mapping", path),
            (VALID_WITH_RESERVE.replace("      1: 5\n", "      2: 5\n"), "below requires.gpu.count", f"{path}.2"),
            (VALID_WITH_RESERVE.replace("      0: 3\n", "      0: -1\n"), "non-negative integer", f"{path}.0"),
            (VALID_WITH_RESERVE.replace("      0: 3\n", "      0: true\n"), "non-negative integer", f"{path}.0"),
            (VALID_WITH_RESERVE.replace("      0: 3\n", "      first: 3\n"), "whole-number GPU position", f"{path}.first"),
            (
                VALID_WITH_RESERVE.replace("      0: 3\n      1: 5\n", "      true: 3\n"),
                "whole-number GPU position",
                f"{path}.True",
            ),
        )
        for text, fragment, key_path in cases:
            with self.subTest(fragment=fragment, key_path=key_path):
                self.assert_refused(text, fragment, key_path=key_path)
                result = load_text(text)
                self.assertTrue(
                    all("docs/runbooks/release-files.md §4" in error.fix for error in result.errors)
                )

        collected = load_text(
            VALID_WITH_RESERVE.replace("      0: 3\n      1: 5\n", "      0: -1\n      2: true\n")
        )
        self.assertEqual(
            {error.key_path for error in collected.errors},
            {f"{path}.0", f"{path}.2"},
        )
        self.assertEqual(len(collected.errors), 3)

    def test_embedding_space_shape_refusals(self) -> None:
        path = "profiles.2x96v-256d.embedding_space"
        self.assert_space_refused(VALID_WITH_SPACE.replace(SPACE, "    embedding_space: []\n"), "expected a mapping", path)
        self.assert_space_refused(VALID_WITH_SPACE.replace("      dimensions: 17\n", "      dimensions: 17\n      future: true\n"), "Unknown key", f"{path}.future")
        for key, line in (
            ("id", "      id: example-space-17\n"),
            ("role", "      role: generator\n"),
            ("dimensions", "      dimensions: 17\n"),
        ):
            with self.subTest(missing=key):
                text = VALID_WITH_SPACE.replace(SPACE, SPACE.replace(line, ""))
                self.assert_space_refused(text, "missing required key", f"{path}.{key}")

        for bad_id in ("Example-space", "example--space", "-example-space", "example_space"):
            with self.subTest(id=bad_id):
                text = VALID_WITH_SPACE.replace("id: example-space-17", f"id: {bad_id}")
                self.assert_space_refused(text, "lowercase hyphenated embedding space id", f"{path}.id")

        text = VALID_WITH_SPACE.replace(SPACE, SPACE.replace("role: generator", "role: absent"))
        self.assert_space_refused(text, "not pinned", f"{path}.role")
        for bad_dimensions in ("0", "-1", "true", '"17"'):
            with self.subTest(dimensions=bad_dimensions):
                text = VALID_WITH_SPACE.replace("dimensions: 17", f"dimensions: {bad_dimensions}")
                self.assert_space_refused(text, "positive integer", f"{path}.dimensions")

    def test_unknown_key_names_nearest(self) -> None:
        result = load_text(VALID.replace("reference: 2x96v-256d", "referance: 2x96v-256d"))
        self.assertTrue(any("nearest valid key is 'reference'" in error.problem for error in result.errors))

    def test_missing_key(self) -> None:
        self.assert_refused(VALID.replace("      data_volume_gb: 4000\n", ""), "missing required key", key_path="profiles.2x96v-256d.requires.data_volume_gb")

    def test_memory_table_refusals(self) -> None:
        without_memory = VALID[: VALID.index("    memory:\n")] + VALID[VALID.index("    models:\n") :]
        self.assert_refused(without_memory, "missing required key", key_path="profiles.2x96v-256d.memory")
        memory_start = VALID.index("    memory:\n")
        models_start = VALID.index("    models:\n")
        empty_memory = VALID[:memory_start] + "    memory: {}\n" + VALID[models_start:]
        self.assert_refused(empty_memory, "non-empty mapping", key_path="profiles.2x96v-256d.memory")
        self.assert_refused(VALID.replace("      caddy:\n", "      bad.service:\n"), "service name")
        self.assert_refused(VALID.replace("        gb: 1\n", "        gb: one\n", 1), "positive integer", key_path="profiles.2x96v-256d.memory.caddy.gb")
        self.assert_refused(VALID.replace("        gb: 1\n", "        gb: 0\n", 1), "positive integer", key_path="profiles.2x96v-256d.memory.caddy.gb")
        self.assert_refused(VALID.replace("      caddy:\n        gb: 1", "      caddy:\n        gb: 1\n        future: true"), "Unknown key", key_path="profiles.2x96v-256d.memory.caddy.future")
        self.assert_refused(VALID.replace("        gb: 32\n        role: generator", "        gb: 32\n        role: alternate"), "not pinned", key_path="profiles.2x96v-256d.memory.gideon-generator.role")
        # The table's errors name the runbook section for this lock.
        result = load_text(without_memory)
        self.assertTrue(all("docs/runbooks/release-files.md §4" in error.fix for error in result.errors if error.key_path == "profiles.2x96v-256d.memory"))

    def test_profile_role_and_flag_names_follow_grammars(self) -> None:
        self.assert_refused(VALID.replace("  2x96v-256d:\n", "  bad.name:\n").replace("reference: 2x96v-256d", "reference: bad.name"), "hardware profile name")
        self.assert_refused(VALID.replace("      generator:\n", "      bad.role:\n"), "lowercase hyphenated role name")
        self.assert_refused(VALID.replace("            max-model-len:", "            max.model-len:"), "hyphenated flag name")

    def test_pin_grammars(self) -> None:
        self.assert_refused(VALID.replace("repo: example/fixture", "repo: example"), "owner/name repository")
        self.assert_refused(VALID.replace(FAKE_REVISION, FAKE_REVISION[:-1]), "40-character lowercase")
        self.assert_refused(VALID.replace(FAKE_DIGEST, "sha256:abc"), "sha256:<64")

    def test_required_model_sections_and_scalar_values(self) -> None:
        self.assert_refused(VALID.replace("      generator:\n", "      alternate:\n"), "missing required key", key_path="profiles.2x96v-256d.models.generator")
        empty_files = VALID[: VALID.index("        files:\n")] + "        files: {}\n"
        self.assert_refused(empty_files, "non-empty mapping", key_path="profiles.2x96v-256d.models.generator.files")
        self.assert_refused(VALID.replace("            reasoning-parser: qwen3", "            reasoning-parser: [qwen3]"), "scalar flag value")
        self.assert_refused(VALID.replace("          config.json:\n", "          .config.json:\n"), "relative POSIX file path")
        self.assert_refused(VALID.replace("            size: 1\n", "            size: 1\n            future: true\n"), "nearest valid key is 'sha256'")
        self.assert_refused(VALID.replace('        compute_capability: "12.0"', "        compute_capability: 12.0"), "non-empty string")
        self.assert_refused(VALID.replace('        compute_capability: "12.0"', '        compute_capability: "sm120"'), "major.minor")
        self.assert_refused(VALID.replace("      platform: x86_64", "      platform: X86-64"), "uname -m")

    def test_size_and_gpu_bounds(self) -> None:
        self.assert_refused(VALID.replace("            size: 1", "            size: -1"), "non-negative integer")
        self.assert_refused(VALID.replace("        gpu: 0", "        gpu: 2"), "below requires.gpu.count")

    def test_environment_names_must_be_uppercase_and_not_secret_like(self) -> None:
        self.assert_refused(VALID.replace("HF_HOME", "hf_home"), "environment name matching")
        self.assert_refused(VALID.replace("HF_HOME", "API_TOKEN"), "never be secret-like")

    def test_reference_must_name_a_profile(self) -> None:
        self.assert_refused(VALID.replace("reference: 2x96v-256d", "reference: missing-profile"), "must name a profile")

    def test_duplicate_empty_and_non_mapping_documents(self) -> None:
        self.assert_refused(VALID + "version: 2\n", "duplicate mapping key")
        self.assert_refused("", "is empty")
        self.assert_refused("[]", "root must be a mapping")

    def test_missing_permission_and_encoding_errors(self) -> None:
        missing = load_models_lock("/tmp/models-lock-not-found", host=FakeHost())
        self.assertIn("models lock is missing", missing.errors[0].problem)
        denied = load_models_lock("/tmp/models.lock", host=FakeHost(error=PermissionError("denied")))
        self.assertIn("permissions", denied.errors[0].problem)
        invalid = load_models_lock("/tmp/models.lock", host=FakeHost(error=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")))
        self.assertIn("not valid UTF-8", invalid.errors[0].problem)
        for result in (missing, denied, invalid):
            self.assertTrue(result.errors[0].fix)
            self.assertTrue(render_errors(result.errors).endswith(result.errors[0].fix))


class ExportedGrammars(unittest.TestCase):
    def test_exported_grammars_reject_unsafe_names(self) -> None:
        self.assertIsNotNone(PROFILE_NAME.fullmatch("2x96v-256d"))
        self.assertIsNotNone(ROLE_NAME.fullmatch("generator-1"))
        self.assertIsNotNone(FLAG_NAME.fullmatch("max-num-seqs"))
        self.assertIsNotNone(ENV_NAME.fullmatch("HF_HOME"))
        self.assertIsNotNone(SERVICE_NAME.fullmatch("open-webui"))
        for grammar in (ROLE_NAME, SERVICE_NAME, FLAG_NAME):
            for value in ("-bad", "bad.name", "bad/name"):
                self.assertIsNone(grammar.fullmatch(value))
