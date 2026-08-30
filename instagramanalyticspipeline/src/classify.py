"""
CLI to set/update the manual Partnership + Content_Type classification for
one Post_ID. Writes to instagram_classifications, which the pipeline reads
and reattaches to Instagram_Master on every refresh -- it never touches
Instagram_Master directly.

Usage:
  python -m src.classify --post-id 1789xxxxxxxx --partnership "Caffe Borbone" --content-type "Sponsored Integration"
  python -m src.classify --post-id 1789xxxxxxxx --partnership Organic --content-type Skit --by "jane@company.com"
"""
import argparse
import sys

from . import bigquery_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post-id", required=True, help="Instagram media ID (Post_ID)")
    parser.add_argument("--partnership", required=True, help='e.g. "Caffe Borbone", "Organic"')
    parser.add_argument("--content-type", required=True, help='e.g. "Skit", "Sponsored Integration"')
    parser.add_argument("--by", default="cli", help="Who made this change (for auditing)")
    args = parser.parse_args()

    client = bigquery_store.get_client()
    bigquery_store.ensure_schema(client)
    bigquery_store.upsert_classification(
        client, args.post_id, args.partnership, args.content_type, args.by
    )
    print(f"Classified Post_ID={args.post_id}: Partnership={args.partnership!r}, "
          f"Content_Type={args.content_type!r}")
    print("Run the pipeline again to reattach this to Instagram_Master, or query "
          "instagram_classifications directly -- it is already the source of truth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
