import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.matching import MIN_SUGGEST_SCORE, find_candidate_groups, pair_score  # noqa: E402


def item(content_id, platform, caption, day):
    return {
        "Content_ID": content_id,
        "Platform": platform,
        "Caption": caption,
        "Publish_Date": datetime(2026, 1, day),
    }


class TestPairScore(unittest.TestCase):
    def test_same_platform_never_matches(self):
        a = item("ig:1", "Instagram", "Check out our new espresso blend!", 1)
        b = item("ig:2", "Instagram", "Check out our new espresso blend!", 1)
        self.assertEqual(pair_score(a, b), 0.0)

    def test_identical_caption_same_day_scores_high(self):
        a = item("ig:1", "Instagram", "New drop with @caffeborbone this week", 5)
        b = item("yt:1", "YouTube", "New drop with @caffeborbone this week", 5)
        self.assertGreater(pair_score(a, b), 0.9)

    def test_unrelated_captions_score_low(self):
        a = item("ig:1", "Instagram", "Morning routine vlog", 1)
        b = item("tt:1", "TikTok", "Recipe for spicy noodles", 1)
        self.assertLess(pair_score(a, b), 0.3)

    def test_far_apart_dates_lose_date_bonus(self):
        a = item("ig:1", "Instagram", "Trying the new latte flavor today", 1)
        b = item("yt:1", "YouTube", "Trying the new latte flavor today", 30)
        near = item("fb:1", "Facebook", "Trying the new latte flavor today", 2)
        self.assertLess(pair_score(a, b), pair_score(a, near))

    def test_reworded_caption_same_video_still_matches(self):
        """Real production data (Aug 2026): same video, captioned
        completely differently per platform, posted 8 minutes apart --
        Jaccard scored this at 0.45 (below MIN_SUGGEST_SCORE), which is
        why _caption_similarity uses an overlap coefficient instead."""
        a = item(
            "ig:1", "Instagram", "There's two types of Italy trip \U0001f90c\U0001f1ee\U0001f1f9", 21
        )
        b = item(
            "tt:1", "TikTok", "Which type of Italy trip do you prefer? \U0001f1ee\U0001f1f9☀️", 21
        )
        self.assertGreaterEqual(pair_score(a, b), MIN_SUGGEST_SCORE)


class TestFindCandidateGroups(unittest.TestCase):
    def test_groups_same_content_across_platforms(self):
        items = [
            item("ig:1", "Instagram", "Unboxing the new Caffe Borbone machine", 10),
            item("yt:1", "YouTube", "Unboxing the new Caffe Borbone machine", 10),
            item("tt:1", "TikTok", "Unboxing the new Caffe Borbone machine", 11),
            item("fb:1", "Facebook", "Completely unrelated organic post", 3),
        ]
        groups = find_candidate_groups(items)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(set(group["content_ids"]), {"ig:1", "yt:1", "tt:1"})
        self.assertTrue(group["auto_confirm"])

    def test_never_groups_two_items_from_same_platform(self):
        items = [
            item("ig:1", "Instagram", "Behind the scenes at the shoot", 1),
            item("ig:2", "Instagram", "Behind the scenes at the shoot", 1),
            item("yt:1", "YouTube", "Behind the scenes at the shoot", 1),
        ]
        groups = find_candidate_groups(items)
        for group in groups:
            platforms = [cid.split(":")[0] for cid in group["content_ids"]]
            self.assertEqual(len(platforms), len(set(platforms)))

    def test_low_confidence_match_not_auto_confirmed(self):
        items = [
            item("ig:1", "Instagram", "Cooking pasta tonight for the family", 1),
            item("yt:1", "YouTube", "Cooking pasta with the kids this evening", 1),
        ]
        groups = find_candidate_groups(items)
        if groups:
            self.assertFalse(groups[0]["auto_confirm"])

    def test_unmatched_items_are_left_out(self):
        items = [
            item("ig:1", "Instagram", "Completely unique caption about hiking", 1),
            item("yt:1", "YouTube", "Totally different topic, baking bread", 20),
        ]
        groups = find_candidate_groups(items)
        self.assertEqual(groups, [])


if __name__ == "__main__":
    unittest.main()
