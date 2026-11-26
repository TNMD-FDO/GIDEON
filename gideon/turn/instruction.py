"""The release's query-side embedding instruction and input form."""

from typing import Final

# E4: the release's default query-side embedding instruction, a product artifact and never a site key.
RETRIEVAL_INSTRUCTION: Final[str] = (
    "Given a legal research question, retrieve passages from court opinions, "
    "statutes, rules, and sentencing guidelines that answer it"
)
INSTRUCTED_FORM: Final[str] = "Instruct: {task}\nQuery: {query}"


def instructed(question: str) -> str:
    """Apply the embedding model's query-side form with the release's default instruction."""

    return INSTRUCTED_FORM.format(task=RETRIEVAL_INSTRUCTION, query=question)
