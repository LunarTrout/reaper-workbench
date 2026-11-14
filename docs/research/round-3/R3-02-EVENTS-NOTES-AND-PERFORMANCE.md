# R3-02 — план реализации событий, нот и исполнения

## Владелец направления

Перевести R2-02 в план для надёжной интерпретации исполнения человека — от акустического события до проверенной музыкальной аннотации. Главный upstream — звуковые evidence contracts R3-01; downstream — метрическая структура R3-03, mix attribution R3-04, context graph R3-05 и разрешение монтажных действий R3-06.

## Прочитать и проследить в коде

Общий контракт, все R2 финалы и все R3 брифы. Исследовать src/reaper_audio/analysis/events.py, temporal.py, transients.py, tempo.py, timeline_context.py, edit_map.py/edit_map_report.py, models.py; связанные daw editing/alignment; MIDI/project context в Lua bridge; tests/test_analysis_core.py, test_tempo.py, test_edit_map.py, test_edit_map_report.py, test_alignment.py и другие реально найденные тесты. Не считать имена функций достаточным доказательством поведения.

## Решения, которые должны стать готовыми к коду

1. Согласовать пятиуровневую схему R2-02: сырое signal observation; occurrence/event; сгруппированное performance gesture (occ_/peg_ из R2); musical interpretation; human/project annotation. Описать поля, стабильность ID, parent/child/overlap/variant relations, status, confidence, alternative hypotheses и evidence references. Отдельно показать, где onset/silence/clip являются SignalEvent, а где нота остаётся pitch-class/note candidate.
2. Специфицировать связь сигнального времени, event/performed time и musical/notational time с R3-01/03. У события должны быть точность, time_basis, support window, источник, метод, confidence, occurrence grouping и provenance. MIDI в проекте — ProjectFact, не аудио-подтверждение.
3. Разобрать ложное объединение/разделение на плотном металле: дублированные гитары, аккорд против нескольких нот, медиаторная атака и sustain, bleed, FX, повтор loop. Сформулировать какие гипотезы безопасны для просмотра, а какие могут управлять destructive cut/MIDI; задать abstain/review gates, не универсальные жанровые правила.
4. Обосновать tolerances по последствиям и источнику из R2, и превратить их в configurable policy с units/time basis. Проверить существующие пороги в коде: не копировать их как truth без контрольного oracle.
5. Показать обратный поток: редактирование/переслушивание/верификация обновляет или инвалидирует только зависимые event claims. Не делать автоматическое точное MIDI из насыщенной гитары без достаточного evidence и предусмотренного режима подтверждения.

## Coding plan и проверки

Для каждой предложенной сущности указать Python type/schema location, сериализацию, backward-compatibility и конкретных producers/consumers. Составить backlog от event normalization и candidate annotations до grouping/uncertainty/API exposure. Точный файл/символ найти самостоятельно. Предложить unit/property/integration fixtures с known-answer MIDI/audio, варианты тактовых и временных интерпретаций, плотные металл-фрагменты, double-track, повтор/loop и edge cases. Метрики отдельно для false split, false merge, pitch, timing и abstention; false merge=0 из R2 проверить как предлагаемый gate и объяснить его измерение.

Только планировать тесты; не добавлять fixtures и не запускать их. Чётко выделить, какие quality gates невозможны без ручной разметки или V100 inference и какой минимальный набор annotation нужен.

## Передать соседям

R3-01 предоставляет aligned source audio, stage и signal events. R3-03 получает occurrence/gesture groups и альтернативы без подмены нотной сеткой. R3-05 получает canonical IDs/relationships/status/evidence. R3-06 получает строгую eligibility policy: какие event status/hypothesis IDs пригодны для preview/apply и какие приводят только к вопросу/отказу. R3-04 получает роль/слышимую активность как evidence, но не как диагноз masking.

