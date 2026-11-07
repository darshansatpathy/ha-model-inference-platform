# 2. Promote or roll back on measured comparison, not on a timer

**Status:** accepted

## Context

A canary that is promoted after a fixed wait is promoted whether or not anyone looked at it. A
canary that is judged on too few requests is judged on noise.

## Decision

At each step the controller compares canary and stable over the step's window: error rate and p95
latency, each against an absolute ceiling and relative to stable. A step with fewer than
`min_requests` canary requests is extended (up to `max_extensions`); if it never gets enough
traffic, the canary is rolled back. The first failed step rolls back to 0% immediately.

## Consequences

- Promotion means every step had enough evidence and passed.
- Thresholds are explicit in `Policy` and printed in every drill report.
- At 100% there is no stable traffic to compare with, so the stable figures of the previous step
  serve as the baseline.
- The drills prove the policy catches a 3% error rate and a 150 ms slowdown at the first step.
