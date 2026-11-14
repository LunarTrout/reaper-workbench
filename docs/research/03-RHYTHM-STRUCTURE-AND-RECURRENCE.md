# Сабагент 3 — ритм, метр, форма и повторяющиеся риффы

## Решение, для которого нужен ответ

Как системе выводить по звучанию фактические пульс и метр, начало такта/фразы, groove и изменение темпа, а также находить границы риффов, повторения и варианты повторов? Проектная сетка REAPER должна быть доступной подсказкой, но не заменять слуховой вывод.

## Что исследовать в нашем проекте

- analysis/tempo.py, transient/onset tools, edit_map.py, section novelty/self-similarity, group alignment, tempo map и related tests.
- Где модель сейчас использует tempo map, а где отдельную audio-derived оценку. Отдельно проверить distinction project meter vs performed meter.
- Понимает ли edit map только ближайшую долю сетки или музыкальную функцию события; применяются ли default thresholds и track-name heuristics.
- Исторические project examples, ручной ORIG, тестовые metal audio и существующие annotations, если они доступны.

## Вопросы внешнего исследования

1. Сравнить beat tracking, tempo tracking, downbeat detection, meter/time-signature inference, beat/bar phase, groove/swing/microtiming и tempo-map estimation. Указать, какие выходы могут быть неоднозначны в риффе.
2. Исследовать структурную сегментацию и retrieval повторяющихся фрагментов: self-similarity matrices, embeddings, chroma/rhythm features, beat-synchronous methods, learned representations, local alignment/dynamic time warping.
3. Отдельно оценить задачи для black/death metal: blast beats, halftime/doubletime, смена тактовой группировки, syncopated chugs, tremolo picking, неакцентированные riff repetitions, разные исполнения одного мотива, tempo drift.
4. Как соотнести project tempo/signature, аудио-кандидаты, note events, барабанные акценты и повторяемость между инструментами? Как сообщать несколько объяснений, например половинный/двойной BPM или 3/4 против 6/8?
5. Какие audio representations и training/evaluation datasets отражают heavy music; что делать при отсутствии подходящей размеченной выборки?
6. Какими метриками проверять tempo, beat, downbeat, meter, boundary, recurrence retrieval и variant matching? Нужны tolerance windows и музыкально осмысленные критерии.
7. Какие методы помещаются в обычный DSP-проход, а какие требуют отдельной модели или GPU? При GPU использовать только один V100 16 GB.

## Результат

Предложить multi-resolution map музыкальной структуры: hypotheses, anchors, confidence, alternatives, relation to project grid, repetition pairs and evidence clips. Сравнить SOTA-методы и найти практическую архитектуру для нашего проекта, указав ограничения и тесты на металле. Не превращать результат в жанровый quantize recipe.

