# Classification dashboard (local)

A local Flask app for classifying content into partnerships/content types,
reviewing auto-suggested cross-platform matches, and triggering a full
refresh across all four pipelines. Runs entirely on your own machine --
nothing here is deployed anywhere.

## Setup

Uses the same Python environment as the pipelines (it shells out to each
one for the Sync page, so it needs their dependencies too, not just its
own):

```bash
conda activate social-analytics   # or whatever env you run the pipelines in
cd webapp
pip install -r requirements.txt
cp .env.example .env
```

Fill in `BQ_PROJECT_ID` in `.env` (same value as every other pipeline's
`.env`). The dataset names default to what each pipeline already uses --
only change them if you customized a pipeline's own `BQ_DATASET`.

You'll also need `gcloud auth application-default login` done once, same
as the pipelines (see `instagramanalyticspipeline/docs/SETUP.md` if you
haven't).

## Run

```bash
python3 app.py
```

Then open **http://127.0.0.1:5050** in a browser.

## Pages

- **Classify** (`/`) -- the main queue. One row per real-world piece of
  content, showing every platform it's matched across (via the shared
  content layer -- see `shared/README.md`), with a Partnership and
  Content Type field to fill in and save. Filters: unclassified only
  (default) vs. all, and Instagram Collabs only (posts where
  `Instagram_Master.Collabed = TRUE`) -- useful for working through
  brand collaborations specifically. Classifying a group writes the same
  Partnership/Content Type to `content_groups` *and* to every member
  platform's own `*_classifications` table, so each pipeline's own master
  table reflects it too.
- **Pending Matches** (`/pending`) -- auto-suggested matches that scored
  between `MIN_SUGGEST_SCORE` and `AUTO_CONFIRM_SCORE` (see
  `shared/src/matching.py`), shown side by side with what they'd be
  grouped with. Accept or reject each one.
- **Partnerships** (`/partnerships`) -- manage the partnership list and
  each partnership's content types (e.g. Ferrero -> Nutella, B-ready).
  These populate the Classify page's dropdowns. New partnerships/content
  types typed directly on the Classify page are also saved here
  automatically.
- **Sync** (`/sync`) -- runs all four platform pipelines, then
  cross-platform matching, then the Instagram Duration backfill, in the
  background (same as running each command by hand in Terminal -- see
  each pipeline's own README). Takes several minutes; the page polls for
  live log output.

## Running it as a real hosted site instead of locally

See `deploy/README.md` for deploying to Cloud Run behind Google
sign-in (restricted to whichever accounts you list) -- a real `https://`
URL you can open from your phone or any browser, no terminal needed.
Sync is intentionally disabled on that deployment (see that page for
why); classify/browse/pending review all work the same either way, since
both read/write the same BigQuery project.

## Why a separate small app instead of extending a pipeline

None of the four pipelines are servers -- they're one-shot scripts meant
to run on a schedule. This is the one piece of the project that's
inherently interactive (a human classifying content), so it gets its own
process. It reuses `shared/src/content_store.py` directly rather than
duplicating any BigQuery logic.
