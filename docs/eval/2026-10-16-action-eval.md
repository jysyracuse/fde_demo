# Action decision eval: rules vs DeepSeek vs Jev vs Claude (2026-10-16)

Question: given one overdue customer's evidence and `PLAYBOOK.md`, which follow-up action?
Set `full` (100 customers, 58 overdue). Offline: `scripts/eval_actions.py data/eval/full`;
raw output `data/eval/full/results.json`, blind judge `data/eval/full/claude.json`.

## Setup
- Same case packet for every arm: billing, CRM, replies, usage, plus code-computed facts (days
  overdue, late-payment count, card status, promise date passed for CRM tasks, usage %).
- Same playbook text; Jev option descriptions are parsed from it (one pass, not tuned).
- rules = layer 1 with CRM linked by truth and keyword reply intent. deepseek = one call,
  temperature 0. jev.choice = one Choice over 8 actions. jev.checks = one Noul per check, code
  applies the playbook order (first >= 0.5 wins). claude = judged from `cases.json` before any
  model ran, without labels (but with knowledge of the generator, see caveats).

## Results
| arm | vs label | vs label corrected (CUS-01091) | needs_human | median latency |
|---|---|---|---|---|
| rules | 57/58 | 58/58 | 0 | ~0 |
| claude | 57/58 | 58/58 | 0 | — |
| jev.choice | 55/58 | 56/58 | 0 | 210 ms |
| deepseek | 53/58 | 54/58 | 2 | 1,222 ms (87k tokens total) |
| jev.checks | 52/58 | 53/58 | 0 | (same call as jev.choice) |

Every arm is 8/8 on dispute, renewal, churn and chronic-late. All errors sit in four places:

| case | what is there | who erred |
|---|---|---|
| CUS-01091 | standard_overdue label, but 4 late payments in 12 months = playbook threshold | **the label** (all five arms say escalate) |
| CUS-01055 | unapplied payment, same amount, reference names another customer's invoice | jev.choice (0.91), jev.checks; deepseek needs_human |
| CUS-01083 | unapplied payment, same amount, reference names another company | jev.choice (0.94), jev.checks |
| CUS-01094 | open "Add 10 users" opportunity (upgrade, not renewal) | deepseek, jev.checks (renewal 0.66) |
| CUS-01040 | reply promises 22 Oct; as_of 15 Oct | deepseek: "promised date has passed" (date error) |
| CUS-01050 | reply "will pay Friday" | deepseek needs_human (read it as a possible chronic-late signal) |
| CUS-01017, 01026 | "AP shows it settled, could you confirm?" | jev.checks: dispute 0.52-0.53 beat payment_claimed 0.25 |

## Findings
1. **Label error found by the eval.** CUS-01091 is a generator artefact (standard_overdue drew 4
   late payments at random). Labels need auditing too; fix in the generator (cap late payments
   for non-chronic categories).
2. **The hard cases are decoys, not text.** Same-amount payments with someone else's reference
   and upgrade-vs-renewal opportunities. Rules get them right because they check the field
   (reference contains the invoice number, opportunity Type). Both LLMs fall for surface
   similarity.
3. **Jev is confidently wrong on decoys** (0.91, 0.94). Confidence gating would not have caught
   them, so calibration needs its own check on harder data before any auto-action.
4. **DeepSeek compared dates wrongly** (22 Oct "passed" on 15 Oct) where the packet left a date
   uncomputed (reply promises). Every date the decision needs must be compared in code.
5. **Decomposition hurt here** (jev.checks < jev.choice). With "first match wins", a false
   positive on an early check (dispute) overrides everything after it; the dispute and
   payment_claimed descriptions overlap on "please confirm" wording.
6. **Rules win, but the set favours them.** The generator and the rules were designed together,
   so every signal has a clean structured form. This does not show rules beat models on real data.

## What this supports
- Keep the current split: code owns structured checks (references, opportunity type, counts,
  dates); a model reads only reply text (intent, promise date), validated.
- Candidate use of Jev: reply intent and quote/label consistency (counterexample 3B), where the
  input is short text and the options are few; tighten descriptions with "not this" cases.

## Caveats
n = 58, one run, synthetic English data, most cases easy. Claude's judging is not independent:
it knows how the generator builds each category. Jev = `jev-latest` (1.13). Playbook written once,
not tuned for either model. Next: holdout set with new wording and counter-cases (see the
external review, finding 2 and 5), and a second Jev run with reordered options (Jev jaggedness 8).

## Holdout (2026-10-17): 40 cases written by Pi (GPT-6.1-sol)
`data/eval/holdout/` (cases.json, truth.json with Pi's rationale, claude.json, results.json).
Pi wrote packets and labels from `PLAYBOOK.md` + packet format + 2 examples, in 4 batches:
near misses, boundaries, precedence conflicts, missing/contradictory data (needs_human) with new
wording (ESL, forwarded mail, withdrawn requests). `check_packets`: 0 problems. Claude judged
blind (labels unread) before any model ran. `arm_rules_packet` was frozen before the holdout;
one fix after it: `StageName: null` crashed it (`or ""`), recorded as a finding.

| arm | vs Pi label | needs_human given | median latency |
|---|---|---|---|
| claude | 40/40 | 5/5 | — |
| deepseek | 33/40 | 5/5 | 1,150 ms (59k tokens) |
| jev.checks | 32/40 | 0/5 | 237 ms |
| rules_pkt | 25/40 | 0/5 | ~0 |
| jev.choice | 19/40 | 0/5 | (same call) |

No label was overturned: every disagreement was checked against Pi's rationale and the playbook.
Debatable: H033 (failure code lost) and H034 (same-amount payment, reference null) could be
read as "check not matched" -> standard_reminder; the playbook's "missing something the
decision depends on" supports needs_human.

Findings
1. **Full-set ranking reverses on the holdout.** rules 57/58 -> 25/40, jev.choice 55/58 -> 19/40.
   The full set rewarded whoever shares the generator's assumptions.
2. **Keyword reply intent breaks on new wording**: "querying the seat line" missed (H005, H036),
   "Not paid yet" read as paid (H010), withdrawn cancellations still read as cancel (H004, H014,
   H018, H020; review finding 4.2). Reply text is where a model earns its place.
3. **Only DeepSeek and Claude say needs_human** (5/5). Rules and Jev have no path for a missing
   field; rules_pkt treats a missing case status as open (H031 -> dispute).
4. **DeepSeek misses precedence**: open renewal + cancellation -> retention (H007, H016, H023,
   H038); usage 59 + expired card -> gentle (H008); chronic over a referenced payment (H027).
5. **jev.choice ignores "first match wins"**: one 8-way choice picks the most salient signal
   (expired card -> gentle 9x, cancel text -> retention). Wrong picks: median confidence 0.58,
   6/21 at >= 0.8; right picks median 0.97. jev.checks (code applies the order) recovers to 32.
6. **Same split, now with evidence**: code owns order and structured fields; a model reads reply
   text; missing data routes to a human. Candidate design: jev.checks for structured booleans +
   DeepSeek (or Jev Noul) for reply intent + an explicit "field missing" check in code.

Caveats: n = 40, one run, labels by one model; Claude's agreement is not independent (Claude
wrote the batch briefs naming the traps). Pi run 1 hung 2 h: `pi -p` waits for stdin EOF in a
background job; fixed with `< /dev/null`.
