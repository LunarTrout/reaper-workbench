# R3-03 — план реализации времени, формы и намерения монтажа

## Владелец направления

Перевести R2-03 в реализуемый контракт музыкального времени и структуры, который сохраняет неоднозначность исполнения и поддерживает композиционные цели пользователя. Upstream — capture/time mappings R3-01 и performance events R3-02; downstream — safe edit target R3-06, context graph R3-05 и section-matched reference comparison R3-04.

## Обязательное чтение и code mapping

Прочитать общий контракт, все шесть R2 финалов и шесть R3-брифов. Проследить src/reaper_audio/analysis/tempo.py, timeline_context.py, temporal.py, events.py, edit_map.py, edit_map_report.py, daw/alignment.py, daw/align.py, daw/editing.py; Lua/ReaScript tempo/time-signature/automation map; MCP exposure; tests/test_tempo.py, test_alignment.py, test_align.py, test_edit_map.py, test_edit_map_report.py, test_editing.py. Найти реальные символы и фактические time bases.

## Что должно быть согласовано

1. Отдельные signal/file/sample, project, performed и notational time координаты; двусторонние преобразования, версия карты и неопределённость преобразования. Tempo/time-signature automation рассматриваются как функция времени/события. Ни базовый BPM, ни marker/grid не подменяют измерение исполнения.
2. Схема и зависимости для pulse/beat/phase/bar/meter/grouping/tempo curve/section/riff/repetition/variant/cadence/tail. Для каждого — гипотеза или факт, evidence clips/events, альтернативы, confidence, метод, human/marker annotation и допустимый downstream use. «Не найдено повтора/метра» и abstain — допустимы.
3. Контракт задачи монтажа: user intent; выбранная hypothesis_id; event-to-target correspondence; phase mode; protected events/regions; explicit bounds, source offset/fades/stretch policy; allowable operations; preview diff; expected audible/post-edit invariants. Не выводить необходимость quantize из одного процента отклонения от сетки.
4. Для инструментальных целей отдельно разобрать phrase-level and transient-level alignment, сохранение грува, attack/body/tail, repeated phrase variants и риск ложного grid. MIDI creation/editor timeline uses same musical map but distinct from inferred audio notes.
5. Определить область stale invalidation: edit tempo/map changes invalidate only dependent musical times, not unrelated source measurements; edit audio invalidates relevant onset/structure windows. Сверить с R3-05 dependency model.

## Coding plan и приемка

Сформировать file::symbol backlog с owner, inputs/outputs, units, time bases, rejection/ambiguity behavior and migration. Предложить tests/evaluation matrix: fixed and automated tempo, non-4/4 and competing phases, metronome drift, intentional push/pull, repeated/variant riffs, no-repeat cases, project-grid mismatch, item-edge rendering and re-analysis. Задать отдельные quality measures для event-target correspondence, timing, phrase integrity, tails, feel and abstention. Criteria for musical acceptance require annotated examples/listener review; no false pass from beat-grid proximity alone.

Только описать тесты. Не запускать REAPER или тесты, не менять item/MIDI/tempo. Выделить для живой проверки семантику текущего REAPER version, effective quarter BPM, automation mapping and saved project readback.

## Передать соседям

R3-02 отдаёт occurrences/gestures с неопределённостью; R3-01 — correct offsets/windows; R3-05 — typed time edges and revisioned dependencies. R3-06 должен получать только конкретный, ревизионно-валидный edit intent и возвращать фактические границы/offset/fades plus new evidence; R3-04 получает section identities/alignments для сопоставления с референсом.

