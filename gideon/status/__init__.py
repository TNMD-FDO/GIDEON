"""The read-only terminal front door for GIDEON's box status.

The status presents needs attention, waiting on you, and at a glance. It exits
0 when no active page needs attention, 1 when one does, and 2 when it cannot
check. This package imports only the standard library, ``yaml``, and ``gideon``
and performs no writes.
"""
