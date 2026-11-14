# Hermes dispatch: full-capture throughput research

You are the research coordinator. Read and follow
`docs/PERFORMANCE-PIPELINE-RESEARCH-TZ.md` in full.

Spawn exactly two non-overlapping Hermes subagents:

1. Assign Workstream A only: REAPER render completion, safe file readiness,
   and render/analysis overlap through documented APIs.
2. Assign Workstream B only: whole-pipeline profiling, CPU/process/I/O/archive
   performance, bounded scheduling, and exact-analysis throughput.

Both agents must inspect the current code and measurement artifacts and search
the internet for engineering evidence. Prefer official Cockos/ReaScript sources
for REAPER claims; cite primary or technical sources for general scheduling and
DSP claims. Share the project constraints and the measured evidence from the
brief. Have them exchange only cross-cutting questions, not duplicate research.

This is research/design only. Do not modify production source, tests, project
files, render data, the existing capture archive, or the active performance
run. Write only the two requested agent reports and the Hermes synthesis named
in the brief. Verify that each report separates measured facts, sourced claims,
and inference. The synthesis must identify the fastest complete, correct design
and give a measurable implementation/acceptance sequence; no code changes yet.

User clarification for this active assignment: cover the full optimization
spectrum for every current pipeline stage, not just render/analysis overlap.
Before finishing, reread the added “Required breadth: cover every stage and
every acceleration family” section in the brief and make sure the two existing
workstreams and synthesis include its stage × technique matrix (parallel CPU,
GPU/V100, exact algorithmic/native optimization, latency/I/O/data movement, and
scheduling/back-pressure). Keep the original two-subagent limit: fold this
clarification into the current two reports; do not spawn a third agent. Preserve
the current capture-performance run untouched.

At the end, return the three file paths, major findings, disagreements or
unresolved questions, and the next engineering decision that should be reviewed.
