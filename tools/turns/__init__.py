"""The box-side turn harness.

The API mode is repository tooling importable with only the standard library,
PyYAML, and the repository's :mod:`gideon` package, run with the system Python.
The browser mode runs from the dev venv's interpreter: its one third-party
dependency, Playwright, is imported inside :mod:`tools.turns.chromium` alone and
only when that mode launches, so every other module here stays on the
standard library. Neither mode is ever a pip dependency of the product.
"""
