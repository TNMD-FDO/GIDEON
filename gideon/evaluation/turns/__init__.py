"""The evaluation turn harness's drivers and service-door helpers.

The API and service modes are importable with only the standard library,
PyYAML, and the repository's :mod:`gideon` package, and run with the system
Python. The browser mode runs from the dev venv's interpreter: its one
third-party dependency, Playwright, is imported inside
:mod:`gideon.evaluation.turns.chromium` alone and only when that mode
launches. Neither mode is ever a pip dependency of the product. The command
entry remains ``python3 -m tools.turns``.
"""
