"""Contracts for the committed models lock and its refusal shapes."""

import os
import subprocess
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path

from gideon.host.images import DIGEST
from gideon.host.models import (
    ENV_NAME,
    FILE_PATH,
    FLAG_NAME,
    HF_REPO,
    PROFILE_NAME,
    REVISION,
    ROLE_NAME,
    SERVICE_NAME,
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
    def test_committed_lock_has_exact_release_facts_and_grammar_pins(self) -> None:
        result = load_models_lock(LOCK)
        self.assertTrue(result.ok, render_errors(result.errors))
        self.assertIsInstance(result.lock, ModelsLock)
        assert result.lock is not None
        self.assertEqual(result.lock.version, 1)
        self.assertEqual(result.lock.reference, "2x96v-256d")
        self.assertEqual(len(result.lock.profiles), 1)

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
        self.assertEqual(
            profile.memory,
            (
                MemoryRow("caddy", 1, None),
                MemoryRow("prometheus", 2, None),
                MemoryRow("node-exporter", 1, None),
                MemoryRow("grafana", 3, None),
                MemoryRow("postgres", 20, None),
                MemoryRow("open-webui", 4, None),
                MemoryRow("gideon-generator", 32, "generator"),
                MemoryRow("searxng", 1, None),
                MemoryRow("dcgm-exporter", 2, None),
                MemoryRow("postgres-exporter", 1, None),
                MemoryRow("cadvisor", 5, None),
                MemoryRow("blackbox-exporter", 1, None),
            ),
        )
        self.assertEqual(profile.memory_row("gideon-generator"), MemoryRow("gideon-generator", 32, "generator"))
        self.assertIsNone(profile.memory_row("missing-service"))
        self.assertEqual(len(profile.models), 1)
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

    def test_models_dataclasses_are_frozen_and_slotted(self) -> None:
        for value in (
            PinnedFile("config.json", FAKE_DIGEST, 1),
            ServeBaseline("example", {}, {}),
            ModelPin("generator", "example/fixture", FAKE_REVISION, 0, ServeBaseline("example", {}, {}), ()),
            GpuRequirements("Example", "1.0", "Example GPU", 1, 1),
            ProfileRequirements("x86_64", GpuRequirements("Example", "1.0", "Example GPU", 1, 1), 1, 1),
            MemoryRow("example-service", 1, None),
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
        # The table's errors name the architecture leaf that owns the lock.
        result = load_text(without_memory)
        self.assertTrue(all("docs/archi/host.md" in error.fix for error in result.errors if error.key_path == "profiles.2x96v-256d.memory"))

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
