"""A minimal Ironclad tool skill: `CASE` + a synchronous `run`.

The typed signature of `run` becomes the tool's JSON schema (auto-derived); the return string
goes back to the model. This is the entire contract — see `docs/plugin-api.md` / ADR-0004.
"""

CASE = {
    "name": "reverse",                 # the tool name the model calls
    "description": "Reverse a string",
    "capability": "reverse",           # unique skill id
}


def run(text: str) -> str:
    """Return *text* reversed."""
    return text[::-1]
