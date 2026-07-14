# Model: EAGZ-1 (Entropy-Adaptive Grid Zoning, v1)

## 0. Naming & Versioning

This document specifies model **EAGZ-1** — *Entropy-Adaptive Grid Zoning, version 1*.

Since several variants will likely be tested (different thresholds, windows, weighting schemes), use this naming convention for future iterations so they stay comparable and traceable in logs/experiments:

```
EAGZ-<major>.<minor>[-suffix]

EAGZ-1        this spec, baseline parameters
EAGZ-1.1      same structure, retuned parameters (e.g. different entropy_threshold)
EAGZ-2        structural change (e.g. different sizing formula, different clustering step)
EAGZ-1-nodecay  same as EAGZ-1 but with temporal weighting (τ) disabled, for A/B testing
```

Every candidate zone the pipeline outputs should be tagged with the model name + version + the exact parameter set used to produce it (see §7 config table), so results can be attributed correctly when comparing variants in production.

---

## 1. Purpose

This model determines **which geographic zones are eligible for gameplay** and **how the 10×8 grid is sized and positioned**, for a game where **each round lasts 60 seconds**.

The round duration is the single most important constraint on this model and changes several design decisions from a naive version of this idea:

- **60 seconds is too short a window to reliably measure spatial distribution.** A handful of strikes in one minute isn't statistically enough to compute a meaningful entropy value or detect concentration reliably.
- **But the grid must still be sized/calibrated for the actual 60-second round**, not for whatever longer window is used to gather statistics.

EAGZ-1 solves this by explicitly separating two time windows:

| Window | Purpose | Typical length |
|---|---|---|
| **Observation window** (`W_obs`) | Gather enough strikes to compute stable statistics (entropy, density, centroid) | 8–12 min (trailing, rolling) |
| **Round window** (`W_round`) | The actual gameplay round players bet on | 60 sec (fixed, given) |

The model reads recent history over `W_obs` to characterize *where storms currently are and how concentrated they are*, but sizes grid cells and sets density targets so that outcomes are appropriately uncertain **within a single 60-second round**, not within `W_obs`.

---

## 2. High-Level Pipeline

```
Raw strike stream (continuous ingestion)
      │
      ▼
[Stage 1] Coarse spatial binning (continuous)     → shortlist of active regions
      │
      ▼
[Stage 2] Adaptive grid sizing (scaled to W_round) → per-candidate grid definition
      │
      ▼
[Stage 3] Dispersion validation (entropy on W_obs) → accept/reject zone
      │
      ▼
Playable zone(s) published — refreshed once per round cycle (~every 60s)
```

Stage 1 runs continuously as strikes arrive. Stages 2–3 are recomputed **once per round** (or every N rounds — see §6), not continuously, since the game only needs a fresh zone decision at round boundaries.

---

## 3. Stage 1 — Coarse Spatial Pre-Filtering

Unchanged in principle from the general approach, but the activity floor and window need to reflect the fast game cadence.

### 3.1 Binning method

- Geohash precision 4–5 (≈20–39 km cells), or equivalent fixed grid.
- Maintain rolling in-memory buckets: `geohash_coarse → { count, last_timestamp, strike_ids[] }`, keyed on **`W_obs`**, not `W_round` (the coarse activity detector needs the longer window to avoid flapping between "active"/"inactive" every minute).

### 3.2 Shortlist extraction

- Extract buckets with `strike_count ≥ min_region_activity` over `W_obs`.
- Merge adjacent active buckets into candidate regions.
- This shortlist is recomputed at the same cadence as Stage 2/3 (once per round), or slightly less often if candidate regions are stable (see §6.1).

---

## 4. Stage 2 — Adaptive Grid Sizing (Zoom Calibration for a 60s Round)

**This is the stage most affected by the 1-minute round constraint.** Sizing must target the strike density expected *during the round itself*, not during `W_obs`.

### 4.1 Target density (round-scoped)

```
λ_target = expected strikes per cell DURING THE 60-SECOND ROUND
         ≈ 0.4 – 1.0   (tunable; keep below 1.0 — with only 60s of exposure,
                         cells should mostly have a real but non-trivial chance
                         of being hit, not a near-guarantee)
```

### 4.2 Rate estimation and scaling

1. Compute the region's strike rate over the observation window:

   ```
   rate_per_km2_per_sec = total_strikes_in_region(W_obs) / (region_area_km2 × W_obs_seconds)
   ```

2. Convert to an expected count for a single 60-second round at a given cell size `s`:

   ```
   expected_strikes_per_cell_per_round = rate_per_km2_per_sec × (s × s) × W_round_seconds
   ```

3. Solve for cell edge `s` such that this equals `λ_target`:

   ```
   s = sqrt( λ_target / (rate_per_km2_per_sec × W_round_seconds) )
   ```

   This is the key correction versus a naive model: **without dividing by `W_round_seconds`, cell sizing would be calibrated for the wrong exposure time** (e.g. sized as if the round lasted `W_obs` minutes), producing cells that are far too small/optimistic relative to what a player can actually expect to happen in 60 seconds.

4. Clamp `s` between `cell_size_min` and `cell_size_max` (see §7).
5. Build the 10×8 grid centered on the (temporally-weighted) strike centroid.

### 4.3 Stationarity caveat

This scaling assumes the strike rate is roughly stationary between `W_obs` and the upcoming `W_round` — reasonable for a rolling window of 8–12 min feeding a 60s round, but breaks down if a storm is rapidly intensifying or dissipating. See §8 for mitigation (rate trend adjustment).

---

## 5. Stage 3 — Dispersion Validation (Entropy on `W_obs`)

Entropy is still computed on the longer `W_obs` window — a single round's worth of strikes (60s) is too sparse to trust for this calculation. The model assumes that a region's *spatial concentration pattern* is more stable over minutes than its literal strike timing, which is a reasonable assumption for storm cell geometry.

### 5.1 Strike-to-cell assignment

Using the grid from Stage 2, assign all strikes from `W_obs` (not just the upcoming round) to cells: `n₁, ..., n₈₀`, `total = Σnᵢ`.

### 5.2 Normalized Shannon entropy

```
pᵢ = nᵢ / total
H  = -Σ pᵢ · log(pᵢ)
H_norm = H / log(80)        → range [0, 1]
```

### 5.3 Temporal weighting (recommended)

Because `W_obs` (8–12 min) is much longer than `W_round` (60s), recency weighting matters more here than in a slower-paced game — a storm can meaningfully shift position within `W_obs`, and you want the entropy/centroid to reflect where it is *now*, not its average position over the last 10 minutes.

```
weight(strike_k) = exp(-Δt_k / τ),  τ ≈ 2–4 min   (shorter than previously suggested,
                                                     to track fast storm movement relative
                                                     to the short round length)
nᵢ = Σ weight(strike_k) for strikes k in cell i
```

### 5.4 Acceptance rule

```
Zone is PLAYABLE if:
  1. round_strikes ≥ min_round_strikes  (>=10 strikes falling IN THE GRID over the
                                          last W_round=60s — counted geographically,
                                          so a grid straddling a border counts
                                          strikes from BOTH countries, not a
                                          per-country count)
  2. total ≥ min_total_strikes          (weighted activity floor over W_obs)
  3. H_norm ≥ entropy_threshold          (dispersion floor)
```

Rationale for (1): eligibility must be a property of the *grid*, not of a
country, because a grid can sit across a national border. Counting per country
(as the legacy CAFG-0 model did) undercounts such grids.

---

## 6. Cadence — Aligning the Pipeline with 60-Second Rounds

### 6.1 Recompute frequency

- **Stage 1** (coarse binning): continuous, streaming, negligible cost.
- **Stage 1 shortlist extraction, Stage 2, Stage 3**: recompute **once per round**, i.e. every 60 seconds, timed to complete *before* the next round's betting window opens.
- If pipeline latency is a concern, decouple slightly: compute the zone for round N+1 during round N (using the freshest available `W_obs` data at trigger time), so the result is ready when round N ends. This requires the pipeline (Stages 1–3) to run comfortably under 60 seconds — with the shortlist from Stage 1 kept small, this should be feasible (Stage 3's entropy calc is O(candidates × 80), trivially fast).

### 6.2 Window relationship

```
W_round = 60s                      (fixed, given)
W_obs   = 8–12 min (480–720s)       (≈ 8-12× W_round; tune based on how much data
                                      is needed for stable entropy — see §9 for
                                      how to validate this empirically)
```

---

## 7. Parameters (EAGZ-1 baseline)

| Parameter | Description | Baseline value |
|---|---|---|
| `W_round` | Game round duration (fixed) | 60 sec |
| `W_obs` | Observation window for statistics | 600 sec (10 min) |
| `coarse_geohash_precision` | Stage 1 bucket size | precision 4–5 |
| `min_region_activity` | Min strikes in coarse bucket (over `W_obs`) to become a candidate | 5 strikes |
| `recompute_interval` | Pipeline recompute cadence | 60 sec (once per round) |
| `λ_target` | Target expected strikes/cell **during the 60s round** | 0.4–1.0 |
| `cell_size_min` / `cell_size_max` | Clamp bounds for adaptive cell size | 200m / 10km |
| `min_total_strikes` | Weighted activity floor (over `W_obs`) | tune empirically, start low |
| `min_round_strikes` | Raw strikes in the grid over the last `W_round` (per-grid gate) | 10 |
| `entropy_threshold` | Minimum `H_norm` to accept a zone | 0.5 (start conservative) |
| `τ` | Recency decay constant for entropy weighting | 2–4 min |

Log every zone decision with the exact parameter values used, tagged with the model version (§0), to support later comparison across EAGZ variants.

---

## 8. Edge Cases & Operational Notes (60s-round specific)

- **Rate instability between `W_obs` and `W_round`**: if a storm is rapidly intensifying, the `W_obs`-derived rate underestimates the actual rate for the upcoming round (and vice versa for dissipating storms). Optional refinement: compute the rate over the last 2 recent sub-windows within `W_obs` (e.g. last 2 min vs. prior 8 min) and apply a simple trend adjustment (e.g. weight the more recent sub-window more heavily) before Stage 2's rate estimate. Flag as a candidate improvement for EAGZ-1.1 rather than a blocker for the first version.
- **No candidate regions found for the next round**: with only 60s rounds, "no playable zone" states will be visible to players quickly and repeatedly if this happens often — make sure the fallback behavior (e.g. reuse last valid zone, widen `W_obs`, or show a clear "no active storms" state) is decided as a product behavior, not left as an implicit default.
- **Grid recompute lag**: since a new zone should ideally be ready before each round starts, monitor pipeline execution time explicitly; if it approaches a meaningful fraction of the 60s budget, consider caching Stage 1's shortlist across multiple rounds (recompute shortlist every 2–3 rounds instead of every round) while still recomputing Stage 2/3 every round.
- **Entropy computed on a longer window than the round itself**: by design (see §1), this is intentional — but document it clearly for anyone debugging "why did a low-strike round still validate a zone." It's because the zone's *shape* was judged on `W_obs`, not on the 60s of actual play.
- **Multi-modal regions**: unchanged from general case — split via simple density clustering before Stage 2 if a coarse region contains two distinct storm clusters.

---

## 9. Suggested Validation Approach (for comparing EAGZ variants)

Since you'll be testing several models/parameter sets, track at minimum, per round:

- Model name + version + parameter snapshot used
- `H_norm` of the accepted zone
- `total_strikes` over `W_obs` and actual strikes landed during `W_round`
- Which cell(s), if any, actually got hit during the round
- Distribution of cell win rates over many rounds, aggregated per model version — this is the actual metric you care about: **no single cell (or small subset of cells) should dominate win rate across rounds** for a given model version to be considered successful.

---

## 10. Reference Pseudocode

```python
def run_eagz1_pipeline(strike_store, config, now):
    W_obs = config.W_obs_seconds
    W_round = config.W_round_seconds  # 60

    # Stage 1
    recent = strike_store.get_strikes(since=now - W_obs)
    coarse_buckets = bin_by_geohash(recent, config.coarse_geohash_precision)
    active = [b for b in coarse_buckets if b.count >= config.min_region_activity]
    candidate_regions = merge_adjacent(active)

    playable_zones = []
    for region in candidate_regions:
        for cluster in split_if_multimodal(region.strikes):

            # Stage 2 — round-scoped sizing
            rate = len(cluster.strikes) / (cluster.area_km2 * W_obs)
            cell_size = clamp(
                sqrt(config.lambda_target / (rate * W_round)),
                config.cell_size_min, config.cell_size_max
            )
            grid = build_grid(
                center=weighted_centroid(cluster.strikes, tau=config.tau),
                cell_size=cell_size, rows=8, cols=10
            )

            # Stage 3 — entropy on W_obs
            counts = assign_to_cells(cluster.strikes, grid, tau=config.tau)
            total = sum(counts)
            h_norm = normalized_entropy(counts, total)

            if total >= config.min_total_strikes and h_norm >= config.entropy_threshold:
                playable_zones.append({
                    "grid": grid,
                    "model": "EAGZ-1",
                    "params": config.snapshot(),
                    "h_norm": h_norm,
                    "total_strikes_obs": total,
                })

    return playable_zones
```

---

## 11. Summary

| Stage | Window used | Purpose |
|---|---|---|
| 1. Coarse binning | `W_obs` (rolling) | Find active regions cheaply |
| 2. Adaptive zoom | `W_obs` for rate, scaled to `W_round` | Size grid cells for a realistic 60s outcome distribution |
| 3. Entropy validation | `W_obs` | Reject over-concentrated zones using stable statistics |

Model **EAGZ-1** separates "how we learn where storms are" (`W_obs`, ~10 min) from "what the player actually experiences" (`W_round`, 60s), and explicitly scales the density target so grid cells reflect real 60-second odds rather than the odds implied by a longer lookback window.

---

## Implementation notes (this codebase)

- Pure algorithm: `lightning/eagz.py` (`run_eagz1`, `best_zone`, `Eagz1Config`, `Strike`). No Django/DB imports.
- Stage 1 uses an **equivalent fixed grid** (`coarse_deg`, default 0.3° ≈ 33 km) rather than a geohash library — §3.1 explicitly permits this and it makes adjacency-merge trivial.
- Grid geometry is defined **equirectangularly** (linear lat/lon within the zone bbox), which is the canonical mapping the server scores on and the client must render/hit-test against, so displayed score == authoritative score.
- Baseline params here set `W_round = 60`, `λ_target = 0.7`, `min_total_strikes = 8`, `τ = 180 s`. Tune per §7/§9.
