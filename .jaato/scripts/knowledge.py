"""Embed a domain-knowledge file into an agent's system prompt.

Called from a persona as ``{{!py:scripts/knowledge.py <name>.md}}``.
The framework runs this during ``configure()``, before the agent's
first turn, so the knowledge is already PRESENT in the prompt rather
than being something the agent must decide to go and look up.

Keeping it in a file rather than inline in the persona separates two
things that change for different reasons: WHO the agent is (the
persona) and WHAT the domain says (this). A second persona on the same
domain includes the same file, and correcting the domain corrects both.

THIS IS NOT THE FRAMEWORK'S PATTERN FOR DOMAIN KNOWLEDGE, and it is
said here rather than left for someone to copy out of an example.

Under jaato the correct way to give an agent domain knowledge is a
REFERENCE: the `references` plugin. Knowledge lives in a catalog with IDs and tags, and a profile
pre-selects what an agent starts with:

    plugins: [references]
    plugin_configs:
      references:
        preselected: [siniestro_intake]
        lookup_strategy: hybrid

That buys what this script cannot: several documents selected by tag
rather than one by filename, semantic matching so a question about a
crash pulls the crash document, transitive references, and the agent
able to reach further material mid-call with `selectReferences` instead
of carrying everything in its prompt from the first token.

This demo uses the prefetch instead for one specific reason. The
speaking profile runs `plugins: []` deliberately -- every tool in the
schema is a chance for the model to emit a tool call instead of speech,
which is the one thing a voice stage exists to produce, and
`listReferences` is a CORE tool that would be in the schema from the
first turn. Against a model already prone to reading tool names aloud,
that is a real cost for a demo whose subject is audio, not retrieval.

So: prefetch for one small document in a voice demo; `references` for a
production agent, where the catalog grows, the knowledge is shared
between agents, and a tool call costs nothing anyone can hear.
"""
from pathlib import Path


def render(context, args) -> str:
    """Return the named file from ``.jaato/knowledge/``.

    Raises rather than returning a placeholder: a mandatory directive
    that silently rendered nothing would leave an agent believing it
    had domain knowledge it never received, which is the failure this
    file exists to prevent.
    """
    if not args:
        raise ValueError("knowledge.py needs a file name, e.g. "
                         "{{!py:scripts/knowledge.py siniestro_intake.md}}")
    name = args[0] if isinstance(args, (list, tuple)) else str(args)
    root = Path(__file__).resolve().parent.parent / "knowledge"
    path = (root / name).resolve()
    if root not in path.parents:
        raise ValueError(f"{name} is outside the knowledge directory")
    if not path.is_file():
        raise FileNotFoundError(f"no knowledge file at {path}")
    return path.read_text(encoding="utf-8").strip()
