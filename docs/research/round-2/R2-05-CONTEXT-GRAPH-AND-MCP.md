# R2-05 — Единый контекст, граф доказательств и LLM-интерфейс MCP

**Владелец темы:** модель того, что агент знает о проекте, звуке, цели и предыдущих действиях.  
**Выход для общего проекта:** предложить связный и навигируемый контекст, который остаётся полным по смыслу, но не требует помещать весь проект и все измерения в prompt.

Прочитайте общий контракт и все остальные задания. Это интеграционная роль, но не право навязать другим темы без проверки их evidence. Вам нужны реальные inputs/outputs R2-01…R2-04 и операции/проверки R2-06.

## Центральный вопрос

Как представить музыкальный проект и звуковые свидетельства так, чтобы текстовая модель могла строить абстрактные выводы из конкретного звука, видеть зависимости и ограничения, находить нужные действия, а после изменения проследить влияние по связанным сущностям?

## Исследовать и ответить

### A. Требования к цельному контексту

1. Определите минимально достаточный контекст не для одного узкого инструмента, а для набора задач после физической записи: изучить исполнение, понять форму, диагностировать звук, выровнять/нарезать, сочинить/редактировать MIDI, аранжировать, обработать, панорамировать, свести, мастерить, экспортировать и исправлять ошибки.
2. Что модель должна знать о пользовательской цели, ограничениях/предпочтениях, ожидаемом результате, допустимой степени изменения и необходимости согласования? Что нельзя надёжно вывести из аудио?
3. Разделите «состояние проекта», «захваченный звук», «измерение», «наблюдение listener», «музыкальную гипотезу», «цель», «план/операцию», «изменение проекта» и «проверку результата». Покажите, какие links идут между этими слоями.
4. Как представить проект одновременно как timeline, signal-flow graph, музыкальную структуру, source/performer map и изменяемое состояние DAW?
5. Как сделать отношения машинно-навигаемыми: например, от claim «гитара слишком сухая в риффе B» до нужного окна master/stem, дорожек, FX/sends, событий исполнения, reference и обратно? Какие неоднозначные пути нужно показывать?

### B. Проект графа/схемы

Сравните graph, relational/normalized store, nested report, event sourcing, property graph и другие подходы. Не выбирайте «graph» только по метафоре. Определите:

- сущности и stable IDs;
- typed directed edges, cardinality, edge provenance and temporal scope;
- состояния во времени, revisions/fingerprints/epochs и stale dependency propagation;
- time-basis transforms и uncertainty;
- signal stages, track/bus routing, FX/send/automation links;
- musical events, notes, riffs/sections, roles and interpretations;
- references, user intent, actions, expected/actual state and verification;
- content-addressed audio assets, leases/TTL, released/expired tombstones, retention of initial/final project only;
- source-of-truth ownership when the REAPER project and derived graph disagree.

Каждая метрика/claim/event/observation должна иметь lineage. Объясните, как compact output сохраняет ссылочную целостность при pagination, detail views, external audio assets и long tracks.

### C. Переход между уровнями абстракции

1. Опишите navigation operations для движения source/sample→event→part/riff→section→arrangement/mix conclusion и обратного drill-down.
2. Как система устанавливает причинную иерархию без ложного утверждения «этот FX вызвал проблему»? Нужны supporting links, alternatives, counterevidence и discriminating actions.
3. Как граф меняется после edit: какие claims становятся stale; какие outputs можно считать сохранными; как запись операции связывает before/after audio, project state и analysis?
4. Как context assembler выбирает релевантный subgraph под произвольную цель пользователя без hardcoded task pipelines? Сравните query-by-entity, temporal scope, goal/constraint, relation traversal, search и progressive disclosure.
5. Как доказать, что сборщик не пропустил критичное: coverage matrix по объектам/анализам, explicit unknowns/missing, index/query recall, source/section traceability и context-ablation test?

### D. MCP-поверхность и модельное поведение

1. Спроектируйте capability discovery для полного REAPER API без загрузки всех 732 функций в каждую сессию: domain taxonomy, exact symbol search, structured search card, related calls/prerequisites, runtime availability, pagination, generic escape hatch, typed high-level operations.
2. Сколько MCP-инструментов/ресурсов нужно и почему? Не оптимизируйте только на меньшее число. Измерьте полноту выполнения произвольных задач, tool-selection recall/completeness, лишние вызовы, prompt burden, accidental omissions and failure recovery.
3. Как организовать пакетный broad analysis: одной командой создать project/audio snapshot, рассчитать подходящие доступные analysis families и вернуть manifest; затем модель выбирает views/прослушивания/сравнения/точечные анализы по задаче. Где это полезно, а где нельзя запускать всё подряд?
4. Как instructions/README объясняют не recipe, а способность модели декомпозировать новую цель, искать подходящие tools, проверять prerequisites, уточнять требования и проверять результат?
5. Какие схемы описаний, outputSchema, examples и instruction tests реально улучшают discovery? Проведите task-based evaluation, а не стилистическую оценку README.
6. Как клиент подтверждает, что аудио доставлено модели, а не только лежит по URI? Какие статусы forwarded, unknown, uri_only и known-answer проверки нужны?

### E. Главный эксперимент на достаточность контекста

Разработайте эксперимент, который прямо проверяет утверждение пользователя «контекста может быть достаточно». Для одних и тех же музыкальных задач сравнить:

- один плоский текстовый summary;
- DSP-only;
- audio/listener-only;
- project-only;
- связанный full context;
- связанные views с доказательным drill-down.

Используйте blind tasks разных классов: диагноз/референс, вопрос по конкретному событию/риффу, предложить correction, написать/изменить MIDI под услышанную партию, найти правильные REAPER entities, применить/проверить edit. Измеряйте expert agreement, factual grounding, entity/time selection, correction quality, omissions, tool selection, reversibility and unnecessary calls. Отдельно проверьте, какие конкретные links/fields добавили пользу, а какие только увеличили контекст. Учитывайте человеческое расхождение, а не задавайте один экспертный текст как абсолютную истину.

## Общие связи

- **Получить от R2-01:** audio asset, signal stage/channel/window, delivery and model-observation schema.
- **Получить от R2-02:** event/note/technique ontology, event confidence and source/time transforms.
- **Получить от R2-03:** pulse/beat/bar/riff/section hierarchy and alternatives.
- **Получить от R2-04:** mix claim types, causal alternatives, metric families, references and human preference.
- **Передать R2-06:** stable entity IDs, revision gates, evidence-bound operation requests, before/after lineage and dependency invalidation.
- **Решить конфликты первой волны:** unified report is a navigation contract, not a giant inline blob; fewer tools is only a hypothesis until task evaluation; audio URI is not proof of hearing; all API functions remain discoverable without flooding.

## Требуемый вывод

Нарисуйте end-to-end traceability examples in domain-general terms (not a fixed audio workflow): from raw evidence to an abstraction and back to action and verification. Give a candidate schema/graph and explain uncertainty, provenance and retrieval. Keep the design flexible enough for goals not enumerated above.
