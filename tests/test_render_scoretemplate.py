"""Contracts for the reranker's rendered score template."""

import unittest
from dataclasses import replace

from test_render import (
    FIXTURE_CASES,
    ROOT,
    DirHost,
    checkout_files,
    inputs,
    render,
    trial_inputs,
)
from test_render_api import applied_record

from gideon.host.render import render_all
from gideon.host.render.command import compose_digests, recreate_judgment
from gideon.host.render.compose import service_names
from gideon.host.render.engine import RERANK
from gideon.host.render.scoretemplate import SCORE_TEMPLATE_PATH, SCORE_TEMPLATE_SOURCE


class ScoreTemplate(unittest.TestCase):
    def test_reference_gpu_hosts_emit_the_owned_template_verbatim(self) -> None:
        source = (ROOT / SCORE_TEMPLATE_SOURCE).read_bytes()
        self.assertEqual(SCORE_TEMPLATE_PATH, "model-servers/qwen3_reranker.jinja")
        self.assertEqual(SCORE_TEMPLATE_SOURCE, f"compose/{SCORE_TEMPLATE_PATH}")
        for host, site, no_gpu, build_box in FIXTURE_CASES:
            with self.subTest(host=host):
                rendered = render_all(inputs(site, no_gpu=no_gpu, build_box=build_box))
                if no_gpu:
                    self.assertNotIn(SCORE_TEMPLATE_PATH, rendered.by_path)
                    continue
                template = rendered.by_path[SCORE_TEMPLATE_PATH]
                self.assertEqual(template.relative_path, SCORE_TEMPLATE_PATH)
                self.assertEqual(template.mode, 0o644)
                self.assertEqual(template.owners, (RERANK.service_name,))
                self.assertEqual(template.content.encode(), source)
        self.assertNotIn(SCORE_TEMPLATE_PATH, render_all(trial_inputs()).by_path)

    def test_template_carries_the_card_prompt_and_preserves_the_final_newlines(self) -> None:
        source = (ROOT / SCORE_TEMPLATE_SOURCE).read_text()
        self.assertIn(
            '<|im_start|>system\n'
            'Judge whether the Document meets the requirements based on the Query and the Instruct '
            'provided. Note that the answer can only be "yes" or "no".<|im_end|>\n',
            source,
        )
        for label in ("<Instruct>", "<Query>", "<Document>"):
            with self.subTest(label=label):
                self.assertIn(f"\n{label}: {{{{ ", source)
        self.assertTrue(source.endswith('<|im_start|>assistant\n<think>\n\n</think>\n{{ "\\n" -}}\n'))

    def test_changed_template_recreates_only_the_reranker(self) -> None:
        base = inputs()
        applied = applied_record(base)
        changed = replace(base, templates={
            **base.templates,
            SCORE_TEMPLATE_SOURCE: base.templates[SCORE_TEMPLATE_SOURCE] + "\n{# fictitious change #}\n",
        })
        judgment = recreate_judgment(
            render_all(changed), applied, service_names(changed), compose_digests(changed)
        )
        self.assertEqual(judgment.services, (RERANK.service_name,))
        self.assertEqual(judgment.reasons(), (("changed rendered files", (RERANK.service_name,)),))

    def test_missing_template_refuses_with_the_release_checkout_fix(self) -> None:
        files = checkout_files()
        del files[str(ROOT / SCORE_TEMPLATE_SOURCE)]
        code, _, err = render(DirHost(files))

        self.assertEqual(code, 1)
        self.assertIn(f"Render source is missing: {SCORE_TEMPLATE_SOURCE}", err)
        self.assertIn("A checkout without it is not a release tree", err)
        self.assertNotIn("Traceback", err)
