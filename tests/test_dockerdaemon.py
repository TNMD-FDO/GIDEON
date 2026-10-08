"""Truth table for ownership of the shared Docker daemon JSON object."""

import json
import unittest

from gideon.host import dockerdaemon


class DockerDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base: dict[str, object] = {
            "data-root": "/var/lib/docker",
            "log-driver": "journald",
            "features": {"cdi": True},
        }
        self.wanted = dockerdaemon.needs(None, None, None)

    def test_owned_names_paths_defaults_and_conditions(self) -> None:
        self.assertEqual(
            [(key.name, key.path) for key in dockerdaemon.OWNED_KEYS],
            [
                ("data-root", ("data-root",)),
                ("log-driver", ("log-driver",)),
                ("features.cdi", ("features", "cdi")),
                ("proxies.http-proxy", ("proxies", "http-proxy")),
                ("proxies.https-proxy", ("proxies", "https-proxy")),
                ("insecure-registries", ("insecure-registries",)),
                ("default-address-pools", ("default-address-pools",)),
            ],
        )
        self.assertEqual(dockerdaemon.OWNED_KEYS[0].defaults, ("/var/lib/docker",))
        self.assertEqual(dockerdaemon.OWNED_KEYS[1].defaults, ("json-file",))
        self.assertEqual(dockerdaemon.OWNED_KEYS[2].defaults, ())
        self.assertEqual(dockerdaemon.OWNED_KEYS[5].defaults, ([],))
        self.assertEqual(dockerdaemon.OWNED_KEYS[6].defaults, ([],))
        self.assertTrue(dockerdaemon.OWNED_KEYS[5].membership)
        self.assertFalse(dockerdaemon.OWNED_KEYS[6].membership)
        self.assertFalse(any(key.membership for key in (*dockerdaemon.OWNED_KEYS[:5], dockerdaemon.OWNED_KEYS[6])))

        self.assertEqual([need.key.name for need in self.wanted], [key.name for key in dockerdaemon.OWNED_KEYS[:3]])
        self.assertEqual(len(dockerdaemon.OWNED_KEYS), 7)
        both = dockerdaemon.needs("http://proxy:3128", "registry.example:5000", "198.18.0.0/16")
        self.assertEqual([need.key.name for need in both], [key.name for key in dockerdaemon.OWNED_KEYS])
        self.assertEqual([need.value for need in both[3:]], ["http://proxy:3128", "http://proxy:3128", "registry.example:5000", [{"base": "198.18.0.0/16", "size": 24}]])
        self.assertEqual([need.key.name for need in dockerdaemon.needs("http://proxy:3128", None, None)], [key.name for key in dockerdaemon.OWNED_KEYS[:5]])
        self.assertEqual([need.key.name for need in dockerdaemon.needs(None, "registry.example:5000", None)], [key.name for key in (*dockerdaemon.OWNED_KEYS[:3], dockerdaemon.OWNED_KEYS[5])])
        self.assertEqual([need.key.name for need in dockerdaemon.needs(None, None, "")], [key.name for key in dockerdaemon.OWNED_KEYS[:3]])

    def test_absent_default_met_and_short_scalars(self) -> None:
        self.assertEqual(dockerdaemon.read({}, self.wanted).to_set, ("data-root", "log-driver", "features.cdi"))
        self.assertEqual(dockerdaemon.read(self.base, self.wanted).to_set, ())
        for name, default in (("data-root", "/var/lib/docker"), ("log-driver", "json-file")):
            with self.subTest(name=name):
                current = dict(self.base)
                del current[name]
                self.assertEqual(dockerdaemon.read(current, self.wanted).to_set, (name,))
                current[name] = default
                expected = () if name == "data-root" else (name,)
                self.assertEqual(dockerdaemon.read(current, self.wanted).to_set, expected)
        moved = {**self.base, "data-root": "/srv/docker", "log-driver": "syslog"}
        self.assertEqual(
            dockerdaemon.read(moved, self.wanted).short,
            (("data-root", '"/srv/docker"', '"/var/lib/docker"'), ("log-driver", '"syslog"', '"journald"')),
        )

    def test_nested_keys_and_foreign_siblings(self) -> None:
        current = {**self.base, "features": {"cdi": True, "containerd-snapshotter": True}, "runtimes": {"custom": {}}}
        reading = dockerdaemon.read(current, self.wanted)
        self.assertEqual(reading.foreign, ("features.containerd-snapshotter", "runtimes"))
        self.assertEqual(reading.short, ())
        self.assertEqual(dockerdaemon.read({**self.base, "features": {}}, self.wanted).to_set, ("features.cdi",))
        self.assertEqual(
            dockerdaemon.read({**self.base, "features": {"cdi": False}}, self.wanted).short,
            (("features.cdi", "false", "true"),),
        )
        self.assertEqual(
            dockerdaemon.read({**self.base, "features": {"cdi": 1}}, self.wanted).short,
            (("features.cdi", "1", "true"),),
        )

    def test_proxy_needs_and_lapsed_proxy(self) -> None:
        proxy = "http://proxy.example:3128"
        wanted = dockerdaemon.needs(proxy, None, None)
        self.assertEqual(dockerdaemon.read(self.base, wanted).to_set, ("proxies.http-proxy", "proxies.https-proxy"))
        current = {**self.base, "proxies": {"http-proxy": proxy, "https-proxy": proxy, "no-proxy": "localhost"}}
        reading = dockerdaemon.read(current, wanted)
        self.assertEqual(reading.to_set, ())
        self.assertEqual(reading.foreign, ("proxies.no-proxy",))
        self.assertEqual(dockerdaemon.read(current, self.wanted).foreign, ("proxies.http-proxy", "proxies.https-proxy", "proxies.no-proxy"))
        current["proxies"] = {"http-proxy": "http://user:pass@other.example:3128", "https-proxy": proxy}
        short = dockerdaemon.read(current, wanted).short
        self.assertEqual(short[0][0], "proxies.http-proxy")
        self.assertIn("…@other.example", short[0][1])
        self.assertNotIn("user:pass", short[0][1])

    def test_registry_membership_default_and_short(self) -> None:
        wanted = dockerdaemon.needs(None, "registry.example:5000", None)
        self.assertEqual(dockerdaemon.read(self.base, wanted).to_set, ("insecure-registries",))
        self.assertEqual(dockerdaemon.read({**self.base, "insecure-registries": []}, wanted).to_set, ("insecure-registries",))
        met = {**self.base, "insecure-registries": ["other.example:5000", "registry.example:5000"]}
        self.assertEqual(dockerdaemon.read(met, wanted).to_set, ())
        self.assertEqual(dockerdaemon.read(met, wanted).short, ())
        short = {**self.base, "insecure-registries": ["other.example:5000"]}
        self.assertEqual(dockerdaemon.read(short, wanted).short, (("insecure-registries", '["other.example:5000"]', '"registry.example:5000"'),))
        self.assertEqual(dockerdaemon.read(met, self.wanted).foreign, ("insecure-registries",))

    def test_address_pool_requires_one_exact_entry_and_preserves_foreign_keys(self) -> None:
        """Docker allocates from every listed pool, so a second entry is short."""

        base = "198.18.0.0/16"
        expected = [{"base": base, "size": 24}]
        wanted = dockerdaemon.needs(None, None, base)
        self.assertEqual([need.key.name for need in wanted], [
            *[key.name for key in dockerdaemon.OWNED_KEYS[:3]],
            "default-address-pools",
        ])
        self.assertEqual(wanted[-1].value, expected)
        self.assertEqual(dockerdaemon.address_pool_value(base), expected)
        self.assertEqual(dockerdaemon.render(expected), '[{"base": "198.18.0.0/16", "size": 24}]')

        for current in (self.base, {**self.base, "default-address-pools": []}):
            with self.subTest(current=current):
                self.assertEqual(dockerdaemon.read(current, wanted).to_set, ("default-address-pools",))
        equal = {**self.base, "default-address-pools": expected}
        self.assertEqual(dockerdaemon.read(equal, wanted).to_set, ())
        self.assertEqual(dockerdaemon.read(equal, wanted).short, ())

        for found in (
            [{"base": "198.19.0.0/16", "size": 24}],
            [{"base": base, "size": 25}],
            [*expected, {"base": "198.19.0.0/16", "size": 24}],
        ):
            with self.subTest(found=found):
                reading = dockerdaemon.read({**self.base, "default-address-pools": found}, wanted)
                self.assertEqual(reading.short, (
                    ("default-address-pools", json.dumps(found), json.dumps(expected)),
                ))
                self.assertEqual(reading.to_set, ())

        malformed = dockerdaemon.read(
            {**self.base, "default-address-pools": "invalid"}, wanted
        )
        self.assertEqual(malformed.malformed, (("default-address-pools", '"invalid"'),))
        self.assertEqual(malformed.short, ())
        self.assertEqual(
            dockerdaemon.read(equal, self.wanted).foreign,
            ("default-address-pools",),
        )

        current = {**self.base, "default-address-pools": [], "runtimes": {"custom": {}}}
        merged, changed = dockerdaemon.merge(current, wanted)
        self.assertTrue(changed)
        self.assertEqual(merged["default-address-pools"], expected)
        self.assertEqual(merged["runtimes"], current["runtimes"])
        self.assertEqual(current["default-address-pools"], [])

    def test_malformed_containers(self) -> None:
        for name, value in (("features", "on"), ("proxies", ["http://proxy"]), ("insecure-registries", "registry.example")):
            with self.subTest(name=name):
                current = {**self.base, name: value}
                reading = dockerdaemon.read(current, dockerdaemon.needs("http://proxy", "registry.example", None))
                self.assertIn((name, dockerdaemon.render(value)), reading.malformed)
                self.assertFalse(any(item[0].startswith(name) for item in reading.short))
        unowned = dockerdaemon.read({**self.base, "proxies": "on", "insecure-registries": "x"}, self.wanted)
        self.assertEqual(unowned.malformed, ())
        self.assertEqual(unowned.foreign, ("insecure-registries", "proxies"))

    def test_merge_sets_only_defaults_and_preserves_all_other_values(self) -> None:
        current: dict[str, object] = {
            "data-root": "/srv/docker",
            "log-driver": "json-file",
            "features": {"containerd-snapshotter": True},
            "runtimes": {"custom": {"path": "/usr/bin/custom"}},
            "insecure-registries": [],
        }
        wanted = dockerdaemon.needs("http://proxy:3128", "registry.example:5000", None)
        merged, changed = dockerdaemon.merge(current, wanted)
        self.assertTrue(changed)
        self.assertEqual(merged["data-root"], "/srv/docker")
        self.assertEqual(merged["log-driver"], "journald")
        self.assertEqual(merged["features"], {"containerd-snapshotter": True, "cdi": True})
        self.assertEqual(merged["proxies"], {"http-proxy": "http://proxy:3128", "https-proxy": "http://proxy:3128"})
        self.assertEqual(merged["insecure-registries"], ["registry.example:5000"])
        self.assertEqual(merged["runtimes"], current["runtimes"])
        self.assertIsNot(merged["runtimes"], current["runtimes"])
        self.assertEqual(current["log-driver"], "json-file")
        self.assertEqual(current["features"], {"containerd-snapshotter": True})
        self.assertNotIn("proxies", current)

        same, changed = dockerdaemon.merge(self.base, self.wanted)
        self.assertFalse(changed)
        self.assertEqual(same, self.base)
        self.assertIsNot(same["features"], self.base["features"])
        self.assertEqual(dockerdaemon.merge({}, self.wanted)[0], self.base)

    def test_render_and_text(self) -> None:
        self.assertEqual(dockerdaemon.render("http://user:pass@proxy.example:3128/path?q=1"), '"http://…@proxy.example:3128/path?q=1"')
        self.assertEqual(dockerdaemon.render("http://proxy.example:3128/path"), '"http://proxy.example:3128/path"')
        self.assertEqual(dockerdaemon.render("ordinary text"), '"ordinary text"')
        self.assertEqual(dockerdaemon.render(["registry.example:5000"]), '["registry.example:5000"]')
        self.assertEqual(
            dockerdaemon.render([{"http-proxy": "http://user:pass@proxy.example:3128"}]),
            '[{"http-proxy": "http://…@proxy.example:3128"}]',
        )
        self.assertEqual(dockerdaemon.text(self.base), json.dumps(self.base, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    unittest.main()
