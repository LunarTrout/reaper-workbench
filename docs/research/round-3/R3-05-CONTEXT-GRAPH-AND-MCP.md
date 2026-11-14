# R3-05 — план реализации общего контекста и интерфейса модели

## Владелец направления

Перевести R2-05 в code-ready shared contract: единый граф смысла/происхождения, эффективное получение контекста и интерфейс discovery для полноценной работы LLM. Это интеграционная область: прочитать все шесть R2 финалов, все R3 briefs и code map всех пяти соседей; не навязывать им непроверенную музыкальную семантику.

## Код/документы для обязательного аудита

Исследовать src/reaper_audio/models.py, analysis/bundle.py, analysis/artifacts.py, analysis/timeline_context.py, analysis/edit_map_report.py, corpus/model.py, reaper handles/revision, mcp_server.py, api_catalog.py/json; Lua bridge state/context; MCP readme/architecture/safety/validation and tests/test_mcp_server.py, tests/test_analysis_bundle.py, tests/test_artifacts.py, project/API catalog tests. Установить текущие модели, сериализацию, source of truth для tools/list и actual resource templates. Зафиксировать расхождение 12 vs 19 MCP tools, затем отличить существующие schemas от R2-05 proposal 14/10.

## Требуемый дизайн

1. Согласовать entities/typed relations, canonical identifiers and revision. Включить Project, Track/Bus/Master, Item/Take/Source/FX/Send/Envelope/MIDI/Marker, TimeMap/Section/Occurrence/Gesture, AudioEvidence/Measurement/ListenerResult, Claim/Hypothesis/Intent, Operation/StateDelta/Verification. Обосновать persist vs derive-on-demand; relational ledger + typed edges кандидат из R2, не догма.
2. Определить event/evidence/action schemas, lifecycle, versioning, provenance, confidence, alternatives and scoped dependency invalidation. Любой claim navigates to exact evidence; cause is Hypothesis unless experimentally established; URI, role label, MIDI object or model sentence alone is not audio proof.
3. Спроектировать context assembly: compact manifest overview, task-oriented views with progressive disclosure, exact pagination/coverage, relevance and expansion path, audio evidence handoff, token estimates, stale and contested states. Full discovery of REAPER API and generic Actions must remain reachable; don't dump 732 functions into every prompt.
4. Предложить инструментальную карту для полного user task space: inspect/understand/search capabilities; get current snapshot/context; retrieve/deepen evidence; analyze/cross-compare; plan/propose mutation; apply/revert/save; verify/update context; inspect errors and unknowns. Сравнить direct tools, semantic search/catalog, resources/templates and composed calls. Число tools/resources выбирать только по task-eval. Document exact tool contracts, schemas, error codes, idempotency, revision/lease rules, multi-project isolation, and discoverability.
5. Показать контекстные ответы для любой цели, а не hardcoded mix pipeline: user says goal; model discovers relevant state and evidence; identifies missing context; asks only essential clarification; proposes composable operations; observes actual result; refreshes linked claims. Примеры нужны только как capability tests.

## Coding plan и проверка

Предложить последовательность migration без code rewrite: shared models and serialization; project/time/revision service; evidence/claim ledger; dependency tracking; context views; MCP binding/resources; LLM guide synchronized from schema. Указать backward compatibility, source of truth, schema versioning and migration of current responses. Составить adversarial task-evaluation set: no relevant tool obvious, hidden functionality search, large project navigation, ambiguity/stale context, failed audio delivery, conflicting evidence, long context, multiple projects, model requests ambiguous destructive change. Criteria measure task success, correct abstention, tool selection, context sufficiency, tokens/calls and hallucinated facts.

Только описать acceptance plan, не менять MCP. Зафиксировать, какие interface tests can run headlessly later and which require real MCP host/model/audio delivery. README documentation should describe capabilities, discovery rules, state/evidence semantics and failure recovery—not genre recipes.

## Передать соседям

Выдать canonical schema proposal всем доменным владельцам. R3-01..04 возвращают typed domain outputs and dependency sets; R3-06 returns project deltas and verification evidence. Обратно распространить naming decisions and schema version. Если локальные требования конфликтуют, перечислить конфликт и рекомендуемое общее решение; не молча нормализовать.

