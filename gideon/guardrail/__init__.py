"""The arithmetic guardrail's judge.

The code enforcement of the no-model-arithmetic rule, as five modules each
importing only from those before it: ``grammar`` (the bounds, the
vocabularies, and every pattern source), ``families`` (pattern families judged
in ``FAMILIES`` order — the deadline family, the Guidelines family, sentence
credit — each carrying its refusal, its figure form and normaliser, its
restatement constructions, its confirmation context, and its own pattern-set
version), ``judge`` (the judge's entries over a text, a rendered message, and a
floor), ``writer`` (the guardrail trip's content-free row with its silent
writer, ``record_trip``), and ``window`` (the lag window's state and check over
a stream, ``StreamCheck``, text in and released text out). This module binds
every public name of the five, so callers read one surface.

General's service and ``engine verify`` call it, with the turn harness beside
them.  Every module imports the standard library only at module level: the
writer's import of the Postgres driver sits inside
``write_trip_row``, which no host path calls.
"""

from gideon.guardrail.grammar import *  # noqa: F403, I001
from gideon.guardrail.families import *  # noqa: F403
from gideon.guardrail.judge import *  # noqa: F403
from gideon.guardrail.writer import *  # noqa: F403
from gideon.guardrail.window import *  # noqa: F403
