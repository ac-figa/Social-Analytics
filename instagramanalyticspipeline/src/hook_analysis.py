"""
Analyzes the opening seconds ("the hook") of Instagram videos/Reels
published in the last N days: transcribes the spoken words (Google Cloud
Speech-to-Text) and reads any on-screen title/text (Claude vision) from
that window, so patterns across top performers can actually be studied
instead of guessed at.

Runs as a standalone, idempotent pass over instagram_master -- re-running
it only analyzes posts that don't already have a row in
instagram_hook_analysis (see bigquery_store.get_posts_needing_hook_
analysis()'s docstring), so it's safe to run repeatedly, e.g. as a
routine step after a regular sync, without re-paying for posts already
analyzed. Delete a post's row first if you want it re-analyzed.

Requires:
  - ffmpeg on PATH (video/audio extraction) -- `brew install ffmpeg`.
  - The Speech-to-Text API enabled on your GCP project (uses the same
    Application Default Credentials as BigQuery, no separate API key):
    `gcloud services enable speech.googleapis.com`.
  - ANTHROPIC_API_KEY in .env.

Run:  python -m src.hook_analysis
      python -m src.hook_analysis --days 30 --limit 20   (a first test batch)

A post is skipped (not a failure) when Meta's media_url field is
withheld -- this happens for media Meta considers to have copyrighted
audio, common on Reels using trending sounds. There's no way around
this from the API; those posts just won't have a hook analysis.
"""
import argparse
import base64
import json
import logging
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from google.cloud import speech

from . import bigquery_store, config
from .graph_client import InstagramGraphClient, TokenExpiredError

log = logging.getLogger(__name__)

_CLAUDE_MODEL = "claude-opus-5"

_ON_SCREEN_TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "on_screen_text": {
            "type": "array",
            "description": (
                "Every distinct piece of on-screen text/title/caption overlay visible "
                "across these frames, in the order it appears. Empty list if there's no "
                "on-screen text at all."
            ),
            "items": {"type": "string"},
        }
    },
    "required": ["on_screen_text"],
    "additionalProperties": False,
}

_ON_SCREEN_TEXT_PROMPT = (
    "These are frames sampled from the opening few seconds of a short-form video "
    "(the \"hook\"). Read out any on-screen text -- a title card, caption overlay, "
    "or burned-in subtitles -- exactly as written. Ignore the platform's own UI "
    "(like counts, username, icons) -- only text the creator put in the video itself."
)


def run(since_days: int = 90, limit: int = None) -> int:
    if not config.ANTHROPIC_API_KEY:
        log.error("Fatal: ANTHROPIC_API_KEY is not set (see .env.example).")
        return 1

    graph_client = InstagramGraphClient()
    try:
        account_info = graph_client.get_account_info()
    except TokenExpiredError as e:
        log.error("Fatal: %s", e)
        return 1

    bq_client = bigquery_store.get_client()
    bigquery_store.ensure_schema(bq_client)

    # Scoped to whichever account this run is authenticated as (see
    # ENV_FILE in config.py's docstring for how a second brand's account
    # runs against the same table under different credentials) -- both
    # brands' posts live in the same instagram_master table, so without
    # this every run would pull every account's posts.
    candidates = bigquery_store.get_posts_needing_hook_analysis(bq_client, since_days, account_info["id"])
    if limit:
        candidates = candidates[:limit]
    if not candidates:
        log.info(
            "Nothing to analyze for @%s -- every video in the last %d days already has a hook analysis row.",
            account_info.get("username"), since_days,
        )
        return 0
    log.info(
        "Analyzing the hook of %d video(s) from @%s (window: %.1fs)...",
        len(candidates), account_info.get("username"), config.HOOK_WINDOW_SECONDS,
    )

    media_urls = graph_client.get_media_urls([c["Post_ID"] for c in candidates])

    speech_client = speech.SpeechClient()
    anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    ok = no_media_url = failed = 0
    for c in candidates:
        post_id = c["Post_ID"]
        media_url = media_urls.get(post_id)
        now = datetime.now(timezone.utc)

        if not media_url:
            log.info("%s (%s): no media_url available -- skipping", post_id, c.get("Permalink"))
            bigquery_store.upsert_hook_analysis(
                bq_client,
                {
                    "Post_ID": post_id, "Hook_Window_Seconds": None, "Hook_Transcript": None,
                    "Hook_On_Screen_Text": None, "Analysis_Status": "no_media_url", "Analyzed_At": now,
                },
            )
            no_media_url += 1
            continue

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                video_path = _download_video(media_url, tmpdir)
                audio_path = _extract_hook_audio(video_path, tmpdir, config.HOOK_WINDOW_SECONDS)
                frame_paths = _extract_hook_frames(video_path, tmpdir, config.HOOK_WINDOW_SECONDS)

                transcript = _transcribe(speech_client, audio_path)
                on_screen_text = _read_on_screen_text(anthropic_client, frame_paths)

            bigquery_store.upsert_hook_analysis(
                bq_client,
                {
                    "Post_ID": post_id,
                    "Hook_Window_Seconds": config.HOOK_WINDOW_SECONDS,
                    "Hook_Transcript": transcript,
                    "Hook_On_Screen_Text": " / ".join(on_screen_text) if on_screen_text else None,
                    "Analysis_Status": "ok",
                    "Analyzed_At": now,
                },
            )
            ok += 1
            log.info("%s: analyzed OK", post_id)
        except Exception as e:  # noqa: BLE001 -- one failed post must not abort the whole run
            log.warning("%s (%s): hook analysis failed: %s", post_id, c.get("Permalink"), e)
            bigquery_store.upsert_hook_analysis(
                bq_client,
                {
                    "Post_ID": post_id, "Hook_Window_Seconds": None, "Hook_Transcript": None,
                    "Hook_On_Screen_Text": None, "Analysis_Status": "failed", "Analyzed_At": now,
                },
            )
            failed += 1

    log.info(
        "Hook analysis complete: %d ok, %d skipped (no media_url), %d failed", ok, no_media_url, failed
    )
    return 0


def _download_video(media_url: str, tmpdir: str) -> str:
    path = str(Path(tmpdir) / "video.mp4")
    with requests.get(media_url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return path


def _extract_hook_audio(video_path: str, tmpdir: str, window_seconds: float) -> str:
    """16kHz mono PCM WAV -- the format Google Speech-to-Text's
    synchronous recognize() expects for LINEAR16."""
    path = str(Path(tmpdir) / "hook_audio.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_path, "-t", str(window_seconds),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", path,
        ],
        check=True, capture_output=True,
    )
    return path


def _extract_hook_frames(video_path: str, tmpdir: str, window_seconds: float) -> list:
    """~1 frame per second across the hook window -- enough to catch a
    title card that only appears briefly, without sending an excessive
    number of images to Claude for a ~4-second clip."""
    pattern = str(Path(tmpdir) / "frame_%02d.jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-t", str(window_seconds), "-vf", "fps=1", pattern],
        check=True, capture_output=True,
    )
    return sorted(Path(tmpdir).glob("frame_*.jpg"))


def _transcribe(speech_client: speech.SpeechClient, audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        content = f.read()
    audio = speech.RecognitionAudio(content=content)
    rec_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code=config.HOOK_SPEECH_LANGUAGE,
    )
    response = speech_client.recognize(config=rec_config, audio=audio)
    transcript = " ".join(
        result.alternatives[0].transcript for result in response.results if result.alternatives
    )
    return transcript or None


def _read_on_screen_text(client: anthropic.Anthropic, frame_paths: list) -> list:
    if not frame_paths:
        return []
    content = []
    for p in frame_paths:
        image_b64 = base64.standard_b64encode(p.read_bytes()).decode("utf-8")
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}}
        )
    content.append({"type": "text", "text": _ON_SCREEN_TEXT_PROMPT})

    response = client.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": _ON_SCREEN_TEXT_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        return []
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        log.warning("Unexpected on-screen-text response (not valid JSON): %s", text[:200])
        return []
    return parsed.get("on_screen_text", [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90, help="Only analyze videos published in the last N days.")
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap how many videos to analyze this run (good for a first test)."
    )
    args = parser.parse_args()
    sys.exit(run(since_days=args.days, limit=args.limit))
