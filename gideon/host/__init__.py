"""The ``gideon host`` subtree (spec §1.5).

Self-contained by design: everything under gideon/host/ runs on a bare Ubuntu
Server install where only the standard library and python3-yaml exist —
never PyPI (§2.3). tests/test_host_import_boundary.py enforces the seam.
"""
