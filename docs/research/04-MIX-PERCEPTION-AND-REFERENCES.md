# Сабагент 4 — восприятие микса, диагноз и референсы

## Решение, для которого нужен ответ

Как приблизить способность опытного звукоинженера услышать проблему в целом и в нужном источнике, объяснить её, предложить разумные варианты и проверить улучшение? Типовой вопрос: «наши гитары сухие относительно референса — что слышно, где причина и какие варианты пробовать?»

## Что исследовать в нашем проекте

- Default DSP bundle: уровень, loudness, dynamics, spectrum/timbre, phase/spatial, masking heuristics, source-to-master deltas, reference analysis.
- Music Flamingo, Ossicle-4B, known-answer/paired-comparison failures and calibration scripts.
- Наличие full track/master renders, routed/processed context, clip/reference inputs, comparison tools, audio limits, revisions and capture provenance.
- Утверждения README о том, что является measured fact, heuristic, model judgement и unsupported inference.

## Вопросы внешнего исследования

1. Что реально помогает моделям определять mix problems, instrument roles, masking, dryness/space, tonal balance, width/depth, dynamics, distortion и phase problems? Сравнить listening models, audio-language models, DSP and hybrid judgement.
2. Какие benchmark'и оценивают audio QA и mix/mastering judgement? Есть ли экспертные preference datasets, metal-specific data и схемы blind evaluation?
3. Как сравнивать полный стереореференс с нашим миксом и отдельными stem/track? Исследовать loudness matching, section alignment, stereo preservation, normalization dangers, reference-target mismatch.
4. Как проверять позиционные и order biases, self-consistency, confident hallucinations, directional sign errors, genre bias и misleading model explanations?
5. Какие измерения надёжны как supporting evidence для субъективного впечатления, а какие создают ложную определённость? Указать ограничения psychoacoustic metrics, masking proxies, loudness, spectral centroid, phase correlation и др.
6. Как должен выглядеть проверяемый ответ на вопрос о миксе: услышанное наблюдение, source/time anchor, evidence, interpretation, alternative causes, proposed actions, expected trade-off and post-change test?
7. Как калибровать модели на тяжёлом металле и пользовательских предпочтениях, не зашивая универсальные правила «как должен звучать жанр»?

## Результат

Предложить доказательную систему аудио-диагноза и reference comparison: какие модели применять, как сочетать с измерениями, как определять конфликт моделей и как не превращать стиль в фиксированный target. Отдельно описать dry-guitar benchmark и минимальный набор truth/ratings, которые нужен от человека.

