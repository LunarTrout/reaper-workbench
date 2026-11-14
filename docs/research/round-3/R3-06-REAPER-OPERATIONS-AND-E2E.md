# R3-06 — план реализации операций REAPER, обратимости и E2E

## Владелец направления

Перевести R2-06 в конкретный roadmap для нативных REAPER read/write operations и полного проверяемого цикла. Владелец — безопасная адресация/мутация состояния; совместно с R3-05 отвечает за revisions/transactions, с R3-01 за захват результата, с R3-02/03 за ограничения монтажа/MIDI и с R3-04 за цели проверки микса.

## Кодовый аудит

Прочитать общий контракт, все R2 финалы, все R3 briefs. Проследить src/reaper_audio/reaper/bridge_client.py, handles.py, api_catalog.py, models.py, mcp_server.py, src/reaper_audio/daw/session.py, editing.py, alignment.py/align.py, delivery.py, cycle.py, текущий resident Lua script (в т.ч. незакоммиченные изменения), tests/test_bridge_client.py, test_daw_session.py, test_editing.py, test_alignment.py, test_delivery.py, test_api_catalog.py, test_mcp_server.py и relevant tests. Для каждого action проверить путь от MCP/Python к ReaScript в Lua и обратно.

## Требуемый дизайн

1. Разделить read capabilities, generic discoverable API/Actions и typed operations с гарантиями. Для каждой capability задать version availability, permissions, object scope, preconditions, expected state, side-effect footprint, undo/restore semantics, idempotency, post-readback and evidence update. Readback только о существовании объекта не означает проверку реального изменения.
2. Согласовать project revision/state_rev, stale gates and scoped dependency set с R3-05. GUID address, snapshot-local index, project switch, save-as, undo/redo, external user edits and bridge restart должны быть описаны.
3. Расписать категорию edit primitives: track/routing/FX/automation, item edge/fade/slip/split, source offsets, MIDI, markers/regions, tempo/time signatures, render/capture, save/checkpoint. Не создавать feature-specific “death metal quantize” endpoint. Generic ReaScript remains broad, but arbitrary API call is not automatically safe or reversible.
4. Подтвердить source/master/media lifecycle: only origin + final project copies, no unlimited project generations, named save with collision-safe behavior, no silent overwrite, ephemeral manifest-owned captures, no wildcard deletion, clear cleanup on success/failure, record actual hash/path and restore. Define what “reversible” means per operation and per render; don't label a destructive render reversible without preserved source mapping and proof.
5. Design operation lifecycle prepare → inspect diff → consent/risk policy → apply → readback → capture/analyze → confirm/refute → commit/checkpoint or rollback. Define which risk levels require explicit user confirmation and which can be bundled as one safe transaction, without multiplying low-risk calls.
6. Turn four E2E examples in R2-06 into capability tests (not fixed product pipelines) and show broad task composition: diagnose/change/recheck mix; edit timing while preserving feel; create/edit MIDI with musical context; manage FX/automation/routing and verify. Include no-op, stale context, ambiguity, crash/restart, failed render, duplicate filename, unsupported API version, folder routing and missing audio evidence negative cases.

## Coding plan и acceptance

Backlog items identify exact Lua/Python/MCP code locations, owner, schema change, host/API version, rollback, dependent analyses and tests. Separate tests: pure simulation/unit, bridge contract, isolated test-project live REAPER, user acceptance/human hearing. No tests or live REAPER runs in this round. Define pass/fail/inconclusive gates G0…Gn and map each to R2 issues. Avoid treating CPU-only synthetic tests as musical editing acceptance. Preserve project health when any step fails.

## Передать соседям

R3-05 receives state delta, operation lineage, revision transitions and Verification edge. R3-01 receives exact capture request/stage and must return aligned evidence before a sound-dependent action can be called verified. R3-02/03 define confirmed/reviewed event and hypothesis eligibility plus protected structures. R3-04 provides claims, target evidence and compatible reference windows. Action is blocked or asks for context when required dependency is missing/stale/contested.

