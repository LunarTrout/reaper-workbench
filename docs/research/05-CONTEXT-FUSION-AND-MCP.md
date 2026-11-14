# Сабагент 5 — единый контекст, evidence fusion и MCP для ИИ

## Решение, для которого нужен ответ

Как собрать всё, что известно о звуке и проекте, в максимально полный, актуальный и понятный модели контекст, не превращая его в несвязанный текст или каталог из сотен одинаково видимых инструментов? Как объединить все анализаторы в удобный общий результат, чтобы LLM могла и сама увидеть всю картину, и раскрыть детали?

## Что исследовать в нашем проекте

- MCP tools/resources/prompts, project snapshot/revision, reaper_analyze, edit map, reference/analysis resources, API catalog/search and generic ReaScript calls.
- Документы AI-INTERFACE.ru.md, MCP-README-LLM, AI-MIX-SURFACE, GAP-ANALYSIS, README, VALIDATION; проверить расхождения между README, кодом, зарегистрированными tools и живым MCP server.
- Формат audio URI, expiry/release, source identifiers, time coordinates, structured output, model-specific analysis, model version/input provenance.
- Убедиться, что full project/audio context включает master, tracks, FX/routing, fader/pan/mutes, automation/MIDI, takes/items/source offsets, tempo map, selection and edits; отметить фактические omissions.

## Вопросы внешнего исследования

1. Как современные LLM/MCP-системы лучше организуют audio modality, multimodal resources, context manifests, progressive disclosure, tool discovery and follow-up querying?
2. Каков оптимальный schema единого listening report: project revision, track IDs, synchronized audio resources, audio capture point, DSP features, music events, sections, reference links, model observations, confidence, alternatives and missing-data declarations?
3. Как объединять несовместимые единицы и разный уровень доказательности, не усредняя неподходящие outputs? Как хранить lineage от финального утверждения до wav/time/method/version?
4. Как сделать полноту контекста явной: inventory of sources expected/received, timeline coverage, freshness, capture integrity, available/unavailable analyses, known blind spots?
5. Как добиться, чтобы модель не трактовала отсутствие данных как отрицательный факт и не продолжала при stale revision, missing audio или неверной capture stage?
6. Как дать доступ к сотням ReaScript функций, сохраняя широкую гибкость: capability domains, exact structured search, catalog resources, typed safety boundaries, direct generic API calls and documented formulations?
7. Какие модели аудио-обработки MCP host actually forwards to the caller, что остаётся URI, и как это проверить на конкретном клиенте?
8. Как проектировать README, prompts и tool descriptions так, чтобы текстовая модель понимала контекст и выбирала нужную возможность без обязательного task-specific pipeline?

## Результат

Предложить data contract для единого отчёта/analysis ID, модель доступности audio resources, правила fusion/provenance/uncertainty, механизм полноты контекста и LLM-facing navigation. Показать, какие текущие MCP-функции переиспользовать, какие расширить и какие new outputs действительно необходимы. Не создавать лишний инструмент, если тот же capability можно добавить в существующий integrated analysis.

