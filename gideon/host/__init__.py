"""The ``gideon host`` subtree.

Self-contained by design: everything under gideon/host/ runs on a bare Ubuntu
Server install where only the standard library and python3-yaml exist —
never PyPI. tests/test_host_import_boundary.py enforces the seam.
"""
