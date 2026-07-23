# Contributing

Corrections and suggestions are welcome — especially on the data.

## Corrections to the clearance → coverage taxonomy or dataset
The definitions in [TAXONOMY.md](TAXONOMY.md) are meant to be argued with. If you think a status
mapping is wrong (e.g. how a particular reimbursement mechanism should be classified), or you have
a **sourced** correction to a date, open an issue with the primary source. Dates are never
estimated — an issue without a verifiable source can't be actioned.

## Adding or fixing a source
Sources are configured declaratively. If a feed has moved or a new body starts publishing a
machine-readable feed, open an issue with the URL and which stage/region it belongs to.

## Bug reports
Open an issue describing what you saw and, if it's a rendering problem, your browser. The site is
a single static page with no backend, so most issues are either a source that changed its format
or a classification rule that needs refining.

## Maintainer routine — weekly spot-check
Automated classification can drift silently, so the dataset is audited by hand on a cadence, not
assumed correct. Once a week:

1. **Read the build health.** Open the latest GitHub Actions run → **Summary**. It lists sources
   contributing, any that went silent or errored, and the undated count. Anything under
   *"silent, steady (possible breakage)"* is the first thing to check — a source that quietly
   stopped returning items usually means a moved feed or a changed page structure.
2. **Sample five recent items** against their primary source. Pick five entries from the last few
   days (e.g. 2 FDA/openFDA authorisations, 1 NICE or CMS item, 1 trial, 1 HEOR paper) and open
   each link. Confirm: the **date** matches the source, the **stage/layer** is right, and the
   **country/body** attribution is correct.
3. **Log what you find.** If a rule mis-classifies, note the source and the wrong→right mapping and
   fix the rule (or open an issue). If a date can't be verified, it should read *"date unknown"* —
   never a guessed date.

Per-source health is also recorded in the private `history.json` each run, so a source that reads
zero for several days running (real breakage) is distinguishable from a one-day blip.

## Scope
This is a focused tool for AI in clinical evidence, regulation, HTA and reimbursement. Suggestions
that broaden it beyond that scope may be declined to keep the signal high.
