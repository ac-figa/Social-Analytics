"""
Reads a platform's own in-app audience-demographics screenshot (age,
gender, top locations) via Claude vision and turns it into structured
percentages -- the Media Kit's fallback for platforms whose API exposes
no demographics endpoint at all (TikTok, as of writing -- see
tiktokpipeline/docs/SETUP.md's sibling investigation notes). Only ever
called when config.ANTHROPIC_API_KEY is set; callers are expected to
check that first.

Age buckets and country labels are normalized in the prompt itself (see
_PROMPT) to match the format Meta's own demographics API already
returns for Instagram/Facebook -- bare hyphenated age ranges ("18-24",
not "18-24 years old") and ISO 3166-1 alpha-2 country codes ("US", not
"United States") -- so every account_demographics row in the system
uses one consistent format regardless of source, and the Media Kit
never needs per-platform display logic to paper over the difference.
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
            "description": (
                "One entry per age bucket shown, each with its percentage. Normalize the "
                "label to Meta's own bucket style (no 'years old' suffix, plain hyphenated "
                "range) -- see the prompt for the exact set."
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
        "countries": {
            "type": "array",
            "description": (
                "One entry per named country shown, each with its percentage, as an "
                "ISO 3166-1 alpha-2 country code (e.g. \"US\", \"IT\", \"GB\"), not the "
                "country name. If the chart also shows a catch-all 'Others'/'Other' "
                "rollup row (the remainder not broken out by country), include it too "
                "with the literal label \"Others\" -- needed to preserve the true total "
                "audience share, not just the share among named countries."
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
    "Read the exact numbers off the charts and return them as structured data, "
    "normalized to match how another platform's demographics API (Meta's) already "
    "formats the same kind of data in this system, so every source lines up:\n"
    "- Age buckets: use bare hyphenated ranges only, no \"years old\" suffix and no "
    "extra spacing -- e.g. \"18-24\", \"25-34\", \"35-44\", \"45-54\". If the screenshot's "
    "bucket exactly matches one of Meta's standard buckets (\"13-17\", \"18-24\", "
    "\"25-34\", \"35-44\", \"45-54\", \"55-64\", \"65+\"), use that exact string. If the "
    "platform groups its oldest bucket differently (e.g. a single \"55+\" instead of "
    "splitting 55-64/65+), keep it as the plain range shown (\"55+\") rather than "
    "guessing a split that isn't in the data.\n"
    "- Countries: give the ISO 3166-1 alpha-2 country code (e.g. \"US\", \"IT\", \"GB\", "
    "\"CA\"), not the country name -- read the country name off the chart, then convert "
    "it to its two-letter code.\n"
    "- Gender: use \"Male\", \"Female\", \"Other\" (or whatever categories are actually "
    "shown).\n"
    "If a section isn't present in the screenshot, return an empty list for it."
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
