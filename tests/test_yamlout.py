"""The deterministic YAML emitter: quoting, ordering, empties, round-trips."""

import unittest

import yaml  # type: ignore[import-untyped]

from gideon.host.render.yamlout import HEADER, dump


class Emitter(unittest.TestCase):
    def test_header_is_the_first_line(self) -> None:
        self.assertEqual(dump({"a": 1}).splitlines()[0], HEADER)

    def test_keys_bare_when_unambiguous_values_always_quoted(self) -> None:
        out = dump({"name": "gideon", "on": "x", "restart": "unless-stopped", "n": "2GB", "k.v-1_": True})
        self.assertIn('name: "gideon"\n', out)
        self.assertIn('"on": "x"\n', out)
        self.assertIn('"n": "2GB"\n', out)
        self.assertIn('restart: "unless-stopped"\n', out)
        self.assertIn("k.v-1_: true\n", out)

    def test_insertion_order_is_preserved(self) -> None:
        out = dump({"z": 1, "a": 2, "m": {"q": 1, "b": 2}})
        body = out.splitlines()[1:]
        self.assertEqual(body, ["z: 1", "a: 2", "m:", "  q: 1", "  b: 2"])

    def test_empties_and_null(self) -> None:
        out = dump({"networks": {"gideon": {}}, "ports": [], "x": None})
        self.assertEqual(out.splitlines()[1:], ["networks:", "  gideon: {}", "ports: []", "x: null"])

    def test_lists_of_mappings_and_nesting(self) -> None:
        out = dump({"items": [{"a": 1, "b": ["x", {"c": 2}]}, "s"]})
        self.assertEqual(
            out.splitlines()[1:],
            ["items:", "  - a: 1", "    b:", '      - "x"', "      - c: 2", '  - "s"'],
        )

    def test_long_digest_strings_are_never_folded(self) -> None:
        digest = "127.0.0.1:5000/caddy@sha256:" + "f" * 64
        out = dump({"image": digest})
        self.assertIn(f'image: "{digest}"\n', out)

    def test_round_trips_through_pyyaml(self) -> None:
        doc = {
            "name": "gideon",
            "services": {
                "caddy": {
                    "image": "127.0.0.1:5000/caddy@sha256:" + "0" * 64,
                    "ports": ["0.0.0.0:443:443"],
                    "volumes": ["/etc/gideon/tls:/etc/gideon/tls:ro", "caddy_data:/data"],
                    "secrets": ["tls_key"],
                    "networks": ["gideon"],
                }
            },
            "networks": {"gideon": {}},
            "secrets": {"tls_key": {"file": "/etc/gideon/secrets/tls_key"}},
            "odd": {"on": "yes", "3": "three", "quote\"d": "a\nb", "uni": "§ — ü"},
        }
        self.assertEqual(yaml.safe_load(dump(doc)), doc)

    def test_deterministic(self) -> None:
        doc = {"a": [1, {"b": "c"}], "d": {"e": None}}
        self.assertEqual(dump(doc), dump(doc))

    def test_unsupported_values_raise(self) -> None:
        with self.assertRaises(TypeError):
            dump({"f": 1.5})
        with self.assertRaises(TypeError):
            dump({1: "one"})  # type: ignore[dict-item]
        with self.assertRaises(TypeError):
            dump({"s": {"x"}})
