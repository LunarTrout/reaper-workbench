# Сабагент 6 — REAPER, безопасные правки и end-to-end доказательство

## Решение, для которого нужен ответ

Как из слухового вывода перейти к полному и надёжному действию в REAPER через документированный нативный путь, сохранить естественное исполнение и доказать, что результат звучит правильно? Охватить не только ритмический монтаж, но и полный жизненный цикл работы с записанным проектом: items/takes, routing, FX, automation, MIDI, mix/master, saving and delivery.

## Что исследовать в нашем проекте

- scripts/reaper_bridge.lua, ReaScript API catalog, bridge client, session/operations, edit planning, alignment, seam search, project capture/render and save functions.
- D_STARTOFFS/stretch markers/timebase/fades/group operations, revision checks, apply/readback/undo, project dirty state, native capture side effects.
- docs/TZ.md, EDITING-ACCEPTANCE.md, GAP-ANALYSIS.md, SAFETY, VALIDATION, recent commits and live logs where available.
- Разницу между API, который catalogued, API, доступным через generic call, typed wrappers, unit-tested operations, live-tested operations and end-to-end accepted behavior.

## Вопросы внешнего исследования

1. Какие документированные REAPER APIs/Actions обеспечивают audio access, track/master rendering, takes/items/FX/routing/automation/MIDI, revision-safe editing and readback? Какие signal paths обходят FX, sends или master chain?
2. Как корректно резать, перемещать, раскрывать края, менять source offset, crossfade и сохранять tails/phase/PDC? При каких режимах playrate, stretch markers, tempo-based items и groups простая формула невалидна?
3. Как выбирать seam с минимальным риском щелчка, дырки, повторной атаки, потери сустейна, фазовой отмены и нарушения groove? Сравнить zero-crossing, envelopes, multi-channel/multi-mic search, crossfades and render-based audition.
4. Как строить revision-bound transactions, previews, independent readback, rollback, crash/failure recovery, safe project copies and save/version policy?
5. Какие end-to-end сценарии проверят весь путь: услышать проблему → найти source/time → сформировать edit map → preview/apply → render → compare → rollback/accept? Охватить также MIDI/FX/reference workflows.
6. Как сохранить только исходный и принятый финал, автоматически очищать временные audio analysis assets и не создавать стемы/версии на плейлисте или в media folder?
7. Какие тестовые проекты и ground-truth edits подходят для тяжёлой многодорожечной музыки; как добавить субъективную оценку владельца и формальные acceptance gates?
8. Что можно считать «полным доступом к REAPER» на практике; где полный ReaScript catalog не равен надёжно реализованному и безопасно проверенному поведению?

## Результат

Предложить корректную native REAPER integration architecture, edit/transaction invariants, capture provenance, artifact-risk validation and end-to-end acceptance plan. Отдельно перечислить поддержанные, непроверенные и опасные операции, необходимые API gaps и как закрывать их узко, не добавляя Frida, память процесса или genre-locked workflows.

