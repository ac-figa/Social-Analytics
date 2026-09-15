"""
Classifies each analyzed hook's on-screen title text into a style/format
category (Claude, text-only -- this just labels the Hook_On_Screen_Text
already sitting in BigQuery, no video re-processing needed).

Deliberately split from db.py's aggregation: this module only ever
returns a categorical label per hook. The actual "which style performs
better" comparison (avg views/likes per style) is computed in
db.get_hook_pattern_analysis() straight from real BigQuery numbers, never
asked of the model -- see vision.py's docstring for the percentage-
inflation bug this session already hit once from trusting a model with
arithmetic on real numbers.
"""
import json
import logging

import anthropic

from . import config

log = logging.getLogger(__name__)

_MODEL = "claude-opus-5"

# Ordered roughly from most to least common in short-form hooks -- not
# load-bearing, just keeps the prompt's list readable.
HOOK_STYLES = {
    "question": "Question",
    "comparison": "Comparison / VS",
    "bold_claim": "Bold Claim",
    "relatable_scenario": "Relatable Scenario / POV",
    "list_or_ranking": "List or Ranking",
    "direct_address": "Direct Address",
    "other": "Other",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "description": "One entry per hook, in the same order given.",
            "items": {
                "type": "object",
                "properties": {
                    "post_id": {"type": "string"},
                    "style": {"type": "string", "enum": list(HOOK_STYLES.keys())},
                },
                "required": ["post_id", "style"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}

_PROMPT_HEADER = (
    "Classify each of these short-form video hooks -- the on-screen title "
    "text shown in the opening seconds -- into exactly one style from this "
    "fixed vocabulary:\n"
    "- question: poses a question to the viewer\n"
    "- comparison: compares two or more things (e.g. \"X vs Y\")\n"
    "- bold_claim: a strong, surprising, or superlative statement\n"
    "- relatable_scenario: sets up a relatable situation or POV\n"
    "- list_or_ranking: a numbered list, ranking, or \"top N\"\n"
    "- direct_address: speaks straight at the viewer with an instruction or "
    "callout (e.g. \"Stop doing X\", \"Watch this before...\")\n"
    "- other: doesn't clearly fit any of the above\n\n"
    "Return one classification per hook listed, using its exact post_id.\n\n"
    "Hooks:\n"
)


def classify_hook_styles(hooks: list) -> dict:
    """hooks: [{"Post_ID": ..., "Hook_On_Screen_Text": ...}, ...] -- only
    pass ones with real on-screen text. Returns {Post_ID: style_key},
    missing any hook Claude didn't return a usable classification for."""
    if not hooks or not config.ANTHROPIC_API_KEY:
        return {}

    lines = [f'{h["Post_ID"]}: "{h["Hook_On_Screen_Text"]}"' for h in hooks]
    prompt = _PROMPT_HEADER + "\n".join(lines)

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except anthropic.APIError as e:
        log.warning("Hook style classification failed (API error): %s", e)
        return {}

    if response.stop_reason == "refusal":
        return {}

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except ValueError:
        log.warning("Unexpected hook-style response (not valid JSON): %s", text[:200])
        return {}

    valid_ids = {h["Post_ID"] for h in hooks}
    return {
        c["post_id"]: c["style"]
        for c in parsed.get("classifications", [])
        if c.get("post_id") in valid_ids and c.get("style") in HOOK_STYLES
    }
