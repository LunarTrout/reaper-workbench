# R2-02 — События исполнения, ноты и происхождение музыкального материала

**Владелец темы:** переход от сигнала к исполняемым событиям.  
**Выход для общего проекта:** объяснить, как модель узнаёт, что и когда сыграно, где заканчивается надёжное наблюдение и начинается гипотеза.

Прочитайте общий контракт и все остальные задания. События должны соединять R2-01 (источник сигнала), R2-03 (ритм/риффы), R2-04 (роль и слышимость), R2-05 (граф и контекст) и R2-06 (редактируемые объекты).

## Центральный вопрос

Как превратить реальное исполнение человека в описание, достаточно точное для анализа, монтажа и аранжировки, не выдавая неопределённую транскрипцию тяжёлой гитары за достоверные ноты или MIDI?

## Исследовать и ответить

### A. Типы музыкальных событий и уровни достоверности

1. Предложите ontology, разделяющую signal events (onset, transient, clipping, decay, silence), performance events (атака струны/удара, нота, chord, articulation, sustain/stop), score/MIDI objects и higher-level interpretations (phrase/riff/role).
2. Для каждого типа определите минимальные поля: stable ID, source/take/item/track IDs, frame/time interval, time basis, pitch/chord candidates, duration/velocity/intensity, channel/stage, method, alternatives, confidence basis, status, evidence links.
3. Как связать одно перцептивное событие с несколькими физическими/сигнальными атаками, например дабл-трекингом, аккордом, шумом медиатора, cymbal bleed или FX transient? Как не объединять разные события из-за похожего onset?
4. Что означает «точность достаточно хороша для просмотра», «для диагноза», «для автоматического редактирования», «для создания MIDI»? Разделите допуски по последствиям ошибочного действия.

### B. Источники правды и роль DI/pre-FX

1. Инвентаризируйте, что в текущем проекте известно из MIDI и что физически исполнено в audio. Различайте observed MIDI, human-entered note, model transcription, inferred role и unassigned source.
2. Проверьте доступные в проекте REAPER-native способы получить source/DI/pre-FX и их временную привязку. Что именно значит текущий AudioAccessor capture, включая gain/pan/fader/FX; можно ли считать его DI?
3. Как сопоставить raw/pre-FX signal, processed stem и master, сохранив mapping до одного источника? Какие FX меняют detectable onset/pitch/tail, какие routing пути смешивают источники?
4. Когда separation допустима как preprocessing; как учитывать leakage/artifacts и не выдумать чистую инструментальную дорожку из разделения?

### C. Ноты, высоты и артикуляция на тяжёлой гитаре

1. Сравните доказательные методы для pitch, note boundaries, polyphony, chord changes, tuning/range, harmonics, palm mute, tremolo/downstroke и длительности на перегруженных гитарах. Отдельно оцените DI vs amp/render, низкий строй, быстрые ноты и дабл-трекинг.
2. Не смешивайте pitch class, фундаментальную частоту, ноту с октавой, ноту в аккорде и физическую струну/лад. Укажите, какие задачи имеют надёжные наблюдаемые признаки и где нужно abstain.
3. Как формировать варианты с confidence, альтернативами и source event links? Как обнаружить октавную/полутоновую ошибку, пропуск атаки, ложный transient, неверную длительность и ошибочное разделение аккорда на ноты?
4. Что можно достоверно получить из существующих MIDI, audio, event/transient detectors и temporal profiles, а что сейчас только кажется музыкально осмысленным?
5. Как минимум сравнить модель с аудио-эталоном и MIDI-истиной, учитывая, что midi item может быть на другой дорожке/временной базе и не описывать реальный recorded take?

### D. События как опора для других решений

1. Как R2-03 должен использовать атаки и длительности, не трактуя их как готовые beats/downbeats?
2. Как R2-04 различает дефект микса и особенность исполнения: приглушённая атака, неравномерный picking, double-track timing, noise, articulation?
3. Как R2-06 преобразует event candidate в редактируемый объект REAPER: mapping event→source→take→item, точность положения, риск края/фейда/stretch marker?
4. Какие связи нужны, чтобы кликнуть/запросить «эту ноту/удар» и открыть исходный звук, соседний контекст и относящийся к нему MIDI/item?

### E. Воспроизводимый бенчмарк

Сконструируйте task-oriented разметку на живых/DI исполнениях пользователя или доступном лицензированном материале: note/onset times, durations, riffs, ambiguities, original feel. Нужны несколько annotators/разногласие человека, разделение train/calibration/test, tests на untouched artists/recordings, метрики (onset timing, pitch class+octave, note overlap, event precision/recall, edit outcome) и отдельные пороги для анализа и автоматической правки. Укажите, какой объём можно честно размечать и какие ограничения нельзя закрыть публичными датасетами.

## Общие связи

- **Получить от R2-01:** capture types/stage, канал layout, file/source/project mapping, latency и audio delivery proof.
- **Получить от R2-03:** гипотезы tactus/downbeat/meter, structure units и требования к source/project musical times.
- **Получить от R2-04:** role/effect diagnostics, bleed/masking, необходимость различать исполнение и микс.
- **Передать R2-05:** event ontology, time-base transforms, confidence/status, provenance и event filters/pagination.
- **Передать R2-06:** границы редактируемых событий, required item/take fields, допустимые uncertainty thresholds и signal-based post-checks.
- **Решить конфликт первой волны:** timing corrections can use onset evidence without requiring exact pitch; MIDI generation/transpose requires much stronger pitch/harmony evidence. Это разные уровни достоверности.

## Требуемый вывод

Дайте честную capability map: что библиотека уже знает, что может измерить, что может предложить как кандидата, что можно автоматизировать и где требуется человек. Не обещайте universal polyphonic transcription или automatic note truth без испытаний на metal.
