"""Deliberately regenerate the committed render fixture bytes.

The three fixtures are the three host kinds: ``example`` the build box,
``second-office`` an office's GPU host, ``no-gpu`` a no-GPU host.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXED_SECRETS = {
    "ldap_bind_password": "bind-$-'\"#password",
    "postgres_openwebui_password": "postgres-openwebui-password",
    "gideon_admin_password": "gideon-admin-password",
    "engine_api_key": "engine-api-key",
    "searxng_secret_key": "searxng-secret-key",
}
FIXED_API_SOURCES_DIGEST = "sha256:" + "0" * 64

def _loaded[T](value: T | None, errors: object) -> T:
    if value is None:
        raise RuntimeError(f"fixture inputs could not be loaded: {errors}")
    return value


def main() -> None:
    from gideon.host.images import load_image_lock
    from gideon.host.lock import load_host_lock
    from gideon.host.models import HardwareProfile, load_models_lock, select_profile
    from gideon.host.render import RenderInputs, render_all
    from gideon.host.render.command import load_templates, manifest_document
    from gideon.host.render.facts import HostFacts
    from gideon.host.site import load_site
    from gideon.host.sysio import RealHost

    gpu_facts = HostFacts(
        (
            "GPU-11111111-1111-1111-1111-111111111111",
            "GPU-22222222-2222-2222-2222-222222222222",
        ),
        service_gid=4242,
    )
    no_gpu_facts = HostFacts((), service_gid=4242)
    host = RealHost()
    lock_result = load_host_lock(ROOT / "host.lock", host=host)
    images_result = load_image_lock(ROOT / "images.lock", host=host)
    models_result = load_models_lock(ROOT / "models.lock", host=host)
    lock = _loaded(lock_result.lock, lock_result.errors)
    images = _loaded(images_result.lock, images_result.errors)
    models = _loaded(models_result.lock, models_result.errors)
    templates = load_templates(host, ROOT)
    lock_text = host.read_text(ROOT / "host.lock")
    models_lock_text = host.read_text(ROOT / "models.lock")

    sites = (
        ("example", ROOT / "config/site.example.yaml", False, True),
        ("second-office", ROOT / "tests/fixtures/site/second-office.yaml", False, False),
        ("no-gpu", ROOT / "config/site.example.yaml", True, False),
    )
    for name, site_path, no_gpu, build_box in sites:
        site_result = load_site(site_path, host=host)
        site = _loaded(site_result.config, site_result.errors)
        profile = select_profile(models, site.hardware_profile)
        if not isinstance(profile, HardwareProfile):
            raise TypeError(f"fixture profile could not be selected: {profile}")
        site_text = host.read_text(site_path)
        inputs = RenderInputs(
            site=site,
            lock=lock,
            images=images,
            facts=no_gpu_facts if no_gpu else gpu_facts,
            profile=profile,
            templates=templates,
            release="fixture",
            secrets={
                **FIXED_SECRETS,
                **(
                    {"proxy_auth": "proxy-user:p@$$-'\"#password"}
                    if name == "second-office"
                    else {}
                ),
            },
            checkout="/opt/gideon",
            api_sources_digest=FIXED_API_SOURCES_DIGEST,
            no_gpu=no_gpu,
            build_box=build_box,
        )
        rendered = render_all(inputs)
        manifest = manifest_document(
            rendered,
            inputs,
            site_text=site_text,
            lock_text=lock_text,
            models_lock_text=models_lock_text,
        )
        output = ROOT / "tests/fixtures/render" / name
        for rendered_file in rendered.files:
            target = output / rendered_file.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered_file.content, encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        (output / "manifest.yaml").write_text(manifest, encoding="utf-8")


if __name__ == "__main__":
    main()
