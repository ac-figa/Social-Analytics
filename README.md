# Social-Analytics

Pulls content performance data from Instagram, Facebook, YouTube, and
TikTok into BigQuery, lets you classify each piece of content by
partnership, and automatically links the same video posted across
platforms so you can pull up-to-date, all-platform numbers for any
partner on demand.

## Architecture

```
instagramanalyticspipeline/   Instagram Reels/posts -> BigQuery (live)
facebookpipeline/              Facebook Page videos/Reels -> BigQuery (live, reuses the Instagram pipeline's Meta token)
youtubepipeline/                YouTube Shorts/videos -> BigQuery (live)
tiktokpipeline/                  TikTok videos -> BigQuery (live, running on Sandbox credentials while the Production app review is pending)
shared/                          Cross-platform matching + partnership reporting layer all pipelines feed into
webapp/                          Local classification dashboard (live) -- shareable partner report pages still planned
```

Each platform pipeline ingests its own detailed data into its own
BigQuery tables (e.g. `instagram_master`), exactly as before -- and
additionally mirrors a normalized subset into `shared`'s `content_items`
table. `shared` is what matches "the same video, posted to Instagram,
YouTube, TikTok, and Facebook" into one group, and is what a partnership's
classification (`Partnership` / `Content_Type`) actually attaches to --
see `shared/README.md` for the full design.

## Getting started

Each pipeline is self-contained -- see its own README for setup:

- `instagramanalyticspipeline/README.md`
- `facebookpipeline/README.md`
- `youtubepipeline/README.md`
- `tiktokpipeline/README.md`

`shared/README.md` explains the cross-platform layer both of the above
feed into.
