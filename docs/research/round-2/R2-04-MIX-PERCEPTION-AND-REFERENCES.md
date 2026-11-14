# R2-04 — Восприятие микса, диагностика и референсы

**Владелец темы:** переход от слышимого результата к обоснованному миксовому выводу.  
**Выход для общего проекта:** показать, какие свойства звука можно измерить, какие интерпретировать, как связать проблему с конкретными источниками и проверить изменение.

Прочитайте общий контракт и все остальные задания. Диагноз без связанного source/section/stage не считается достаточным. Координируйтесь с R2-01 (сигнал/listener), R2-02 (исполнение), R2-03 (музыкальные области), R2-05 (claims/context) и R2-06 (FX/action/post-check).

## Центральный вопрос

Как приблизить опыт опытного звукоинженера для языковой модели настолько, чтобы она могла описать проблему, понять её вероятные причины, предложить подходящее действие и сравнить до/после — без псевдонаучного единственного quality score и жанровой кривой?

## Исследовать и ответить

### A. Таксономия восприятия и causal attribution

1. Разложите широкие пользовательские понятия (dry, muddy, thin, harsh, masked, weak attack, boomy, narrow, phasey, distant, crushed, harsh clipping, unbalanced, unclear) на отличимые perceptual constructs и проверяемые свидетельства.
2. Для каждого construct установите: какие признаки доступны в isolated source, processed track, bus и master; какие метрики можно вычислить; что они доказывают и что не доказывают.
3. Как связать слышимую проблему с несколькими возможными причинами — performance, arrangement, source tone, masking, EQ, compression, saturation, phase, pan, reverb, routing, automation, monitoring/reference? Представляйте causal hypotheses, альтернативы и discriminating tests, а не необоснованную причинность.
4. Какая информация из FX chain/параметров/automation/sidechain/sends нужна, чтобы видеть вероятный signal path и объяснить, почему stem и master различаются?
5. Как различить технический дефект и намеренную художественную характеристику; в каких случаях нужна цель/предпочтение от владельца?

### B. Человек, listener-model, DSP и learned critic

1. Сверьте выводы R1 по Music Flamingo/Ossicle и найдите свежие первичные результаты по моделям/методам диагностики. Отдельно оцените metal, distorted guitars, stereo/phase, loudness matching и conversational claims.
2. Почему zero-shot аудиоописание не равно mix judgement? Какие ошибки/position biases/влияние формулировки должны быть проверены?
3. Изучите issue-specific mix critics, controlled degradations и человеческие рейтинги. Какой объём данных/сегментация/метаданные нужны для критика конкретного дефекта на metal, не создавая иллюзию универсального судьи?
4. Разделите observations («модель описала»), measured evidence, calibrated severity, human preference и recommendation. Что делать, если они конфликтуют?
5. Предложите форматы ответа, которые говорят: наблюдаемое, вероятные объяснения, альтернативы, confidence basis, что проверить, что изменится при каждом результате.

### C. Референсы и сравнительное суждение

1. Какие пары/группы корректно сравнимы по stage (mix/master/stem), loudness, section/function, role/instrumentation, stereo/channel layout, duration и performance?
2. Как loudness-match для слухового сравнения, сохранив независимые абсолютные loudness measurements?
3. Как построить референс не как один «правильный ответ», а как распределение/кластеры допустимых решений? Как отделить отличие аранжировки от микс-проблемы?
4. Как привязать reference segment к source/section/analysis, учесть лицензирование/хранение и объяснить модели, почему эта пара выбрана?
5. Какие comparisons нужны для переходов «гитара сухая» → «какой участок/дорожка?» → «вероятная причина?» → «какие изменения попробовать?» → «улучшилось ли?»

### D. Контекст и действия

1. Какие claims R2-05 должен иметь для mix issue: scope, sources/buses, interval/section, signal stage, evidence IDs, metric family/unit, observation reliability, alternatives, reference metadata?
2. Какие события и детали исполнения из R2-02/R2-03 помогут объяснить миксовую оценку (переменная артикуляция, плотность, полифония, слабое заполнение спектра, риффовая смена)?
3. Какие REAPER-поля/FX/linkage нужны R2-06, чтобы предложить и проверить корректировку? Когда action следует применить к source track, group, bus или master?
4. Как library должна проверить пост-изменительную картину так, чтобы изменение presence не оценивалось по метрике, которая его почти не замечает?

### E. Воспроизводимый опыт

Предложите blind benchmark на одних и тех же multitrack metal excerpts: baseline mix + контролируемые варианты по одному/нескольким дефектам + mix engineer ratings + user preference. Укажите подбор экспертов, repeated samples, inter-rater agreement, severity/just-noticeable changes, same-section references, test split по песням/исполнителям и критерии. Разделите accuracy диагноза, причинной гипотезы, рекомендации, perceptual preference и post-change improvement.

## Общие связи

- **Получить от R2-01:** stage-correct stereo audio, master/stems/buses, channel transforms, listener limitations.
- **Получить от R2-02:** performance features, role/identity caveats, DI-vs-processed mapping.
- **Получить от R2-03:** section/riff mapping, tempo/meter and musically comparable windows.
- **Передать R2-05:** taxonomy claims, units/metric families, evidence/causal graph, reference links and uncertainty.
- **Передать R2-06:** hypotheses-to-FX/automation actions and measurable/subjective post-check criteria.
- **Решить конфликты первой волны:** mix distance is a guardrail, not overall quality; LALM is not a judge by default; listener text cannot replace stage- and section-matched evidence.

## Требуемый вывод

Покажите, как из богатой карты источников/событий/сигнальных путей модель может перейти к абстрактному миксовому выводу и обратно к аудиодоказательству и конкретным доступным действиям. Отдельно укажите, какой уровень уверенности достижим до сбора человеческих рейтингов.
