# Dispatch request — Round 2 research

Continue in the existing visible Hermes session titled “REAPER Workbench — 6 research agents”. Do not create a new chat. Keep the session model set to DeepSeek V4.1 Flash 1017 (deepseek/deepseek-v4.1-flash-1017), reasoning Ultra/Max.

## Assignment

Run exactly six independent research subagents, one for each brief:

1. docs/research/round-2/R2-01-AUDIO-AND-LISTENING-CONTEXT.md
2. docs/research/round-2/R2-02-EVENTS-PERFORMANCE-AND-NOTES.md
3. docs/research/round-2/R2-03-RHYTHM-STRUCTURE-AND-ARRANGEMENT.md
4. docs/research/round-2/R2-04-MIX-PERCEPTION-AND-REFERENCES.md
5. docs/research/round-2/R2-05-CONTEXT-GRAPH-AND-MCP.md
6. docs/research/round-2/R2-06-REAPER-ACTIONS-AND-END-TO-END.md

All six must first read docs/research/round-2/00-COMMON-CONTRACT.md, all six briefs above, all six Round 1 reports in work/research/reports/, docs/research/00-RESEARCH-PROTOCOL.md, then inspect their relevant current repository sources/tests/docs. The briefs are intentionally cross-dependent: each agent must understand what all five other topics need and supply, not optimize its local subproblem in isolation.

## Required orchestration

1. Before dispatch, confirm the existing repository root, branch, HEAD, dirty state and current contents of the R1 reports. Do not assume R1’s repository snapshot is still current.
2. Dispatch exactly six agents in background, mapping one-to-one to the six briefs. Use the Hermes delegation tool’s actual required schema. If the schema/tool fails, inspect its help and retry; do not claim dispatch until Hermes returns a successful delegation ID and count=6.
3. Give each agent the full user objective from the common contract and the shared cross-topic requirements. Each agent must read all briefs and reports before researching.
4. Ask each agent to produce a first explicit input/output contract with its sibling topics, then research. After draft reports exist, have each agent inspect relevant sibling findings, identify conflicts and update its final report. The parent is the integrator; do not spawn a seventh researcher.
5. Keep research bounded by evidence saturation in the common contract. Do not stop after a shallow source list. Use primary docs/papers/code for factual technical claims, include direct URLs and dates, and distinguish fact, inference, and hypothesis.
6. Do not implement code, edit production docs/configuration/projects, launch REAPER, download large weights, or run model inference. CPU inference/offload is forbidden. Do not use the V100 concurrently. If a live REAPER or GPU test is needed to close a decision, specify the exact test and mark it as an owner-approved follow-up; do not execute it.
7. Subagents write only their assigned final reports under work/research/round-2/reports/:
   - R2-01-AUDIO-AND-LISTENING-CONTEXT.md
   - R2-02-EVENTS-PERFORMANCE-AND-NOTES.md
   - R2-03-RHYTHM-STRUCTURE-AND-ARRANGEMENT.md
   - R2-04-MIX-PERCEPTION-AND-REFERENCES.md
   - R2-05-CONTEXT-GRAPH-AND-MCP.md
   - R2-06-REAPER-ACTIONS-AND-END-TO-END.md
   The parent writes one integrated SYNTHESIS.md in the same reports directory only after all six final reports are present.
8. The synthesis must contain the required matrix from the common contract, a coherent entity/edge/time/stage/evidence vocabulary, cross-topic decisions, unresolved owner decisions, experiments that require approval, and dependency-ordered implementation gates. Never hide conflict or mark a hypothesis as proven.

Return immediately after successful background dispatch with the delegation ID, the exact six assignments, and the report paths. Continue monitoring this same research goal until all six reports and the synthesis are complete; report material changes promptly. The final result must state exactly what evidence remains unavailable without a live V100 or REAPER run.
