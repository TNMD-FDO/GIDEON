"""Run eval-set slices from the host checkout and record them.

The package imports the standard library, ``yaml``, and ``gideon`` alone: the
command runs on the box's system Python, never from an image (spec §18.1's
``httpx`` and ``numpy`` are an allowance not taken).
"""
