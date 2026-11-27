"""Release sampling, streamed request fields, and the closed-book prompt."""

import unittest
from pathlib import Path
from unittest.mock import patch

from gideon.evaluation.sampling import THINKING_SAMPLING
from gideon.host.models import HardwareProfile, ModelPin, load_models_lock
from gideon.host.render.engine import GENERATOR
from gideon.turn import generator
from gideon.turn.generator import GENERATOR_SAMPLING, request_body, sampling_for
from gideon.turn.prompt import messages
from gideon.turn.research import RESEARCH_CONFIG

ROOT = Path(__file__).resolve().parent.parent


def generator_pin(profile: HardwareProfile | None) -> ModelPin:
    assert profile is not None, "The hardware profile must exist."
    pin = profile.model(GENERATOR.role)
    assert pin is not None, f"{profile.name} must pin a generator."
    return pin


class TurnGenerator(unittest.TestCase):
    def setUp(self) -> None:
        result = load_models_lock(ROOT / "models.lock")
        assert result.lock is not None, result.errors
        self.lock = result.lock

    def test_each_profile_has_verified_sampling_or_a_known_exclusion(self) -> None:
        trial = generator_pin(self.lock.profile("1x32v-120d"))
        exclusions = {
            trial.repo: "The snapshot was absent from the box at capture time. Read it at trial profile acceptance.",
        }
        covered = set()
        prompt = messages("What did the fictitious harbor board decide?", ())
        for profile in self.lock.profiles:
            pin = generator_pin(profile)
            with self.subTest(profile=profile.name, repository=pin.repo):
                sampling = sampling_for(pin.repo)
                if pin.repo in exclusions:
                    self.assertIsNone(sampling, exclusions[pin.repo])
                    continue
                assert sampling is not None, f"Read the pinned generation config and add sampling for {pin.repo}."
                covered.add(pin.repo)
                digests = [file.sha256 for file in pin.files if file.path == "generation_config.json"]
                self.assertEqual(
                    digests, [sampling.config_digest],
                    f"Read the pinned generation config and update sampling for {pin.repo}.",
                )
                served_name = pin.serve.served_name
                expected = {
                    "model": served_name,
                    "messages": prompt,
                    "max_tokens": RESEARCH_CONFIG["max_tokens"],
                    **sampling.fields,
                    "stream": True,
                    "stream_options": {"include_usage": True, "continuous_usage_stats": True},
                }
                self.assertEqual(request_body(served_name, prompt, sampling), expected)
                self.assertEqual(
                    request_body(served_name, prompt, sampling, thinking_off=True),
                    {**expected, "chat_template_kwargs": {"enable_thinking": False}},
                )
        self.assertEqual(set(GENERATOR_SAMPLING), covered)

    def test_request_reads_the_research_config(self) -> None:
        pin = generator_pin(self.lock.profile(self.lock.reference))
        sampling = sampling_for(pin.repo)
        assert sampling is not None
        prompt = messages("What did the fictitious harbor board decide?", ())
        baseline = request_body(pin.serve.served_name, prompt, sampling)
        max_tokens = RESEARCH_CONFIG["max_tokens"]
        assert type(max_tokens) is int
        changed = max_tokens + 1
        with patch.object(generator, "RESEARCH_CONFIG", {**RESEARCH_CONFIG, "max_tokens": changed}):
            self.assertEqual(
                request_body(pin.serve.served_name, prompt, sampling), {**baseline, "max_tokens": changed},
            )
        with (
            patch.object(generator, "RESEARCH_CONFIG", {**RESEARCH_CONFIG, "tag_grammar": "fixture-grammar"}),
            self.assertRaisesRegex(ValueError, "tag_grammar"),
        ):
            request_body(pin.serve.served_name, prompt, sampling)

    def test_reference_sampling_matches_evaluation(self) -> None:
        reference = generator_pin(self.lock.profile(self.lock.reference))
        sampling = sampling_for(reference.repo)
        assert sampling is not None
        self.assertEqual(sampling.fields, THINKING_SAMPLING)


class TurnPrompt(unittest.TestCase):
    def test_numbered_passages_with_optional_captions_precede_the_question(self) -> None:
        self.assertEqual(
            messages(
                "What did the fictitious harbor board decide?",
                (("Invented Harbor Board v. Rowan", "The mooring stays open."), ("", "Notice is required.")),
            ),
            [
                {
                    "role": "system",
                    "content": "Use only the numbered passages below as sources for your answer, without relying on memory.",
                },
                {
                    "role": "user",
                    "content": "[1] Invented Harbor Board v. Rowan\nThe mooring stays open.\n\n"
                    "[2] Notice is required.\n\nQuestion: What did the fictitious harbor board decide?",
                },
            ],
        )

    def test_empty_passages_leave_only_the_question_in_the_user_message(self) -> None:
        self.assertEqual(
            messages("What happened at the fictitious harbor?", ())[1],
            {"role": "user", "content": "Question: What happened at the fictitious harbor?"},
        )
