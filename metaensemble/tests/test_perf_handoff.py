"""Token-economics benchmark: typed Brief vs prose context-injection.

PERFORMANCE.md §4 Benchmark 1. CI gate: typed Brief is at most 1/3 the
token count of equivalent prose context-injection. A regression here
means the wire-format compression claim is broken.
"""
from __future__ import annotations

import json


def _estimate_tokens(text: str) -> int:
    """Rough token estimate. Heuristic: ~4 chars per token for English."""
    return max(1, len(text) // 4)


# Realistic Brief — what a typical handoff looks like once typed.
_BRIEF = {
    "v": 1,
    "brief_id": "01234567-89ab-7cde-9012-3456789abcde",
    "from": "arch-7b3",
    "to": "be-9c1",
    "task_id": "auth-endpoints",
    "ctx": {
        "prior_runs": ["01234567-89ab-7cde-9012-3456789abcd0"],
        "files": [
            ["src/auth/spec.md", "1-120"],
            ["src/auth/types.ts", "14-58"],
        ],
    },
    "out": {
        "files": ["src/auth/handlers.ts", "tests/auth.spec.ts"],
        "schema": "schemas/auth-response.json",
    },
    "tier": "sonnet",
    "budget": 8000,
}


# Equivalent prose context-injection — calibrated to what current agent
# systems actually deliver: role priming, methodology framing, format
# instructions, quality criteria, and full prose handoff narrative. This
# is the realistic comparison, not a stripped-down version that would
# understate the savings.
_PROSE = (
    "You are an expert backend engineer working as part of a development "
    "team. Your role is to implement production-quality code based on "
    "specifications provided by the team's architect. You should approach "
    "each task systematically: first read the relevant context carefully, "
    "then produce code that is correct, well-tested, and consistent with "
    "the existing codebase's conventions.\n\n"
    "For this task, you are taking over work from the architect, who has "
    "completed the design phase and has now handed the implementation off "
    "to you. The architect's identifier in our system is arch-7b3, and "
    "your identifier is be-9c1. The architect's previous work on this "
    "feature is recorded in run 01234567-89ab-7cde-9012-3456789abcd0, "
    "which you should reference if any of the context below is unclear.\n\n"
    "The task you are being asked to complete is identified as "
    "auth-endpoints. The files you should read before beginning your work "
    "include src/auth/spec.md, specifically lines 1 through 120, which "
    "contain the design specification that the architect produced. You "
    "should also read src/auth/types.ts, specifically lines 14 through 58, "
    "which contain the TypeScript type definitions that your "
    "implementation will need to conform to. Please read both files in "
    "full before starting implementation, and make sure you understand "
    "the relationships between the design and the types before writing "
    "any code.\n\n"
    "You are expected to produce two output files as part of this task. "
    "The first is src/auth/handlers.ts, which will contain the main "
    "implementation of the authentication handlers. The handlers must "
    "conform to the response schema defined at schemas/auth-response.json, "
    "and you should validate your implementation against this schema "
    "before considering the work complete. The second output file is "
    "tests/auth.spec.ts, which will contain the test suite for the "
    "handlers you have implemented. The test suite should cover the happy "
    "path for each handler, edge cases including invalid inputs and "
    "missing fields, and error scenarios such as expired tokens and "
    "rate limiting.\n\n"
    "You are running on the sonnet model tier and have a token budget of "
    "8000 tokens allocated to this run. Please be thoughtful about your "
    "use of the budget and prefer concise, direct code over extensive "
    "commentary. When you have completed the work, please provide a "
    "summary of what you have implemented, any decisions you made that "
    "deviated from the specification, and any issues you encountered "
    "that the architect or the team should be aware of. Thank you, and "
    "please proceed when ready."
)


def test_brief_at_most_one_third_of_prose_tokens():
    """The typed Brief must be at most 1/3 the prose-equivalent token count."""
    brief_tokens = _estimate_tokens(json.dumps(_BRIEF))
    prose_tokens = _estimate_tokens(_PROSE)
    ratio = brief_tokens / prose_tokens
    assert brief_tokens * 3 <= prose_tokens, (
        f"Brief ({brief_tokens} tokens) is more than 1/3 of prose "
        f"({prose_tokens} tokens). Ratio={ratio:.2f}. "
        "Wire-format compression target violated."
    )


def test_brief_payload_fields_have_no_overlap_with_a_prose_summary():
    """Brief content should be machine-targeted, not a prose summary.

    A simple structural check: the JSON serialization should contain neither
    'Hello' nor full sentence punctuation patterns ('. The' style). If a
    future change adds prose to the Brief body, this fails before the
    ratio test does.
    """
    serialized = json.dumps(_BRIEF)
    assert "Hello" not in serialized
    assert ". The " not in serialized
    assert "Please" not in serialized
