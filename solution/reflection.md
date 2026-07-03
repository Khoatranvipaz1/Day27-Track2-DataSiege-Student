# Reflection (≤1 page)

Final private run: **score 34.57** — TPR 0.7407, FPR 0.0822, cost_overage 0.0
(single metered call per event, comfortably inside the 320-credit budget).

**Which fault types were hardest to catch, and why?**

The subtle-magnitude faults that sit inside normal variance — exactly the mix the
private phase is weighted toward. Three cases hurt the most:

- **`checks` distribution/volume drift.** Absolute baseline bounds only catch the
  obvious tails, so the real work is the rolling z-score against each stream's own
  recent history (`data_batch_history`). Faults that move a metric 2–3σ but stay
  *inside* the static baseline are the ones I miss when the shift is gradual: the
  rolling mean drifts with the fault and the deviation never crosses the band. My
  remaining false negatives are almost all here.
- **`ai_infra` embedding drift vs. corpus staleness.** These two look alike on a
  single field — a rising `centroid_shift` with fresh docs is drift, a high
  `avg_doc_age_days` with a *quiet* centroid is staleness — so I gate each on the
  other axis to keep them apart. Instances near the age/shift boundary are genuinely
  ambiguous and cost me both a miss and the occasional false alarm.
- **`lineage` runtime anomalies.** Missing-upstream and orphaned outputs are
  structural and reliable to catch; runtime anomalies are pure statistics on a
  short per-job duration history, so early runs (fewer than 4 samples) have no
  baseline to judge against and slip through.

To hold FPR down against all this I added hysteresis: after any alert I set a short
`recent_alerts` debounce so a single sustained fault (which shows up as several
consecutive off-baseline events) fires once instead of once per event, and only a
*strong* re-deviation re-triggers inside that window. That trade cost me a few TPR
points on multi-event faults but kept FPR at 0.08 instead of ballooning.

**What would you change about your cost/coverage tradeoff, if you had another pass?**

I spend exactly one metered call per event and never exceed budget, so I left
headroom on the table — cost_overage is 0.0 and the private budget (320) allows
roughly full single-pass coverage. With another pass I'd spend some of that slack
on *confirmation* calls: on a borderline `checks` or `ai_infra` event that lands in
the 2–3σ grey zone, re-profile or pull a second signal before deciding, instead of
resolving the ambiguity with a fixed threshold. That directly targets the gradual
distribution-shift misses above — the faults I lose aren't ones I lack the budget to
investigate, they're ones I currently decide too early on a single reading. I'd also
warm up the per-job/per-corpus histories faster (seed from baseline) so the
cold-start window stops leaking early subtle faults.
