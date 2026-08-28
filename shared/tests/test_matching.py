import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.matching import (  # noqa: E402
    AUTO_CONFIRM_SCORE,
    MIN_SUGGEST_SCORE,
    find_candidate_groups,
    pair_score,
)


def item(content_id, platform, caption, day, duration=None):
    return {
        "Content_ID": content_id,
        "Platform": platform,
        "Caption": caption,
        "Publish_Date": datetime(2026, 1, day),
        "Duration": duration,
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

    def test_recurring_caption_far_apart_dates_never_matches(self):
        """Real production bug (Aug 2026): a recurring caption template
        ("It's always the same story", reused across many unrelated
        videos over a year) scored 0.75 on caption alone -- clearing
        MIN_SUGGEST_SCORE -- for a Jul 2026 post against a completely
        unrelated Aug 2025 TikTok video 355 days earlier, with no date or
        duration signal reining it in. Date proximity must be a hard gate,
        not just a weighted factor, for exactly this reason."""
        a = item("ig:1", "Instagram", "It's always the same story \U0001f602\U0001f602", 24)
        far = item(
            "tt:1", "TikTok", "It's always the same story \U0001f602 #funny #dads #fyp", 24
        )
        far["Publish_Date"] = datetime(2025, 8, 3, 19, 57, 20)
        self.assertEqual(pair_score(a, far), 0.0)

    def test_matching_duration_overrides_weak_caption(self):
        """When both sides expose a duration (YouTube/TikTok/Facebook all
        do), it becomes the primary signal -- a near-identical duration on
        the same day should score high even if the caption/title wording
        has almost nothing in common (e.g. a YouTube SEO title vs. a
        TikTok caption for the same upload)."""
        a = item("yt:1", "YouTube", "Best Pizza in Rome? Taste Test", 10, duration=42.0)
        b = item("tt:1", "TikTok", "trying every slice in Rome \U0001f355", 10, duration=42.0)
        self.assertGreaterEqual(pair_score(a, b), AUTO_CONFIRM_SCORE)

    def test_mismatched_duration_blocks_auto_confirm_despite_identical_caption(self):
        """A big duration gap on the same day, same caption, should still
        pull the score down significantly -- guards against two distinct
        videos that happen to share a generic caption/date."""
        a = item("yt:1", "YouTube", "Weekend recap", 10, duration=15.0)
        b = item("tt:1", "TikTok", "Weekend recap", 10, duration=90.0)
        self.assertLess(pair_score(a, b), AUTO_CONFIRM_SCORE)

    def test_strong_date_and_duration_signal_auto_confirms_despite_unrelated_caption(self):
        """Requested directly (Aug 2026): duration within 2 seconds and
        date within 2 days should auto-confirm on its own, without needing
        caption text to agree -- the old weighted formula alone left tight
        duration/date matches with a so-so caption stuck in Pending
        Matches even though duration+date that close is effectively a
        fingerprint (old formula would have scored this ~0.57, below
        AUTO_CONFIRM_SCORE)."""
        a = item("yt:1", "YouTube", "Totally unrelated title about hiking", 10, duration=42.0)
        b = item("tt:1", "TikTok", "A completely different caption about cooking", 11, duration=43.0)
        self.assertEqual(pair_score(a, b), 1.0)

    def test_duration_just_outside_strong_match_falls_back_to_weighted_formula(self):
        """3 seconds apart clears the normal MAX_DURATION_DELTA_SECONDS
        tolerance but not the stricter STRONG_MATCH_MAX_DURATION_DELTA_SECONDS
        bar -- with an unrelated caption, this should NOT auto-confirm."""
        a = item("yt:1", "YouTube", "Totally unrelated title about hiking", 10, duration=42.0)
        b = item("tt:1", "TikTok", "A completely different caption about cooking", 10, duration=45.0)
        self.assertLess(pair_score(a, b), AUTO_CONFIRM_SCORE)

    def test_instagram_missing_duration_falls_back_to_caption(self):
        """Instagram never exposes Duration -- a pair involving it must
        still use the caption-led scheme, not silently score 0 just
        because one side has no duration."""
        a = item("ig:1", "Instagram", "New drop with @caffeborbone this week", 5)
        b = item("yt:1", "YouTube", "New drop with @caffeborbone this week", 5, duration=30.0)
        self.assertGreater(pair_score(a, b), 0.9)


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
