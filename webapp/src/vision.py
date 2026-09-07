"""
Reads a platform's own in-app audience-demographics screenshot (age,
gender, top locations) via Claude vision and turns it into structured
percentages -- the Media Kit's fallback for platforms whose API exposes
no demographics endpoint at all (TikTok, as of writing -- see
tiktokpipeline/docs/SETUP.md's sibling investigation notes). Only ever
called when config.ANTHROPIC_API_KEY is set; callers are expected to
check that first.
"""
import base64
import json
import logging

import anthropic

from . import config

log = logging.getLogger(__name__)

_MODEL = "claude-opus-5"

_SCHEMA = {
    "type": "object",
    "properties": {
        "gender": {
            "type": "array",
            "description": "One entry per gender category shown (e.g. Male, Female, Other), each with its percentage.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "pct": {"type": "number"},
                },
                "required": ["label", "pct"],
                "additionalProperties": False,
            },
        },
        "age": {
            "type": "array",
            "description": "One entry per age bucket shown (e.g. 18-24, 25-34, 55+), each with its percentage.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "pct": {"type": "number"},
                },
                "required": ["label", "pct"],
                "additionalProperties": False,
            },
        },
        "countries": {
            "type": "array",
            "description": (
                "One entry per named country shown, each with its percentage. "
                "Exclude any catch-all 'Others'/'Other' rollup row -- it isn't a real country."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "pct": {"type": "number"},
                },
                "required": ["label", "pct"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["gender", "age", "countries"],
    "additionalProperties": False,
}

_PROMPT = (
    "This is a screenshot of a social platform's audience-demographics analytics "
    "page (Gender, Age, and Locations/Countries breakdowns, each shown as percentages). "
    "Read the exact numbers off the charts and return them as structured data. "
    "Use each label exactly as displayed (e.g. age buckets like \"18-24\" or \"55+\", "
    "country names as shown). Skip any 'Others'/'Other' catch-all rollup row in the "
    "locations list -- only include named countries. If a section isn't present in the "
    "screenshot, return an empty list for it."
)


class VisionParseError(Exception):
    """Raised when Claude can't be reached, or its response doesn't parse
    as the expected demographics shape -- callers should surface this to
    the user rather than silently recording partial/garbage data."""


def parse_demographics_screenshot(image_bytes: bytes, media_type: str) -> dict:
    """Returns {"gender": [{"label", "pct"}, ...], "age": [...], "countries": [...]}
    read off one screenshot. Raises VisionParseError on any failure (bad
    image, API error, unparseable response) -- there is no partial-credit
    path here, since a wrong number silently stored is worse than an
    upload that visibly failed."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                        },
                        {"type": "text", "text": _PROMPT},
                    ],
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except anthropic.APIError as e:
        log.warning("Demographics screenshot parse failed (API error): %s", e)
        raise VisionParseError(f"Couldn't reach Claude to read the screenshot: {e}") from e

    if response.stop_reason == "refusal":
        raise VisionParseError("Claude declined to read this screenshot.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise VisionParseError("Claude's response didn't include any data.")

    try:
        parsed = json.loads(text)
    except ValueError as e:
        log.warning("Demographics screenshot parse failed (bad JSON): %s", text[:500])
        raise VisionParseError("Claude's response wasn't valid data.") from e

    if not (parsed.get("gender") or parsed.get("age") or parsed.get("countries")):
        raise VisionParseError("Didn't find any demographics data in that screenshot.")

    return parsed
