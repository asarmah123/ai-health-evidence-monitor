# Monitor recall / timeliness — 2026-09-14

- log entries: **0** · log start: **empty**
- gold denominator (recent + monitorable): **23**
- excluded as pre-log (published before the log existed): **23**
- scoreable now (published ≥ log start, window elapsed): **0**
- **recall = n/a (no scoreable events yet)** (0 detected / 0)
- pending (event window not yet elapsed): **0**
- **median detection lag: n/a**

**Gold-set composition** (recall reflects coverage of this curated set, NOT a market-wide census):
- by specialty: untagged 20, cross-cutting 8, oncology 2, critical_care 1, mental_health 1, neurology 1
- by event type: payment 9, policy 8, coding 6, regulatory 5, hta 3, coverage 1, clinical 1
- The gold set and brand/code matchers lean toward cross-cutting policy/payment and cardiology, so per-specialty recall is indicative only until the set is broadened — do not read the headline recall as uniform across specialties.

| event | status | lag | matched item (review precision) |
|---|---|---|---|
| KE-echogo-cpt0932t-us | pre-log (excluded) |  |  |
| KE-echogo-apc5743-us | pre-log (excluded) |  |  |
| KE-ecgai-cpt-us | pre-log (excluded) |  |  |
| KE-ecgai-apc5734-us | pre-log (excluded) |  |  |
| KE-alivecor-cpt-us | pre-log (excluded) |  |  |
| KE-alivecor-opps-us | pre-log (excluded) |  |  |
| KE-cpt75577-plaque-us | pre-log (excluded) |  |  |
| KE-plaque-cms-payment-us | pre-log (excluded) |  |  |
| KE-cariheart-fda-us | pre-log (excluded) |  |  |
| KE-plaque-commercial-coverage-us | pre-log (excluded) |  |  |
| KE-annalise-ctb-ntap-us | pre-log (excluded) |  |  |
| KE-bayesian-sepsis-ntap-us | pre-log (excluded) |  |  |
| KE-echogo-nice-euh-uk | pre-log (excluded) |  |  |
| KE-health-tech-investment-act-us | pre-log (excluded) |  |  |
| KE-fda-pccp-final-guidance-us | pre-log (excluded) |  |  |
| KE-rejoyn-fda-mdd-us | pre-log (excluded) |  |  |
| KE-nice-stroke-ai-dg57-uk | pre-log (excluded) |  |  |
| KE-masai-ai-mammography-lancet | pre-log (excluded) |  |  |
| KE-eu-ai-act-in-force | pre-log (excluded) |  |  |
| KE-mhra-ai-airlock-uk | pre-log (excluded) |  |  |
| KE-diga-reform-de | pre-log (excluded) |  |  |
| KE-fda-genai-devices-discussion-us | pre-log (excluded) |  |  |
| KE-nice-listens-ai-uk | pre-log (excluded) |  |  |


_Precision is NOT auto-scored — eyeball each DETECTED row's matched item to confirm it is genuinely about the event. Recall excludes pre-log events (we couldn't have caught them)._