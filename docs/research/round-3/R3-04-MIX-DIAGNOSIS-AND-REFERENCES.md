# R3-04 — план реализации восприятия микса, диагностики и референсов

## Владелец направления

Перевести R2-04 в кодовый план для обоснованных mix claims и воспроизводимого сравнения. Upstream — звуковой evidence R3-01, performance/section map R3-02/03, shared claims/dependency contract R3-05. Downstream — изменение параметров и проверка результата R3-06.

## Прочитать и исследовать

Прочитать общий контракт, все R2 финалы и все R3 брифы. Сопоставить с src/reaper_audio/analysis/metrics.py, mix_evidence.py, compare.py, temporal.py, partial.py, phase.py, mastering.py, flamingo.py, ossicle.py; src/reaper_audio/corpus/*; daw/cycle.py, eq.py, limiter.py, delivery.py; related tests and docs/MIX-QUALITY-METHODOLOGY.ru.md, docs/mix-craft.md, docs/AI-MIX-SURFACE.md. Отследить, какие показатели реально вычисляются и кто их потребляет.

## Вопросы реализации

1. Сформировать схему MixObservation/MixClaim/IssueHypothesis: scope обязателен (source(s), stage, time/section, stereo/channel, level reference), method, evidence refs, confidence, alternatives, intentional-character/user constraints, detected versus inferred. Смешать master/track stages или объявить причину эффекта без подтверждения нельзя.
2. Распределить обязанности DSP и audio listeners. Для каждой метрики описать units/definition/validity domain/failure modes; для semantic listener — input view, claim type, calibration and explicit limitation. Similarity/distance — guardrail; качество — отдельный validated construct. Не трактовать корреляцию как причинность.
3. Специфицировать reference protocol: matched track/section/role/stage/loudness/stereo; distribution/cluster/leave-one-out; selection provenance/why chosen/license; time alignment and normalization. Объяснить, что можно сравнить текущими corpus/compare modules и чего не хватает.
4. Спроектировать human listening/evaluation protocol для металла: issue taxonomy не должна быть жанровой цепочкой; собирать целевые ratings и inter-rater agreement, blind A/B where appropriate, same-content degradation controls and expected uncertainties. Отделить общий обучающий/калибровочный корпус от пользовательских референсов.
5. Связать диагноз с одним или несколькими candidate actions и predicted evidence changes, не с автоматической выдачей одной EQ/панорамы. После операции требуется новая звуковая проверка той же зоны/стадии и обновление claim lineage.

## Coding plan и gates

Для каждого типа claim указать точные code entry points, существующие функции/отчёты и будущие interfaces. Выделить, что можно сделать CPU DSP без inference, что требует V100 и какой размер/VRAM/latency проверки нужен, но сейчас inference не запускать. Составить план unit/known-answer/corpus/LLM/human tests. Для каждого acceptance gate задать target construct, matched conditions, pass/fail/inconclusive, статистику и риск контаминации; proposed thresholds from R2 не считать утверждёнными нормами без валидации.

## Передать соседям

R3-01 предоставляет точный stage, level, aligned multi-track/master windows и delivery status. R3-02/03 предоставляют event/section map and uncertainty. R3-05 получает claims/evidence/dependencies and alternatives. R3-06 получает action candidates with measurable expected_after and explicit verification plan, never a universal mix recipe.

