# R2-06 — Полная работа в REAPER, безопасные изменения и end-to-end доказательство

**Владелец темы:** превращение понимания проекта в корректное и проверяемое действие.  
**Выход для общего проекта:** определить, какой DAW-контекст и механизм действий нужны модели для широкого музыкального ремесла, а не для одного заранее заданного pipeline.

Прочитайте общий контракт и все остальные задания. Тестируйте/исследуйте, как R2-01…R2-05 дают именно тот контекст, который edit needs; ваши операции должны возвращать фактические связи и результаты обратно в общий граф.

## Центральный вопрос

Как предоставить модели гибкую возможность делать в REAPER всё, что требуется для проектов после записи человеком, сохранив понимание выбранных объектов, музыкального результата, реального состояния DAW и обратимости каждого изменения?

## Исследовать и ответить

### A. Полное состояние проекта как контекст действия

1. Проведите независимую карту живого REAPER API и текущего bridge: tracks/master/buses, routing, sends/receives, FX/parameters/presets, automation, items/takes/sources/offsets/stretch markers, groups, lanes/comping, tempo/time signature, markers/regions, MIDI, media files, render/export, undo/save/recovery.
2. Для каждой семьи функций укажите read coverage, write coverage, UI/API differences, availability on REAPER version, typed transaction, post-write readback and tests. Не называйте documented function «полной поддержкой» без корректной сигнатуры и проверенного эффекта.
3. Как модель выбирает объект при нескольких открытых проектах, похожих именах треков, дублирующихся takes, folder/bus routing, hidden/disabled FX, automation modes и project reload?
4. Какие минимальные поля нужны, чтобы увидеть полную семантику объекта и его связи до действия? Привяжите их к общему графу R2-05 и signal stages R2-01.
5. Как сохранить все 732 ReaScript functions discoverable (exact/API search, related signatures, runtime check, generic invoke) без раздачи 732 tools и при этом не обходить safety/preconditions?

### B. Универсальные строительные действия и музыкальные задачи

1. Какие общие операции должны позволять модели самостоятельно составить новую задачу: inspect/select/duplicate/split/move/trim/edge extend/fade/slip/stretch/comp/group, MIDI note create/edit/delete, FX lifecycle/parameter automation, routing, gain/pan, envelopes, markers, regions, save/export? Что пока небезопасно/неподдержано?
2. Как выразить «выровнять гитару» не хардкодной функцией, а связкой анализа пользовательской цели, event/rhythm hypotheses, выбором айтемов, preview changes, edge/fade safeguards и post-listen verification?
3. Как выразить написание/редактирование MIDI музыкально осмысленно: связь с реально услышанными партиями, гармонией/рифом, темпом/размером, доступными VSTi/sounds, длинами item, quantization and human feel? Какие части — возможны через API, какие требуют модели музыкальной композиции/инструментальных presets?
4. Как моделировать FX insert/change, pan, automation and mastering decisions так, чтобы видеть источник, destination, current chain, dry/wet path, plugin parameters, units, latency and result?
5. Где нужен typed domain operation и где generic ReaScript escape hatch? Как обеспечить, что advanced calls также проходят revision/scope validation and readback?

### C. Safety, transaction и recovery

1. Специфицируйте действия как preview → apply → actual readback → explicit verification → undo/rollback. Какие immutable IDs, expected revision/fingerprint, preconditions, postconditions, side effects и tolerances требуются?
2. Определите safe atomicity boundaries, idempotent replay, batch semantics, partial failure, timeout handling, stale analysis refusal, bridge restart, undo stack and project switching.
3. Для destructive/ambiguous operations — split/delete/trim/stretch/FX removal/MIDI overwrite/save/export — как сохранить before snapshot, explain expected result, refusal conditions and restore?
4. Как доказать no silent change: сравнить фактическое состояние, не эхо параметров; проверить target project/chain/position; учитывать render settings, selection, solo/mute and project dirty state.
5. Сохранение/versioning: пользователь хочет избежать мусора и бесконечных стемов/копий. Исследуйте durable baseline/final, временные аудио lease/cleanup, checkpoint or undo, naming collisions, project/media copy semantics, failed-save detection and crash recovery. Не удалять пользовательские данные.

### D. Монтаж без артефактов

1. Какие сигнальные и проектные данные нужны для выбора cut point: waveform/transients, zero crossings, phase, sustain/tail, adjacent item source continuity, FX/reverb, loop/stretch markers, take comping, group/lane sync?
2. Как различать splice, crossfade, trim, slip, stretch и move; какой метод исправляет какую ошибку и какие артефакты создаёт?
3. Как связать candidate cut/edit с exact source frame, source offset, project time, musical position, item/take GUIDs, event hypotheses and uncertainty?
4. Что нужно после edit: readback geometry, detect clicks/seams, compare before/after source and full mix, account for tails, verify timing goal and preserve unedited region?
5. Какие операции можно сделать автоматически при high confidence, какие только preview-first, а какие должны требовать решения пользователя?

### E. Сквозная проверка широкой цели

Предложите набор лицензированных/созданных тестовых проектов, включающий routing, FX на track/bus/master, automation, tempo map, MIDI, groups/lanes, stereo, multitrack physical recordings and deliberate traps. Не считать простой synthetic split/undo полноценной музыкальной приёмкой.

Для нескольких разнотипных задач проверить весь путь: пользовательская цель → актуальный project/audio graph → поиск релевантных evidence/tools → план (если нужен) → preview/apply → фактический readback → повторный capture/analysis/listener → итоговое объяснение с доказательствами → rollback/restore. Включить negative cases: stale context, missing audio, uncertain rhythm, unsupported plugin, project switch, save collision, API failure and corrupted/partial operation.

Определите измеримые gates для:

- правильной идентификации project/entity/time/audio;
- отсутствия несвязанных побочных изменений;
- musical outcome (not just API success);
- слышимых артефактов и сохранения intended feel;
- корректной диагностики до/после;
- успешного undo/recovery;
- задачи, которую модель сама разложила на общий набор доступных действий без предписанного рецепта.

## Общие связи

- **Получить от R2-01:** stage-correct capture, capture provenance, test of audio delivery and post-edit render.
- **Получить от R2-02:** editable event/note object semantics and confidence thresholds.
- **Получить от R2-03:** rhythm hypotheses, target grid and ambiguity/phase rules.
- **Получить от R2-04:** issue/reference claims and post-action quality criteria.
- **Получить от R2-05:** shared graph IDs, time mappings, revision/lineage and API discovery surface.
- **Передать всем:** actual state changes, side-effect footprint, verification evidence and refreshed/stale dependency links.
- **Решить конфликты первой волны:** TrackFX_AddByName record-FX risk, missing item context, request-echo instead of actual state verification, render-tail omissions, unsafe file globbing and incomplete save verification. Recheck each finding against current branch; do not blindly transplant old findings.

## Требуемый вывод

Разделите «API можно вызвать», «операция корректна на объекте», «музыкальная задача дала нужный результат» и «система безопасно доказала результат». Сформулируйте capability/gap matrix и gates до работы на важных проектах. Не ограничивайте будущую библиотеку только сценариями, названными в этом задании.
