"""
CLI to set/update the manual Partnership + Content_Type classification for
one Video_ID. Writes to tiktok_classifications, which the pipeline reads
and reattaches to TikTok_Master on every refresh.

Usage:
  python -m src.classify --video-id 7676513953853263111 --partnership "Caffe Borbone" --content-type "Sponsored Integration"
"""
import argparse
import sys

from . import bigquery_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", required=True, help="TikTok video ID")
    parser.add_argument("--partnership", required=True, help='e.g. "Caffe Borbone", "Organic"')
    parser.add_argument("--content-type", required=True, help='e.g. "Skit", "Sponsored Integration"')
    parser.add_argument("--by", default="cli", help="Who made this change (for auditing)")
    args = parser.parse_args()

    client = bigquery_store.get_client()
    bigquery_store.ensure_schema(client)
    bigquery_store.upsert_classification(
        client, args.video_id, args.partnership, args.content_type, args.by
    )
    print(f"Classified Video_ID={args.video_id}: Partnership={args.partnership!r}, "
          f"Content_Type={args.content_type!r}")
    print("Run the pipeline again to reattach this to TikTok_Master, or query "
          "tiktok_classifications directly -- it is already the source of truth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
