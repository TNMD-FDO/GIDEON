"""The test modules a pull request's hosted gate runs.

These are the modules naming a pin file, a render fixture, or a workflow file,
less the named exclusions. A tripwire under tests holds the lists complete.
"""

from pathlib import Path

from tools.exportboundary import absent_from_export

SELECTED = (
    "test_acceptance",
    "test_alerts",
    "test_apply",
    "test_archive_push",
    "test_backup",
    "test_ci_workflow",
    "test_cistack",
    "test_corpus_lockfiles_committed",
    "test_corpus_watch",
    "test_cotenants",
    "test_dispatch",
    "test_drill",
    "test_engine_verify",
    "test_environment",
    "test_export_boundary",
    "test_export_tree_check",
    "test_gpus",
    "test_host_lock",
    "test_host_path_forms",
    "test_host_provision",
    "test_host_steps",
    "test_imagebuild",
    "test_images_lock",
    "test_imagestore",
    "test_index_gc",
    "test_index_promote",
    "test_index_retire",
    "test_install",
    "test_install_sequence",
    "test_lock_coupling",
    "test_models_lock",
    "test_no_self_update",
    "test_owui",
    "test_pin_selection",
    "test_pinwatch",
    "test_pinwatch_bumped",
    "test_preflight",
    "test_preflight_checks",
    "test_registry_mirror",
    "test_render",
    "test_render_ci",
    "test_render_consumers",
    "test_render_grafana",
    "test_render_model_servers",
    "test_render_owui",
    "test_render_searxng",
    "test_research_notes",
    "test_restore",
    "test_rotate",
    "test_segmenter_kit",
    "test_skills_vendored",
    "test_toolchain_pins",
    "test_turns",
    "test_turns_browser",
    "test_turns_door",
    "test_uninstall",
    "test_upgrade",
    "test_venv_build",
    "test_weights",
    "test_worker_pipeline",
)

EXCLUDED = {
    "test_corpus_cut": "The module reads a pin file as one input of a command test.",
    "test_corpus_install": "The module reads a pin file as one input of a command test.",
    "test_corpus_install_build": "The module reads a pin file as one input of a command test.",
    "test_evaluation_preparation": "The module reads a pin file as one input of a command test.",
    "test_evaluation_run": "The module reads a pin file as one input of a command test.",
    "test_index_build": "The module reads a pin file as one input of a command test.",
    "test_judgments_pool": "The module reads a pin file as one input of a command test.",
    "test_office_values": "The module walks the whole tree and reads no pinned value.",
    "test_smoke_slice": "The module reads a pin file as one input of a command test.",
}


def selected_paths(root: Path) -> tuple[Path, ...]:
    """Return the selected modules' paths in order under ``root``.

    A module is dropped only when the export omits it and it is absent; any
    other missing module is kept, so pytest fails on it and the gate is red.
    """

    relatives = (Path("tests") / f"{stem}.py" for stem in SELECTED)
    return tuple(
        root / relative for relative in relatives if not absent_from_export(relative, root)
    )
