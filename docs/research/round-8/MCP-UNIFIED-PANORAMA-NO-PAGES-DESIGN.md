# Единая панорама MCP без обязательных страниц: дизайн и проверка текущего контракта

**Статус:** design research + implementation verification. The report started as design-only; the worktree now contains the lean overview, explicit exact-family catalogue, precision policy and read-only `read_scope`. The archived lifecycle/MCP path was exercised in-process; a live REAPER session and a live stdio MCP client were **not** run in this cycle.
**Дата:** 2026-11-14 (UTC+03:00). **Репозиторий:** `reaper-audio-library`, ветка `main`, HEAD `86b14bb`, worktree dirty.
**Режим:** исходное исследование — read-only по коду и архиву; текущая проверка — in-process на archived fixture. Пользовательский проект не открывался, живой REAPER/MCP не запускался. Все числа — измерения UTF-8 JSON на архиве `work/benchmark/himmp/capture-archive/captures/cap_01M79659V69NDC5YNAA0SMPX51` (41 трек + master, 42 источника, окно 20 с) и на синтетической проекции. Токены нигде не измерены токенизатором: приведены только пересчёты по явно указанным коэффициентам.

---

## 1. Резюме решения

**Рекомендация:** сохранить один вызов `reaper_panorama_open`, который возвращает один self-describing **обзор всего проекта**, но формально разделить в нём два класса данных:

1. **Обзор, обязательный в первом ответе:** все треки и master с устойчивыми ID; все 24 скалярные метрики для каждого источника; временные уровни 300 с и 30 с как объявленные агрегаты; количество и вид всех items/takes/MIDI/automation/FX/routes/audio; виды и счётчики связей; полнота, покрытие, происхождение и каталог точных семейств.
2. **Точные строки, адресуемые по ID:** ячейки рядов, рёбра и пары связей, MIDI-ноты, точки огибающих, полные параметры FX, полные строки analysis/items/takes/audio. Они возвращаются одним семантическим запросом `reaper_panorama_query` целиком или измеренным отказом. Это не страницы: нет курсора, нет порядка, нет «части N из M».

**Baseline at audit vs verified implementation.** At the original audit the full view did not fit; the current implementation delivers the lean overview below. The historical baseline was **47,540 B view / 49,395 B envelope / `response_over_budget` at 48,000 B**; it is retained only as the audit baseline. The current verified result is:

| величина | значение |
|---|---:|
| канонический документ `panorama.json` | **1 378 352 B** |
| lean overview (реальный `record_path`) | **43 576 B** |
| полный envelope `PanoramaLifecycle.single` | **45 431 B** |
| бюджет по умолчанию `DEFAULT_RESPONSE_BYTES` | **48 000 B** |
| результат `single` при 48 000 B | **`complete`**, 42 источника, 24 metrics, `view_mode: lean_overview` |

Это текущий воспроизводимый результат: lean-обзор сохраняет все 42 mapped source и все 24 scalar-метрики, а точные item/take/audio rows каталогизированы для semantic query. Inline scalar values явно округлены (`analysis.precision.lossless = false`); exact query возвращает canonical unrounded values. Заявленные в `docs/DECISION-single-response-panorama.md` старые 42 864/44 719 — исторические числа; текущий lean-обзор (43 576/45 431) и исходный full view (47 540/49 395) — разные представления. Исходный full view требовал бюджет больше 48 KB; lean-проекция и read_scope закрывают этот ранее открытый случай.

**Статус этого измерения:** 42-source frozen measurement теперь воспроизводится regression-тестом `tests/test_archived_single_view.py` (lean envelope ≤48 000 B, 24 metrics, 42 mapped sources, legacy positional rows, exact query, read_scope/read-only). Сам отчёт остаётся одним прогоном; acceptance — тест. Добавлен synthetic 100-track/101-source regression: lean view >48 KB, `response_over_budget` без view/receipt/token, read_scope exact query и `query_too_large` без partial data. Это CI gate для refusal/read-only пути; это **не** заменяет реальный user project или live DAW.

**Второй вывод:** «вся панорама + все данные без страниц для любого размера проекта и любого harness» физически недостижима. Протокол MCP не задаёт лимит размера tool result и не задаёт семантику усечения, но каждый host задаёт свой лимит и по-разному заменяет большой результат (Claude Code — файлом после 50 000 символов, Hermes — превью 1 500 символов, Codex — per-tool token budget). JSON-текст растёт линейно по числу сущностей и метрик; 24 метрики × 100 треков уже дают ~72 KB даже в «лёгком» обзоре без items/takes. Честная граница при 48 KB — примерно 50–56 источников при текущей плотности. Для 100 треков нужен либо явно переданный host-cap (~96 KB для 100 треков/93 item), либо структурированный отказ; для 100 треков/400 item — ~144 KB. Никакая схема не сделает 100 треков с 24 метриками и полным инвентарём в 48 KB без потери обязательных фактов.

**Третий вывод:** «все метрики» в первом ответе — это **24 скалярные семьи на источник**, а не все временные ячейки. Точные ряды, рёбра связей, MIDI-ноты и точки огибающих остаются query-backed. Это соответствует разрешённому владельцем компромиссу «компактное представление со ссылками для событий/рядов», но должно быть явно написано в контракте и в первом ответе: `inline_families` / `catalogued_families`, а не молчаливое исчезновение.

**Что уже реализовано и что осталось:** реализованы lean overview (43,576/45,431), `content_index`, явные `inline_families`/`catalogued_families`, exact item/take/audio selectors, read-only `read_scope` после refusal, явная precision policy (`lossless=false`, exact query), legacy positional-metric mapping и archived regression tests. Остаются вне P0: per-metric method/version descriptors, `query.scope` point counts, `capture_read` capacity, delta comparability и полный docs sync. Это не отменяет текущий verified результат: 42-source / 24-metric open проходит default 48 KB.

---

## 2. Что сейчас есть в проекте: точные пути, функции, тесты

### 2.1 Модельная поверхность

`src/reaper_audio/mcp_server.py`: **27** декораторов `@server.tool()`, **6** `@server.resource(...)`. Панорамные инструменты:

| инструмент | функция | что делает |
|---|---|---|
| `reaper_panorama_open` | `mcp_server.reaper_panorama_open` (около 2684) → `PanoramaLifecycle.single` | один view + receipt + context_token, либо `response_over_budget` |
| `reaper_panorama_query` | `mcp_server.reaper_panorama_query` (около 2715) → `PanoramaLifecycle.query` → `query.run_query` | точный семантический фрагмент |
| `reaper_panorama_status` | `PanoramaLifecycle.status` | последняя ревизия/состояние handle |
| `reaper_panorama_changes_since` | `PanoramaLifecycle.changes_since` | delta от точной базы |
| `reaper_panorama_refresh` | `PanoramaLifecycle.single` | повторная выдача view |
| `reaper_capture_snapshot` | `daw.full_capture`/`mcp_server` | создать capture, вернуть только метаданные архива |
| `reaper_capture_read` | `mcp_server.reaper_capture_read` | чтение архива только с receipt на exact capture |

Страничных инструментов (`reaper_panorama_page`, `_ack`, `_detail`, `_slice`, `_delta_ack`) на поверхности **нет**. Строки с этими именами остались только в приватных `PanoramaLifecycle.open/page/ack` и в старых тестах; `reaper_capabilities(mode="domain", offset=..., limit=...)` — это браузер каталога ReaScript API, а не панорама, и его `offset/limit` не является страницами проектного контекста.

### 2.2 Проекция view

`src/reaper_audio/context/panorama_view.py`:

| функция/константа | роль |
|---|---|
| `VIEW_SCHEMA = "reaper-workbench.panorama-view/2"` | схема view |
| `DEFAULT_RESPONSE_BYTES = 48_000` | политика одного tool result |
| `INLINE_METRIC_DECIMALS = 3`, `METRIC_DECIMALS` | общее и per-metric округление |
| `Entities` (`source_index` по `stable_id`, master последним) | словарь идентичностей и FK |
| `_tracks`, `_items`, `_takes`, `_fx`, `_routes`, `_envelopes`, `_audio` | таблицы структуры |
| `_analysis` | 24 метрики, `absent`, `metric_units`, `metric_decimals`, `metric_methods`, `methods`, `stages`, `signal_stages`, `channels`, `windows`, `rms_stats`, `series_contract`, `uncertainty` |
| `_temporal` | `stored`, уровни 300/30 с, `available:false` с причиной |
| `_relations` | все 6 объявленных видов, статусы, counts, `meaning/is_not/method/uncertainty/coverage_rule` |
| `_archive` | `catalog` с `bytes_method = measured | estimate_*`, `exact` |
| `_completeness` | 4 именованных измерения |
| `_reading` | эпистемические слои, точность, бюджет |
| `build_view`, `view_bytes`, `budget_report`, `canonical`, `section_bytes` | сборка/измерение |

### 2.3 Канонический документ и метрики

`src/reaper_audio/context/panorama.py`:

- `ANALYSIS_METRIC_KEYS` (24), `ANALYSIS_METRIC_UNITS`, `ANALYSIS_METRIC_COLUMNS` — около строк 63–91.
- `_activity_grid` (около 810) — не более `INLINE_MAP_BUCKETS = 24` корзин на источник; каждая корзина — среднее `rms_400ms` в интервале; ключи — stable source id.
- `_analysis_view` (около 889) — строка источника: `metrics`, `measured_window`, `stage`, `channel_view`, `rms_stats`, `series` (count/units/value_kind/band_count), `uncertainty` (per-source notes/confidence), `detail` (typed ref).
- `_relations_domain` (около 1510) — `band_dominance`, `track_master_level_relation`, `reference_comparison` (not supplied), `phase_pair` (unavailable), `band_dominance_pair` (evaluation table), `partial_loudness_share` (never emitted).
- `build_panorama` (около 1646) — сборка канонического документа, `document_sha256`, capacity check.

`src/reaper_audio/analysis/metrics.py`: `measure_audio` (около 334) производит 24 `MetricValue`, `curves.rms_400ms_*`, `curves.band_level_*`, `curves.temporal_profile`, `curves.events_2ms`, `curves.series_meta` с `window_s/hop_s/decimation_step`; `_time_levels` (около 86) — 0.4 с окно, 0.1 с шаг; `step = ceil(len/20000)` — прореживание (около 446).

### 2.4 Точный запрос

`src/reaper_audio/context/query.py`:

- `QUERY_SCHEMA = "reaper-workbench.panorama-query/1"`.
- `DOCUMENT_FAMILIES = relations, items, takes, audio, fx, analysis, project_summary`; `DETAIL_FAMILIES = series, midi_notes, envelope_points`.
- `_relation_rows` фильтрует edges по `objects/kinds/bands_hz`; pairs очищаются при `bands_hz`, при `include_pairs=False` или когда `kinds` не содержит `band_dominance`/`band_dominance_pair`; сами пары не имеют band-поля, поэтому band-фильтр их не сужает.
- `_family_filter` — items/takes/audio/analysis/fx/project_summary.
- `_metric_metadata` — `methods`, `metric_units`, `metric_methods`.
- `_project_analysis_rows` — фильтр колонок `metrics`, но сохраняет все прочие поля строки.
- `run_query` — `scope`, `budget`, `query_too_large` с `reason=selection_over_budget | exact_detail_over_budget | exact_detail_point_limit`.

`src/reaper_audio/context/details.py`: `DEFAULT_DETAIL_BYTES = 48_000`, `MAX_DETAIL_POINTS = 20_000`, `series_entry`, `resolve_details`, `_resolve_series`, `_resolve_midi`, `_resolve_envelope`; ряд/ноты/точки хэшируются против manifest (`PanoramaBindingError` при несовпадении).

### 2.5 Lifecycle, delta, идентичность

`src/reaper_audio/context/lifecycle.py`:

- `single` (около 1067) — build view, измерить envelope, при fits выдать receipt/context_token; иначе `_over_budget`.
- `_over_budget` (около 2411) — убрать view/receipt/context_token/handle, вернуть `over_limit` с `sections_bytes`, `families_bytes`, `options`, `no_hidden_fallback`.
- `query` (около 1184) — receipt/handle/revision/fact_root/document_hash gates, затем `run_query`.
- `changes_since` (около 1243) — `stale_base` reasons (`wrong_project`, `root_mismatch`, `identity_discontinuity`, `chain_too_long`, `delta_too_large`), `refresh_required`, auto-advance.
- `_diff_values` (около 389) — позиционные JSON-path операции для capture-to-capture delta.
- `stable_fact_view` (около 233) — удаляет capture-local ids/paths/хеши, оставляет значения и оба уровня.
- `require_receipt` (около 2249) — проверяет capture/project/revision/root/document_hash/identity/audio freshness.

`src/reaper_audio/context/changes.py`: `DELTA_SCHEMA = "reaper-workbench.structural-delta/1"`, `build_structural_delta` (около 546) — mutation delta по объектам (`objects.changed` с `id`), render invalidation, `audio_evidence`, `acknowledgeable`; `delta_ack` вызывается автоматически из `record_change`.

`src/reaper_audio/context/identity.py`: `ProjectIdentity`, `identity_from_mapping`, `identity_gaps`, `same_identity_values`; отсутствующий `instance_id`/`epoch` не считается совпадением.

### 2.6 Тесты (текущие)

- `tests/test_single_view.py`: один view + receipt/token; отсутствие pagination-лексики; все канонические секции размещены; 24 метрики и units; **FK `analysis/audio.source` резолвится** (`test_every_present_source_fk_resolves_to_a_stable_track_or_master`); temporal levels; детерминизм; over-budget ничего не минтит; exact query; unknown object; query over budget; receipt gate; mutation gate; catalog `bytes_method`; отсутствие выдуманных интерпретаций; `document_hash`; temporal aggregation; row units; point-limit refusal.
- `tests/test_panorama_details.py`: typed refs для MIDI/точек/рядов; batch counts; tamper fail-closed; byte budget; lifecycle detail gate; legacy sidecar upgrade; MCP query by meaning; stale handle; envelope round-trip; linked payload change виден в fact view.
- `tests/test_panorama_lifecycle.py`: старые page/ack/slice/delta_ack тесты (внутренний lifecycle), `changes_since`, receipt binding, refresh, mutation gate. Эти тесты не означают, что страницы есть на модели.
- `tests/test_capture_panorama.py`: 24 метрики, `details`, MIDI limit 10 000 + `truncated`, capacity, binding, schema.
- Локально сегодня: `PYTHONPATH=src python -m pytest -q tests/test_single_view.py tests/test_panorama_details.py` → **32 passed**. Полный прогон в контексте задачи — 1029 passed, 5 skipped; я его не повторял.

### 2.7 Что из round-7/round-8 уже неверно, а что осталось

| старое утверждение | текущая проверка |
|---|---|
| `analysis.rows.source` и `audio.rows.source` = `-1` для всех 42 | **Исправлено.** `Entities.source_index` ключуется по `stable_id`; строки 0…41; тест на FK проходит. |
| `temporal_profile` падает `TypeError: mappingproxy` | **Исправлено.** `_json_default` обрабатывает `Mapping`; `temporal_profile` 0–1 с возвращает `query` 14 290 B; 20 с — `query_too_large`, не crash. |
| catalog `series`/`midi_notes`/`envelope_points` измеряет чужой объект | **Исправлено по форме.** `bytes_method` теперь `estimate_from_cell_counts` / `estimate_from_note_count` / `estimate_from_point_count`; `measured` только для реально сериализованных семейств. Оценки занижены (series 20 273 610 против 27 060 069 B), но не выдают себя за измерение. |
| `project_map` возвращает 2.4 KB project block | **Исправлено.** Семейство называется `project_summary`; сырая карта проекта не выдаётся. |
| take `rate/pitch/stretch_n/fx_n` пусты; FX `params` пусты | **Исправлено частично.** Take-поля заполнены; FX `params` inline только при ≤4 параметрах, иначе `params_omitted`; query `fx` возвращает полный список (31 867 B на 16 FX). |
| four completeness dimensions отсутствуют | **Исправлено по форме.** `_completeness` возвращает `project_inventory`, `inline_metrics`, `archive_families`, `exact_delivery`; но predicates слабые (см. §3, G10). |
| 39 значений округляются в 0 | **Исправлено на эталоне.** Per-metric decimals; 0 ложных нулей на 42 источниках; `rounded_to_zero` при этом всегда `[]` (не вычисляется). |
| `reaper_capture_read` падает на 1.35 MB | **Не исправлено.** После валидного exact-capture receipt `_read_capture_panorama` поднимает `PanoramaCapacityError`: sidecar 1 378 352 B > `DEFAULT_MAX_BYTES = 700 000`; catch повторяет `build_panorama` с тем же лимитом. Без receipt MCP-wrapper сначала возвращает validation gate, поэтому это не «всегда падает», а «не может отдать архив даже с receipt». |
| per-measurement provenance полная | **Не исправлено.** `analysis_contract.method.version = null` → view `"unversioned"`; `metric_methods` — все индексы 0 на один descriptor; 7 уникальных per-source notes (290 повторов) не inline; confidence не производится (`not_produced`). |
| `reaper_analyze` требует receipt на exact capture | **Неверно и сейчас.** `_require_interpretation_receipt` не передаёт `expected_capture_id`; exact-capture проверяется только в `_require_capture_read_receipt`. |
| delta сравнивает несравнимое | **Не исправлено.** `changes_since` использует `_diff_values` (позиционные `/analysis/<i>/...`) и безусловно пишет `compare.verdict = "comparable"`. |
| `query.scope` доказывает отсутствие усечения | **Не исправлено для series.** На `rms_400ms` 197 точек `scope.matching_points = returned_points = 0`. |
| старые DECISION/VALIDATION числа | **Устарели.** 42 864 / 44 719 / 1 349 027 / 8 633 / 730 117 — исторические. Текущий lean overview: 43 576 / envelope 45 431 / canonical 1 378 352. Исходный full view 47 540 / 49 395 остаётся историческим замером; он не является текущим model view. |

### 2.8 Расхождения документации с кодом

| документ | утверждение | факт |
|---|---|---|
| `docs/DECISION-single-response-panorama.md` §6 | старые 42 864 / 44 719 | текущий lean 43 576 / envelope 45 431, `status: complete`; полный view 47 540 / 49 395 — исторический замер |
| `docs/VALIDATION.md` | 1 349 027 / 42 864 / 44 719; 8 633 B; 730 117 B; 1028 passed — исторические | 1 378 352 / lean 43 576 / envelope 45 431; 1049 passed, 5 skipped, 1 SciPy warning, 105.41 s; exact query raw; read_scope |
| `docs/FULL-CAPTURE.md` | view и sidecar — одна схема `capture-panorama/2` | view — `panorama-view/2` |
| `docs/ARCHITECTURE.md` | «Acknowledging the exact delta advances the token» | auto-advance в `record_change`; ack не нужен |
| `docs/MCP-README-LLM.md` | `project_map` в query; relation kinds с byte size; 3 decimals для всех; `model_interpretations`/`causal_hypotheses` в view; tool map 20 инструментов | `project_summary`; byte size в kind нет; per-metric decimals; в view только prose `reading.epistemic`; 27 инструментов |
| `docs/MCP-README-LLM.md` | «every project-target analysis require a receipt for that exact capture» | `reaper_analyze` не проверяет capture id |
| `docs/MCP-README-LLM.ru.md` | 799 строк, EN 761; RU содержит нормативный текст, которого нет в EN | перевод не эквивалентен |

---

## 3. Трассировочная матрица: требование → почему → текущее решение → файлы/функции/тесты → разрыв

| # | Требование (источник) | Почему появилось | Текущее решение | Файлы/функции/тесты | Разрыв |
|---|---|---|---|---|---|
| R1 | Не показывать сотни низкоуровневых команд как единственный вход; сохранить полноту REAPER и структурированное обнаружение (`TZ.md` §8; round-6 directive) | Агент не должен тонуть в `tools/list` и не должен терять доступ к ReaScript | 27 типизированных инструментов + `reaper_capabilities` (summary/domain) + `reaper_api_search` + `reaper_api_call`/`reaper_action` | `mcp_server.py` `reaper_capabilities` (около 682), `reaper_api_search`, `reaper_api_call`, `reaper_action`; `tests/test_mcp_server.py` | `reaper_capabilities(mode="domain")` использует `offset/limit` и в docstring говорит «pages through one domain»; это каталог API, не панорама, но формулировка «no page» в DECISION слишком широка |
| R2 | Единая панорама проекта: треки/master, ID, содержимое/состояние, аудиоконтекст, FX, routes/sends, pan/fader, tempo/automation, MIDI, time spans, связи, provenance; не разносить связанное по несвязанным JSON без ID | Предыдущие «манифест + виды» и page-контракт фрагментировали проект; модель делала выводы из части | `build_view` — одна колончатая проекция; `entities` + FK; `analysis`, `temporal`, `relations`, `audio` | `panorama_view.py` `build_view`, `Entities`, `_tracks`, `_items`, `_takes`, `_fx`, `_routes`, `_audio`, `_analysis`, `_temporal`, `_relations`; `tests/test_single_view.py` | Для больших проектов items/takes/audio/fx-строки могут не влезть; контракт пока не объявляет `inline_families`/`catalogued_families`, поэтому «единая панорама» либо падает, либо (в будущем) будет молча неполной |
| R3 | Все существующие метрики доступны и интерпретируемы; не выбрасывать ради компактности; не заменять факты вердиктами; значения ссылаются на объект, окно, канал, стадию, метод/версию, единицы, полноту/уверенность | Метрики — доказательная база; потеря метрики или её контекста делает ответ агента ложным | 24 скалярные колонки inline; units/decimals/absent; `stage` + `signal_stage`; `windows`/`channels`; `method`/`metric_methods`; `relations.kinds`; exact query | `panorama.py` `ANALYSIS_METRIC_KEYS`/`_analysis_view`; `panorama_view._analysis`; `query._metric_metadata`; `tests/test_single_view.py::test_metric_families_are_complete_and_carry_units` | `method.version` = `"unversioned"`; `metric_methods` — один descriptor на все 24; per-source notes не inline; `confidence` не производится; `transient_count` unit mismatch (`events` producer vs `count` declared) |
| R4 | Никакой обязательной многостраничной загрузки/cursor-loop; ссылки на события/ряды допустимы, но не потерей значимых фактов; не называть «без страниц», если понимание требует 10 запросов | Владелец отверг page walk как архитектуру; receipt доказывал доставку, не чтение | `single` + `query`; нет page tools; `response_over_budget` вместо страниц | `lifecycle.single`, `lifecycle._over_budget`, `query.run_query`; `tests/test_single_view.py::test_the_view_contains_no_pagination_vocabulary`, `test_over_budget...` | `scope` не доказывает полноту для series (0 points); `query` может требовать несколько независимых вызовов по семьям; это допустимо, но должно быть объявлено |
| R5 | Первый полный контекст одним понятным действием; дельты от известной ревизии; полную панораму можно запросить снова; не полагаться на гарантии клиента о ресурсах/вложениях | MCP stateless; host не обязан читать resource; модель не должна угадывать | `reaper_panorama_open`; `reaper_panorama_refresh`; `changes_since` + auto-advance | `mcp_server.reaper_panorama_open/refresh`; `lifecycle.single/refresh/changes_since`; `tests/test_single_view.py`, `tests/test_panorama_lifecycle.py` | Эталон не проходит 48 KB; `refresh` фактически `single`; это правильно, но документация говорит о «complete» без оговорки |
| R6 | Безопасная работа по ревизии: модель понимает, к какому состоянию относится действие; видит изменения; не применяет устаревшую правку; preview/validate/apply/undo и постпроверка | Ошибка на устаревшей ревизии — тихая порча проекта | `require_receipt`; `expected_revision`; `stale_context`/`stale_base`; mutation delta; undo | `lifecycle.require_receipt`, `changes_since`; `mcp_server` edit gates; `tests/test_single_view.py::test_single_receipt_unlocks_mutation_gates`, `tests/test_panorama_lifecycle.py` | Capture-to-capture delta позиционный и безусловно `comparable`; numeric before/after может быть выдан для разных method/window/stage |
| R7 | Свободное построение результата: никакого зашитого рецепта сведения/жанровых правил/обязательной очередности; общие принципы | Задача пользователя может быть любой; библиотека — наблюдения/операции, не жанровый pipeline | `reading.epistemic` пуст; `tracks.role` null; relation `meaning/is_not` — авторские caveat-константы; нет score | `panorama_view._reading`; `panorama.epistemic_layers`; `tests/test_single_view.py::test_view_does_not_invent_interpretation` | Вне панорамы `reaper_mix_evidence` строит эвристические severity; это не view, но модель может спутать; нужна явная граница |
| R8 | История/журнал: реальные изменения состояния и измеряемые дельты с привязкой к ревизиям/объектам/окнам; не выдумывать причинность/вердикт | Аудит и откат; нельзя «объяснять» изменение без факта | mutation delta id-keyed; `audio_evidence`; `invalidations`; `operation_facts`; `readback` | `changes.build_structural_delta`, `lifecycle.record_change`; `tests/test_mcp_mutation_delta.py` (если есть в дереве), `tests/test_panorama_lifecycle.py` | Нет `comparability`; capture delta не id-keyed; `relation_count`/`relation_ids_complete` могут быть неполными |
| R9 | Компактность без потери метрик; bytes ≠ tokens; учитывать host limits | 1.33 MB canonical не проходит ни один stock host; 48 KB — политика, не константа протокола | lean overview 43 576 / envelope 45 431; `measured_bytes`, `sections_bytes`, `fits` | `panorama_view.DEFAULT_RESPONSE_BYTES`, `LEAN_OVERVIEW_BYTES`, `budget_report`; `lifecycle.single`/`_over_budget` | 42-source default теперь проходит; exact-all scalars не помещаются, precision явно rounded+query; archived regression test добавлен |
| R10 | Универсальность между проектами и MCP-harness без патчей | Codex/Hermes/Claude Code по-разному обрабатывают большие результаты | tools-only, без resource-preload; `max_response_bytes` параметр | `mcp_server`; docs | Нет явного host-cap профиля; `max_response_bytes` есть, но default/документация не объясняют, что делать на 100 треках |
| R11 | Точные значения доступны, не только агрегаты | Кратковременные события, ноты, точки, рёбра — не восстановимы из средних | `query.run_query` + `details.resolve_details`; hash verification | `query.py`, `details.py`; `tests/test_panorama_details.py` | `series` scope 0 points; `events_2ms` не имеет family/ref; `fx_params` как отдельной family нет (только `fx`); `metrics` фильтр не уменьшает строку |
| R12 | Полнота/покрытие/причины отсутствия | Нельзя выдавать частичный capture за полный | `coverage.domains`, `missing/truncated`, `completeness`, `absent` | `panorama_view._coverage/_completeness`; `panorama.coverage` | `completeness.project_inventory.complete` не сверяет expected/observed; `archive_families.complete = bool(families)`; `exact_delivery.delivered=false` всегда |
| R13 | Двуязычная документация, EN — источник истины | Агент и человек должны читать одно | EN/RU файлы | `docs/MCP-README-LLM.md/.ru.md` | 761 vs 799 строк; RU содержит нормативные фрагменты, отсутствующие в EN |
| R14 | Минимизировать число действий агента | Один open вместо open+page×N+ack | `single` + `query` | `lifecycle.single`, `query` | На 100 треках single отказывает; модель вынуждена менять декомпозицию, а не получать контекст |

### 3.1 Четыре измерения полноты: что реально проверяет код

`panorama_view._completeness` (около 964) возвращает:

- `project_inventory.complete = not missing and not truncated` — не сверяет `counts` с `coverage.domains[].expected/observed`; отсутствующая домена без записи в `missing` пройдёт.
- `inline_metrics.complete = declared == 24 and sources and len(present) == declared and attribution_ok` — не проверяет, что `null` — это именно absent, а не 0; впрочем, строки строятся как `None`.
- `archive_families.complete = bool(families)` — true, если есть хотя бы одна семья; не проверяет, что все заявленные семьи каталогизированы.
- `exact_delivery.delivered = False` — честно; но имя измерения обещает больше, чем проверяет.

Это лучше, чем один `coverage.complete`, но ещё не четыре независимых контракта из round-7. Для приёмки нужны точные predicates.

### 3.2 Parent verification against the current code and measurements (2026-11-14)

The child report was checked against the worktree, not accepted as self-report. The current worktree is dirty, has the data-integrity edits, and the full suite was rerun by the parent: **1029 passed, 5 skipped** (one scipy warning). The key claims:

| # | claim in this report | parent check | verdict |
|---|---|---|---|
| P1 | historical full view on the 42-source archive was 47 540 B / envelope 49 395 B; current lean view is 43 576 / 45 431 | current `PanoramaLifecycle.single` returns `status=complete`, `view_mode=lean_overview`, 42 sources, 24 metrics; historical full view is retained as a separate measurement | **verified; historical full view superseded by lean** |
| P2 | `reaper_capture_read` cannot serve the flagship archive | direct `_read_capture_panorama(...)` on the archive raises `PanoramaCapacityError`: `estimated_bytes=1378338`, `max_bytes=700000`; the MCP wrapper additionally needs an exact-capture receipt | **verified**; the wrapper path itself was not live-tested with a valid 60 KB receipt |
| P3 | source FK is fixed, but a duplicated GUID vector is unnecessary | `Entities.source_index` is keyed by `stable_id` and `analysis/audio` rows produce 0..41; `tests/test_single_view.py::test_every_present_source_fk_resolves_to_a_stable_track_or_master` passes; current payload has `track` + separate `master`, **not** an `entities.source` vector | **verified**; current `entities.note` ("tracks and master are one list") overstates the payload layout and would be replaced by explicit `source_slots` in the design |
| P4 | `query.scope` reports 0 points for series | reproduced: `rms_400ms` one source, 0–1 s returns `status=query`, 197 cells, `scope.matching_points=returned_points=0`, `complete=True` | **verified defect** |
| P5 | capture-to-capture delta is positional and unconditionally comparable | `lifecycle._diff_values` emits `/analysis/<index>/...`; `changes_since` writes `compare.verdict="comparable"` without method/window/stage comparison | **verified defect** |
| P6 | method/version is `unversioned`; per-source notes are not inline | view `analysis.methods[0].version == "unversioned"`; `metric_methods` maps all 24 names to descriptor 0; `analysis.uncertainty.notes == []`; canonical notes remain per-source | **verified defect** |
| P7 | no model-facing page tools remain | 27 `@server.tool()` and 6 `@server.resource(...)`; no page/ack/detail/slice/delta_ack registration in `mcp_server.py` | **verified for the MCP surface**; private `PanoramaLifecycle.open/page/ack` still contains `reaper_panorama_page`/`_ack` strings (e.g. lifecycle.py:891, 942, 1955, 2088), so the stronger claim "no reachable string anywhere" is false |
| P8 | `reaper_analyze` does not require an exact-capture receipt | `reaper_analyze` calls `_require_interpretation_receipt(...)` without `expected_capture_id`; only `_require_capture_read_receipt` binds a capture | **verified** |
| P9 | archived 42-source and 100-track sizes are not automated acceptance evidence | covered by `tests/test_archived_single_view.py` (42-source envelope, 24 metrics, legacy rows, read_scope) and `tests/test_single_view.py::test_100_source_overbudget_issues_read_scope_and_refuses_oversized_query` (100 tracks + master; refusal/read_scope/query_too_large) | **closed for 42-source and synthetic 100-source gate; live DAW/user project still unverified** |
| P10 | external host limits are host behavior, not MCP guarantees | current MCP spec pages checked: tool results have no protocol byte limit; pagination is only for list operations; resources are application-driven; Claude Code docs state 10k warning / 25k token default / 50k-char spill; Codex reference lists per-tool `output_token_limit` and no default | **verified as cited**; actual host behavior on this view and tokenizer counts remain **unverified** |

Two corrections applied to the report as a result: (a) the recommended `entities.source` vector was replaced by `source_slots` + `master`/`track` rows, preserving the numeric FK without duplicating GUIDs; (b) the 42-source byte result is marked as a benchmark backed by `tests/test_archived_single_view.py`; the acceptance criteria now also have a synthetic 100-track/101-source refusal/read_scope gate.

---

## 4. Внешние источники: что стандартизовано, что зависит от harness

Все URL ниже открыты **2026-11-14**; спецификация MCP — **2026-07-28** (`https://modelcontextprotocol.io/specification/latest` редиректит на неё).

| # | источник | конкретный факт | статус |
|---|---|---|---|
| S1 | `https://modelcontextprotocol.io/specification/2026-07-28/server/tools` | Tool result содержит `content[]` (text/image/audio/resource_link/embedded resource) и необязательный `structuredContent`; `outputSchema` необязателен; `resource_link` — URI, «that can be subscribed to or fetched by the client», и такие ссылки «not guaranteed to appear in the results of a `resources/list` request»; `structuredContent` — server-produced JSON, и сервер **SHOULD** продублировать его текстом для совместимости; в разделе «Stateful Tools» прямо сказано, что протокол не имеет state handle, а handle — обычная строка в результате и аргументе | **стандарт** (нормативные MUST/SHOULD, кроме Stateful Tools — non-normative guidance) |
| S2 | `https://modelcontextprotocol.io/specification/2026-07-28/server/resources` | Resources «application-driven, with host applications determining how to incorporate context based on their needs»; чтение — отдельный `resources/read`; `subscribe` — только уведомления `notifications/resources/updated`, не доставка содержимого | **стандарт** |
| S3 | `https://modelcontextprotocol.io/specification/2026-07-28/basic/index` | «MCP is a stateless protocol: all the information needed to process a request is contained in the request itself… no state should be inferred from previous requests»; клиент обязан передавать явный идентификатор для cross-call state | **стандарт** |
| S4 | `https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination` | Пагинация — только для `resources/list`, `resources/templates/list`, `prompts/list`, `tools/list`; курсоры opaque, page size определяет сервер; для одного tool result страницы нет | **стандарт** |
| S5 | `https://modelcontextprotocol.io/specification/2026-07-28/client/sampling` | Sampling **deprecated as of 2026-07-28** (SEP-2577); остаётся в спецификации ≥12 месяцев, новым реализациям не рекомендуется | **стандарт (deprecation)** |
| S6 | `https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation` | Elicitation — client capability (`form`/`url`), требует пользовательского взаимодействия; не является способом доставить модели большой контекст | **стандарт** |
| S7 | `https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions` | `subscriptions/listen` — поток уведомлений; сервер не должен слать типы, которые клиент не запросил; уведомление не переписывает уже доставленный контекст | **стандарт** |
| S8 | `https://code.claude.com/docs/en/mcp-configuration` и `.../mcp.md` | «Claude Code displays a warning when MCP tool output exceeds 10,000 tokens and limits output to 25,000 tokens by default»; `MAX_MCP_OUTPUT_TOKENS` меняет лимит; «successful text results longer than 50,000 characters are saved to a file regardless of this variable»; инструмент может объявить `anthropic/maxResultSizeChars` | **host behavior**, не протокол |
| S9 | `https://www.codex-docs.com/en/docs/config-file/config-reference` | `mcp_servers.<id>.tools.<tool>.output_token_limit` — «Token budget for one MCP tool's output, before the standard 20% serialization allowance»; default в текущей документации не указан | **host behavior**; default — **unverified** |
| S10 | Hermes local `skills/autonomous-ai-agents/hermes-agent/references/context-assembly-and-tool-budgets.md` (0.23.x) | MCP tool results persisted above **50,000 chars**; inline preview **1,500 chars**; `read_resource` — обычный tool result с теми же правилами; `resources/updated` не обновляет view модели; `list_changed` не вставляет контент | **host behavior** (локальная установка) |
| S11 | `https://www.anthropic.com/research/effective-context-engineering-for-ai-agents` | Контекст — конечный бюджет; большие tool results обычно компактируются/очищаются; нужен curated context, а не «отправить всё» | **research guidance**, не спецификация |
| S12 | `https://www.anthropic.com/research/code-execution-with-mcp` | Промежуточные tool results расходуют токены; code execution позволяет вернуть только нужные строки | **research/инженерный паттерн**, требует sandbox |
| S13 | `https://arxiv.org/abs/2307.03172` (Lost in the Middle) | Качество ответа зависит от позиции релевантной информации в длинном контексте; длинный контекст не равен равномерному вниманию | **эмпирическое исследование**, не протокол |

### 4.1 Что из этого следует, а что является выводом

**Установлено стандартом/документацией:**

- В протоколе **нет** лимита размера одного tool result и **нет** семантики truncation; любой лимит — host/transport.
- `resource_link` — адрес, а не контент; хост не обязан его читать; ресурсы application-driven.
- `resources/read` — отдельный вызов; подписка — уведомление, не обновление контекста.
- MCP stateless; cross-call state — только явный handle/id в аргументах.
- Пагинация — для list-операций, не для tool result.
- Sampling deprecated; elicitation — user interaction; subscriptions — notifications.
- Claude Code: 10k warning, 25k default cap, 50k chars file spill.
- Hermes: 50k chars spill, 1.5k preview, `read_resource` обычный tool result.
- Codex: per-tool `output_token_limit`; default не документирован.

**Вывод (inference, не свойство протокола):**

- 48 000 B — политика, а не гарантия. Она ниже 50 000 chars Hermes/Claude Code, но выше неизвестного Codex default и не гарантирует, что модель увидит весь результат.
- Один большой view — единственный переносимый путь; resource link/UI expander/client preload не могут быть обязательными.
- Для проекта, который не влезает, честный ответ — измеренный отказ, а не страница/усечение/скилет.

**Не проверено:** реальный token count view токенизатором конкретной модели; фактическое поведение Codex/Claude Code на этом view; live Hermes spill; default Codex; comprehension-тест модели. Всё это отмечено в §10–11.

---

## 5. Варианты и измерения

Все байты — UTF-8 JSON, измеренные `len(canonical(...))`; token-колонки — **пересчёт**, не измерение токенизатором. Для baseline: 42 источника; synthetic: 100 треков + master = 101 источник, 93 или 400 item, 75 FX, собран из строк реального архива (не live REAPER).

| вариант | что видит модель в первом ответе | bytes (42 источника) | bytes (100/93) | bytes (100/400) | tokens @3.07 / @4 / @2.2 (42 источника) | число вызовов для обзора | риск |
|---|---|---:|---:|---:|---|---|---|
| **(i) Плоская дублирующая JSON-панорама** | каждый трек — объект с metrics/items/takes/fx/sends/relations; всё повторяется | **1 871 880** | не измерено (масштабируется хуже) | не измерено | 609 733 / 467 970 / 850 855 | 1 | не влезает ни в один stock host; дублирование ID/метаданных; truncation/spill |
| **(ii) Полный inline graph/таблицы с ID-словарями (исторический `panorama-view/2`)** | все треки/master, 24 метрики, items/takes, FX, routes, automation summaries, audio, temporal levels, relations kinds, catalog | **47 540** (envelope 49 395) — исторический full view | **84 746** (envelope 86 601) | **135 943** (envelope 137 800) | 15 485 / 11 885 / 21 609 | 1 | full view 42-source требует больше 48 KB; для default теперь используется lean (43 576/45 431); 100 треков — 1.8× |
| **(iii) Snapshot-resource/artifact + query/ref** | только manifest/URI; модель должна прочитать resource или звать query | **6 437** (manifest) | не измерено | не измерено | 2 097 / 1 609 / 2 926 | 1 + чтение resource/query | resource не гарантирован; модель не видит метрики; не переносимо stock host |
| **(iv) «Лёгкий» обзор + exact refs (рекомендуемый)** | все треки/master + 24 метрики + content index (count/extent/takes/midi) + temporal levels + FX/routes/automation summaries + audio summary + relations kinds + catalog; items/takes/audio/fx-строки — exact query | **40 246** (envelope ~42 101) | **72 280** | **72 313** | 13 109 / 10 062 / 18 294 | 1 (обзор) + 0…N только за точными строками | 100 треков всё ещё >48 KB; нужен host-cap ~96 KB; items/takes не inline |
| **(v) Двухчастный ответ: overview + exact query** | то же, что (iv), но контракт явно объявляет `inline_families`/`catalogued_families` | 40 246 + query | 72 280 + query | 72 313 + query | как (iv) | 1 + optional | то же; главное — не выдавать catalogued за inline |

### 5.1 Что именно измерено

- `canonical(panorama)` = **1 378 352 B**. Секции: relations 840 989; analysis 112 223; details 77 697; analysis_temporal 77 222; audio 55 450; crosslinks 45 804; fx 30 293; tracks 26 493; relations_summary 25 479; items 23 309; takes 16 437; routes 11 006; project 2 414.
- Current lean view (`view_mode=lean_overview`) = **43 576 B**; envelope = **45 431 B**, so 42 sources / 24 metrics fit the 48,000-byte default. The historical full view measured **47 540 B** / envelope **49 395 B** and is retained as the comparison baseline; it is not the current delivered model view. Current sections are dominated by analysis, relations, binding, audio, archive and content_index; items/takes are catalogued, not inline.
- 100/93 synthetic: view **84 746 B**, envelope **86 601 B**; 100/400: view **135 943 B**, envelope **137 800 B**; 200/400: view **179 093 B** (envelope не измерен, overhead ~1.9 KB).
- Lean overview: baseline **40 246 B**; 100/93 **72 280 B**; 100/400 **72 313 B**. Lean убирает items/takes/audio-строки и заменяет их per-track content index; 24 метрики, temporal levels, relations, catalog остаются.
- Manifest-only: **6 437 B** (binding + project + counts + catalog + coverage), без метрик.
- Flat duplicate: **1 871 880 B** на 42 источника (включая полные analysis rows и все relation edges в каждом треке).
- Exact series payload: **27 060 069 B** на 42 источника, 56 112 cells (`rms_400ms` 8 274, `band_level` 39 438, `temporal_profile` 8 400); одна `rms_400ms` 197 cells = 5 731 B payload, query 8 878 B.
- Exact queries: `analysis` all 42 = 115 059 B response / 112 223 B family → отказ; one source = 5 725 B; `analysis` 2 metrics = 87 705 B → отказ (фильтр не уменьшает строку); `relations` all = 829 317 B / 827 753 B → отказ; one track = 55 073 B / 53 466 B → отказ; `track_master_level_relation` = 26 605 B → проходит; `series rms` one = 8 878 B; `band_level` one 0–5 с = 128 275 B → отказ; `temporal_profile` 0–1 с = 14 290 B; `takes` all = 18 023 B; `fx` all = 31 867 B; `envelope_points` all = 4 494 B; `midi_notes` = 1 781 B (0 notes); `project_summary` = 4 036 B.
- Tokens @4 B/token: view 11 885, envelope 12 349, lean 10 062. **Даже 48 000/4 = 12 000 > 10 000**, поэтому фраза DECISION «inside Codex ~10,000-token output budget at ~4 bytes/token» арифметически неверна; 44 719/4 = 11 180 — тоже.

### 5.2 Trade-offs

- (i) полная, но не помещается; дублирует ID/метаданные; худший вариант для LLM и host.
- (ii) хорош для 42 источников после ужатия, но `entities` items/takes растут линейно; 100/400 — 135 KB.
- (iii) минимальный первый ответ, но нарушает «модель видит панораму»; resource не гарантирован; подходит только как архивный/аварийный режим.
- (iv)/(v) лучший компромисс: обзор + все 24 метрики + content index + typed refs; 100 треков — 72 KB, что требует host-cap ~96 KB или отказа; не прячет факты, а объявляет, что именно точное.

---

## 6. Рекомендуемая схема snapshot/metrics/relations/delta/query

### 6.1 `reaper-workbench.panorama-overview/3` (первый ответ)

Ключевое отличие от `/2`: явные `inline_families`/`catalogued_families`, **компактный source map без дублирования GUID-вектора**, per-metric descriptors, `content_index`, `budget` в самом ответе.

```jsonc
{
  "schema_version": "reaper-workbench.panorama-overview/3",
  "binding": {
    "capture_id": "cap_...", "project_id": "reaper:{...}:...",
    "revision": {"before": {"change_count": 2, "instance_id": "pri_...", "epoch": 1},
                  "after":  {"change_count": 2, "instance_id": "pri_...", "epoch": 1}},
    "time_window": {"project_start_s": 10.0, "project_end_s": 30.0},
    "record_integrity_sha256": "...", "document_sha256": "...",
    "identity": {"complete": true}, "evidence": {"state": "current"},
    "render_manifest": {"complete": true}
  },
  "completeness": {
    "project_inventory": {"complete": true, "counts": {"tracks": 41, "master": 1, "items": 34,
                          "takes": 34, "fx": 16, "routes": 40, "audio": 42, "analysis": 42},
                          "missing": [], "truncated": []},
    "inline_metrics": {"complete": true, "sources": 42, "declared_metrics": 24,
                       "absent": {"integrated_lufs": 4}, "attribution": "analysis.rows.source -> entities.source_map"},
    "archive_families": {"complete": true, "catalogued": ["series","relations","midi_notes",
                          "envelope_points","items","takes","audio","analysis","fx"],
                          "unavailable": []},
    "exact_delivery": {"delivered": false, "reason": "inline view contains no exact rows"}
  },
  "entities": {
    "track":  [["{GUID}", "1. SubKick"]],
    "master": ["{GUID}", "MASTER"],
    "source_count": 42,
    "source_slots": {"track": [0, 40], "master": 41},
    "source_map": "source i in 0..40 -> entities.track[i]; i == 41 -> entities.master",
    "note": "no duplicated GUID vector; stable id = 'track:'+track[i][0] or 'master:'+master[0]"
  },
  "tracks": {
    "columns": ["source","ch","folder","vol","pan","mute","solo","sel","arm","send_n","recv_n",
                "hw_n","fx_n","item_n","first_s","last_s","take_n","midi_n"],
    "rows": [[0,2,0,0.56,0,0,0,0,0,1,0,0,0,1,0.0,299.479,1,0]],
    "stages": [["selected_track_via_master_parallel_batch_post_fx","native_render",1],
               ["master_mix_post_fx","native_render",1]]
  },
  "content_index": {
    "columns": ["source","item_n","take_n","midi_n","first_s","last_s","take_media_n"],
    "rows": [[0,1,1,0,0.0,299.479,1]],
    "note": "exact item/take rows are query data=['items']/['takes']; this index is the complete per-track content inventory"
  },
  "analysis": {
    "metric_columns": ["clipped_sample_count", "..."],
    "metric_units": {"integrated_lufs": "LUFS", "...": "..."},
    "metric_decimals": {"dc_offset": 8, "mono_fold_loss_db": 6, "stereo_correlation": 4, "...": 3},
    "methods": [{"analysis":"reaper-audio-library","version":"metrics/1","decoder":"libsndfile",
                 "measurement_stage":"file_or_buffer"}],
    "metric_methods": {"integrated_lufs": 0, "peak_dbfs": 0, "...": 0},
    "rows": {
      "columns": ["source","available","method_i","stage_i","signal_stage_i","window_i","channel_i",
                  "...24 metric names..."],
      "rows": [[0,1,0,0,0,0,0, -27.954, "..."]],
      "note": "stage_i is the measurement stage; signal_stage_i indexes tracks.stages; null = absent"
    },
    "windows": [[0.0,20.0]], "channels": ["2_channels"],
    "absent": {"integrated_lufs": 4, "loudness_range_lu": 4, "true_peak_estimate_dbfs": 4,
               "stereo_correlation": 4, "local_tempo_median_bpm": 8},
    "uncertainty": {
      "notes": [{"metric":"true_peak_estimate_dbfs","note":"4x polyphase FIR estimate; not validated for standards compliance"}],
      "confidence_basis": "not_produced"
    },
    "rms_stats": {"columns":["count","min","max","mean","p50"],"units":"dBFS",
                  "rows":[[197,-57.588,-23.203,-31.252,-28.917]]},
    "series_contract": {"meta": {"rms_400ms": {"units":"dBFS","value_kind":"scalar_per_cell",
                          "window_s":0.4,"hop_s":0.1,"decimation_step":1}}}
  },
  "temporal": {
    "stored": {"width_s":0.8167,"anchor_s":0.0,"anchor_basis":"first stored cell","buckets":25,
               "sources":42,"cell_meaning":"each stored cell is the mean of the rms_400ms cells in that bucket"},
    "levels": [{"width_s":300.0,"available":true,"buckets":1,"aggregation":"mean of stored grid cells",
                "decimals":2,"rows":[[42,-31.3,-31.3,-31.3,-31.3]]}],
    "exact": "query data=['series'], series=['rms_400ms'], objects=[source], interval=[a,b)"
  },
  "relations": {
    "complete": true, "evaluated_pairs": 820,
    "kinds": [
      {"kind":"band_dominance","status":"emitted","edges":1039,"units":{"band_delta_db":"dB"},
       "meaning":"...","is_not":["audibility","masking","causation"],"method":"analysis.relations.band_dominance",
       "method_version":"1","stage_relation":"same_stage","uncertainty":{"status":"unknown","reason":"..."},
       "coverage_rule":"..."},
      {"kind":"phase_pair","status":"unavailable","reason":"phase_pair_not_measured_in_capture","edges":0},
      {"kind":"partial_loudness_share","status":"not_measured_in_capture","edges":0}
    ],
    "pair_outcomes": {"evaluated":577,"no_active_band_cells":174,"no_dominant_band":69},
    "unavailable": {"count":176,"reasons":{}},
    "exact": "query data=['relations'], objects=[...], kinds=[...], bands_hz=[...]"
  },
  "archive": {
    "inline_families": ["tracks","analysis","fx","routes","automation","envelopes","audio"],
    "catalogued_families": ["items","takes","series","relations","midi_notes","envelope_points"],
    "catalog": {"columns":["family","rows","row_unit","inline_bytes","exact_bytes","bytes_method","selectors"],
                "rows":[["series",56112,"cells",0,27060069,"measured_json",["objects","series","interval","resolution_s"]]]},
    "exact": {"series": {"cells":56112,"sources":42,"address":"query data=['series'] ..."}}
  },
  "budget": {"measured_bytes": 45120, "max_response_bytes": 48000, "fits": true,
             "sections_bytes": {"analysis":9000,"entities":5000,"temporal":3500},
             "token_estimate_method": "utf8_bytes/2.2 conservative"},
  "reading": {"scope":"whole project at one captured revision","delivery":"one MCP tool result",
              "precision":{"inline_metrics":"per metric_decimals","inline_levels":"2 decimals"},
              "epistemic":{"interpretations":"empty","hypotheses":"empty"}},
  "view_sha256": "..."
}
```

Правила:

1. `entities.track` + `entities.master` — единственные GUID-словари; `source_count` и `source_slots` задают отображение `source` integer → строка. Дублирующего `entities.source` нет: `source` не может быть `-1` для присутствующего объекта, а stable id выводится из GUID. Это сохраняет компактность и не теряет адресацию.
2. `content_index` — полный per-track инвентарь, даже если `items`/`takes` вынесены в query. Это не «страница»: нет порядка и курсора.
3. `inline_families`/`catalogued_families` обязательны; `catalogued` означает «строки доступны точным запросом», а не «потеряны».
4. `analysis.metric_methods` — descriptor per metric, а не один индекс на все; `method.version` не `"unversioned"`, а реальная версия producer.
5. `budget` — измеренный размер именно этого ответа; если `fits=false`, ответа с `view` быть не может.

### 6.2 Query (`reaper-workbench.panorama-query/2`)

```jsonc
{
  "schema_version": "reaper-workbench.panorama-query/2",
  "status": "query",
  "binding": {"capture_id":"cap_...","project_id":"...","revision":{...},
               "document_sha256":"...","fact_root":"..."},
  "selector": {"data":["series"],"objects":["track:{GUID}"],"interval":{"start_s":41,"end_s":73},
               "series":["rms_400ms"],"metrics":null,"kinds":null,"bands_hz":null,
               "resolution_s":null,"include_pairs":true},
  "units": {"rms_400ms":"dBFS","analysis":{"integrated_lufs":"LUFS"}},
  "metric_methods": {"integrated_lufs":{"unit":"LUFS","method":"analysis.metrics.bs1770",
                      "method_version":"1","stage":"file_or_buffer","window_basis":"measured_section",
                      "channel_view":"2_channels","decimals":2}},
  "scope": {"matching_rows":1,"returned_rows":1,"matching_points":197,"returned_points":197,
            "complete":true,"hash_verified":true},
  "data": {"exact_detail": {"results": [{"series": {"rms_400ms": {"count":197,
             "times_s":[0.0,0.1],"values":[-22.0,-21.5]}}}]}},
  "budget": {"measured_bytes":8878,"max_response_bytes":48000,"fits":true,
             "family_bytes":{"exact_detail":8878}},
  "note": "the whole selection; no pages, no cursor, no truncation"
}
```

Отказ:

```jsonc
{
  "status": "query_too_large",
  "reason": "selection_over_budget",
  "binding": {...}, "selector": {...},
  "matching_rows": 42, "matching_points": 56112,
  "bytes_per_point": 44.4, "max_points_for_budget": 1081,
  "min_resolution_s_for_budget": 0.02,
  "measured_bytes": 27138014, "max_response_bytes": 48000,
  "family_bytes": {"exact_detail": 27138014},
  "narrow": ["objects=[...]","interval={...}","series=[...] with resolution_s=..."],
  "note": "rows exist in the archive; this is one response's size, not a missing measurement"
}
```

Правила: `scope.matching_*` считается до среза, `returned_*` после; `complete` true только при равенстве; на отказе нет `data`/`next`/`cursor`; `units` и `metric_methods` есть в каждом ответе, включая отказ по analysis.

### 6.3 Delta (`reaper-workbench.structural-delta/2`)

```jsonc
{
  "schema_version": "reaper-workbench.structural-delta/2",
  "base": {"context_token":"ct_...","capture_id":"cap_old","revision":{...},
           "fact_root":"...","document_hash":"..."},
  "target": {"revision":{...},"fact_root":"...","document_hash":"..."},
  "changed": [
    {"kind":"track","id":"track:{GUID}","field":"pan","before":-0.25,"after":0.1},
    {"kind":"analysis","id":"analysis:{GUID}","metric":"peak_dbfs","before":-1.2,"after":-0.8}
  ],
  "added": [{"kind":"item","id":"item:{GUID}"}],
  "removed": [{"kind":"fx","id":"fx:track:{GUID}:2:{GUID}"}],
  "comparability": {"method_match":true,"window_match":true,"stage_match":true,
                     "verdict":"comparable"},
  "invalidated": {"sources":["track:{GUID}"],"audio_ids":["audio:{GUID}"],
                  "analysis_ids":["analysis:{GUID}"],"complete":true,
                  "reason":"render_dependency_changed"},
  "audio_evidence": {"state":"stale"},
  "advance": {"context_token":"ct_...","guarantee":"this exact delta was delivered on this base"}
}
```

Если `verdict != "comparable"` — никаких числовых `analysis`/`relation` before/after; только `invalidated` с причиной (`method_changed`, `window_changed`, `stage_changed`, `identity_discontinuity`). Никаких позиционных `/analysis/17/...` в model-facing delta.

### 6.4 Lifecycle

`initial open → changes_since(context_token) → advance.context_token → explicit refresh` — как сейчас, но:

- `open` возвращает `overview/3` + receipt + token только если `budget.fits`;
- `changes_since` возвращает id-keyed delta и successor token в том же ответе;
- `refresh` — полный повторный `open`, а не page 0;
- stale base: `stale_base` с точной причиной, без операций;
- partial capture: `coverage.incomplete`/`truncated`; view не говорит `complete`;
- over-budget: `response_over_budget` с измеренными секциями и counts, без view/receipt/context_token/full-view handle; допускается только `read_scope` (§6.5); **не** писать `receipt_guarantee` и `next_action` про несуществующий token.


### 6.5 Refusal gap and the current resolution

**Historical defect (now fixed).** `PanoramaLifecycle._over_budget` used to remove every query credential. The current implementation first tries the lean overview; if that still exceeds budget it issues a **read-only `read_scope`** bound to the exact capture/revision/fact root/document hash. `reaper_panorama_query(revision=..., read_scope=...)` accepts it for one exact selection; `require_receipt` and all mutation paths still require a complete full-view receipt and a live identity. `reaper_panorama_query` also supports the normal `handle` + `panorama_receipt` route, and normal reads no longer require complete live identity.

**Required implementation order (design decision; not implemented in this research wave):**

1. **Primary fix — done: the real 42-source overview fits.** The lean projection (`LEAN_OVERVIEW_BYTES=45,000`) delivers 43,576 B / envelope 45,431 B with all 42 sources and 24 scalar columns; item/take/audio rows are explicit exact-query families and `content_index` carries per-track counts/extents. The archived-fixture regression test asserts the envelope stays ≤48,000 B; the historical full view was 47,540/49,395 and is not the current delivered view. Metrics, source/window/stage/channel identity, units, decimals and absent counts are not shaved.
2. **For larger views choose one explicit fallback: (b) read-only selection/query handle from the refusal.** This plan selects (b), not (c), because it adds no second panorama surface and reuses the existing exact-query path. The refusal is the only issuer:

```jsonc
{
  "status": "response_over_budget",
  "reason": "complete_view_too_large",
  "measured_bytes": 49395, "max_response_bytes": 48000,
  "sections_bytes": [{"section": "analysis", "bytes": 11380}],
  "read_scope": {
    "id": "rs_...",                 // not a full-view handle, not a receipt, not a context_token
    "capture_id": "cap_...", "project_id": "reaper:...",
    "revision": { "...": "..." }, "fact_root": "...", "document_hash": "...",
    "permissions": ["read"],
    "allowed": ["reaper_panorama_query"],
    "max_response_bytes": 48000,
    "expires_at": "2026-11-14T10:30:00Z"
  },
  "note": "read-only exact queries only; this does not authorize mutation or a full-project claim"
}
```

   - `reaper_panorama_query` accepts either the existing `handle` + full receipt, or this `read_scope.id`; it binds both to the same capture/revision/fact root/document hash and returns the **whole selected scope** or `query_too_large`.
   - `read_scope` is never accepted by mutation gates (`reaper_edit`, `reaper_api_call`, `reaper_action`, `reaper_save`), never minted as `receipt`/`context_token`, and never sets `delivery.complete=true` for the project. A mutation still requires a delivered full view for the current revision.
   - No `page`, `cursor`, `next_page`, or client reassembly is introduced. The scope is only an authorization binding; selection remains semantic (`objects`, `data`, `series`, `interval`, `metrics`, `kinds`, `bands_hz`).
   - Tests: refusal has no view/receipt/context_token but has a valid read_scope; query with read_scope works; stale/foreign read_scope refused; read_scope cannot authorize edit/API/action/save; read_scope query over budget returns `query_too_large` with counts and no data; restart/TTL expiry invalidates the read_scope; full-view receipt still works unchanged.
3. **Alternative (c), not selected now:** a single-call `reaper_panorama_scope(scope=...)` returning a complete semantic subset with its own `binding` and `completeness.scope` (never a full-project claim). Keep it as an owner-level option only if the read-only handle cannot give the model the overview it needs on large projects. Do not implement both (b) and (c) in the first wave.
4. **Mutation rule (unchanged and non-negotiable):** full-view receipt + current revision/epoch/fact root; a read-only scope handle is never sufficient. The full view remains the only thing that can authorize project-changing operations.


---

## 7. Как все существующие метрики входят в представление и сохраняют provenance

### 7.1 24 скалярные метрики

Объявлены в `panorama.ANALYSIS_METRIC_KEYS` (24), единицы — `ANALYSIS_METRIC_UNITS`; producer — `analysis.metrics.measure_audio`. В view:

- `analysis.metric_columns` — все 24, включая отсутствующие;
- `analysis.metric_units` — 24/24;
- `analysis.metric_decimals` — per-metric (dc_offset 8, mono_fold_loss_db 6, clipped_sample_ratio 8, stereo_correlation 4, modulation_depth 4, остальные 3);
- `analysis.rows.columns` — `source, available, stage, signal_stage, window, channel, ...24...`;
- `analysis.absent` — число `null` по каждой метрике;
- `analysis.stages` — стадия измерения (`file_or_buffer`);
- `analysis.signal_stages` — фактическая стадия сигнала (`selected_track_via_master_parallel_batch_post_fx`, `master_mix_post_fx`);
- `analysis.windows` — измеренные окна;
- `analysis.channels` — channel view;
- `analysis.methods` / `metric_methods` — метод/версия/decoder;
- `analysis.uncertainty` — `confidence_basis: not_produced`; per-source notes пока не inline;
- exact query `analysis` возвращает немасштабированные значения, полный `rms_stats` (median/p05/p95), `series`, `uncertainty.notes`.

**Что не выполнено:** `analysis_contract.method.version = null` → `"unversioned"`; `metric_methods` — один descriptor на все метрики; per-source notes (7 уникальных, 290 повторов) не попадают в view, только в `epistemic_layers.uncertainty.measurement_notes` как count; `transient_count` unit mismatch; `rounded_to_zero` всегда `[]`.

### 7.2 Временные ряды (3)

| ряд | inline | exact |
|---|---|---|
| `rms_400ms` | `analysis.rms_stats` (count/min/max/mean/p50), `analysis.series_contract.meta`; `temporal.levels` — агрегаты | query `data=['series'], series=['rms_400ms']`, interval/resolution; hash verified |
| `band_level` | только counts/units/band_count в descriptor | query `series=['band_level']`; 28 полос; 1 cell = 28 floats |
| `temporal_profile` | descriptor `value_kind=cell_record_per_cell`, count | query `series=['temporal_profile']`; cell fields; 0–1 с = 14 290 B |

`series_contract.meta` в текущем view не содержит `window_s/hop_s/decimation_step`, потому что канонический `analysis[].series` — сокращённая строка (`count/units/value_kind/band_count`), а `details.entries[].series` тоже не подхватывает `curves.series_meta` (в архиве `curves.series_meta = null`). Это **silent downsample path**: на 20 с `step=1`, но на длинном треке `step>1` не объявлен. Нужно писать `window_s/hop_s/decimation_step/cell_fields` в descriptor и в detail entry.

### 7.3 Отношения (6 видов)

`relations.kinds` содержит все 6:

- `band_dominance` — emitted 1 039, method/version 1, units (band_delta_db/broadband_delta_db/active_fraction), meaning/is_not/uncertainty/coverage_rule;
- `track_master_level_relation` — emitted 41, method/version 1, units, caveat (не фейдер, не причинность, не quality);
- `band_dominance_pair` — `evaluated_pairs`, 820 pairs, outcomes 577/174/69;
- `reference_comparison` — unavailable `no_reference_supplied`;
- `phase_pair` — unavailable `phase_pair_not_measured_in_capture`;
- `partial_loudness_share` — `not_measured_in_capture`.

Edges/pairs не inline; query `relations` возвращает их целиком или отказ. В view нет byte size у kind (документация обещает — расхождение); `coverage_rule` и `uncertainty` есть.

### 7.4 MIDI, automation, envelopes, items, takes, FX, audio

| семья | inline | exact | provenance |
|---|---|---|---|
| MIDI | `midi` строка: take, note_n, start/end; 0 на эталоне | `midi_notes` query, 10 000 note cap; notes с duration/PPQ | take id, note count, truncation flag; `coverage.midi` |
| automation | `automation` строка: owner, kind, name, point_n, interval | `envelope_points` query | owner id, point count, interval; `complete/items_scanned/gaps` |
| envelopes | то же; **tempo map дублируется** в `automation` и `envelopes` с двумя id | `envelope_points`; 2 entries × 3 points на эталоне | duplicate id — gap |
| items | geometry: track, start/end, pos/len/off, rate, mute, loop, fades, ch_mode, take_n, take | `items` query; media path | track/take FK; exact geometry |
| takes | item, track, midi, rate, pitch, stretch_n, fx_n, ch_mode, group | `takes` query; media path | take id; fields captured |
| FX | owner, slot, name, on, offline, param_n, params (≤4), params_omitted | `fx` query full parameters | `raw_parameters_pointer`; full list query |
| audio | source, master, stage, rev, rev_change, frames, sr, ch, dur, dur_ok, mapped_end, tail_n, tail, sha16, analysis_i | `audio` query full mapping/hash | stage/method/revision; `audio.stages/revisions` |
| rms_stats | count/min/max/mean/p50 | analysis query full median/p05/p95 | basis `rms_400ms cells`; units dBFS |
| 2 ms events | только counts (`two_ms_event_count`, onset/spike/clip) | **нет family/ref** | stored `curves.events_2ms`, no address — gap |

### 7.5 Полнота, валидность, уверенность

- `binding` — capture/project/revision/record integrity/document hash/identity/evidence/render manifest.
- `coverage.domains` — expected/observed/status/truncated по 10 доменам; `missing`, `truncated`, `silent_drop`.
- `completeness` — 4 имени, но predicates слабые (§3.1).
- `uncertainty.confidence_basis = not_produced` — честно; per-metric confidence нет.
- `audio_evidence.state` (`current`/`stale`) отдельно от структуры; stale audio не делает structural delta свежим.
- `reading.epistemic` — interpretations/hypotheses пусты; `tracks.role` null; нет score/quality.

---

## 8. Почему модель видит полный обзор в первом вызове, что адресуемо и почему это не скрытая пагинация

### 8.1 Что гарантированно в первом вызове (для проекта, который проходит бюджет)

- Все треки и master: имя, stable id, channel/folder/vol/pan/mute/solo/arm, send/recv/hw counts, fx_n, item_n, audio_i/analysis_i.
- Все 24 скалярные метрики для каждого источника, с units/decimals/absent; строки привязаны к source.
- Все items/takes (на 42-источниках): геометрия, take-поля.
- FX: имена/слоты/on/offline/param_n; params для ≤4.
- Routes/sends, automation/envelopes summaries, tempo map/signatures/markers/regions.
- Audio: source/master/stage/frames/sr/ch/dur/tail/sha prefix; stages/revisions.
- Temporal: stored grid geometry + уровни 300/30 с (агрегаты).
- Relations: все 6 видов, counts, meanings, caveats, coverage.
- Archive catalog: counts, units, inline/exact bytes, selectors; `completeness`; `coverage`.

Для 42-источников это **полный обзор**. Для проекта, где items/takes не влезают, первый вызов всё равно содержит `content_index` (count/extent/takes/midi) и `catalogued_families`, но не сами строки; это объявленный exact ref, а не страница.

### 8.2 Что адресуемо точным запросом

- Ячейки `rms_400ms`, `band_level`, `temporal_profile` — по source + interval + resolution.
- Рёбра/пары связей — по objects/kinds/bands/interval.
- MIDI-ноты — по take id + interval.
- Точки automation/envelopes — по track id + interval.
- Полные `analysis` строки (unrounded, median/p05/p95, notes) — по source/metrics.
- `items`/`takes`/`audio` — по object id.
- Полные FX parameters — по owner/fx.
- `project_summary` — project block; сырая карта проекта не выдаётся.

### 8.3 Почему это не скрытая пагинация

1. **Нет курсора/страницы/порядка.** `ARCHIVE_FAMILIES` и `run_query` не имеют `page`, `cursor`, `offset`, `next`; ответ — весь выбранный фрагмент или `query_too_large`.
2. **Первый вызов содержит все факты, нужные для понимания проекта и всех метрик.** Модель может ответить на «кто самый громкий/тихий», «какой master LUFS», «у кого нет LUFS», «сколько item у трека», «какие связи и с каким caveat» без единого query.
3. **Точный query нужен не для сборки панорамы, а для точного значения/ячейки/ноты/точки.** Это адресация, а не «часть N».
4. **Нет обязательной последовательности.** Любой query можно вызвать первым/единственным; нет зависимости «сначала страница 0, потом 1».
5. **Отказ измерен и честен.** `query_too_large` содержит `matching_rows/matching_points`, `measured_bytes`, `family_bytes`, `narrow`; нет частичных `data`.

**Оговорка, которую нельзя скрывать:** для 100 треков при 48 KB первый вызов не проходит; это не «скрытая пагинация», а физический отказ. При 96 KB `overview`/full view для 100/93 проходит; для 100/400 нужен ~144 KB. Если владелец считает каждую item-строку обязательной в первом вызове, то 100 треков/400 item в 48 KB невозможны; это решение владельца, а не дефект форматтера.

### 8.4 Проверка «владелец видит целостную картину сразу»

Я проверил **наличие полей**, а не понимание моделью:

- highest crest: `analysis.rows` + `tracks` names — да;
- master LUFS: последняя строка tracks + `integrated_lufs` — да;
- missing LUFS: `analysis.absent` + null rows — да;
- item count/extent: `tracks.item_n/first_s/last_s` — да;
- relation kinds/caveats: `relations.kinds` — да;
- FX chain names/param counts: `fx` — да; values >4 params — нет, query;
- automation names/point counts: да; point shapes — query;
- MIDI notes: нет на эталоне; для MIDI-проекта — summary + query;
- «когда в песне»: temporal levels дают только агрегаты; точное время — query.

Это соответствует «обзор + все скалярные метрики + адресуемая точность», но **не** «все временные ячейки в первом ответе». Формулировка «все метрики доставлены» должна означать 24 скалярные семьи, а не 56 112 cells.

---

## 9. Физические границы и честное поведение при превышении

### 9.1 Что именно ограничивает

- **Протокол:** лимита нет; host сам решает, что делать с большим результатом.
- **Claude Code:** 10k warning, 25k tokens default, 50k chars file spill; `anthropic/maxResultSizeChars` может изменить.
- **Hermes:** 50k chars spill → `<persisted-output>` + 1.5k preview + path; `read_resource` обычный tool result; subscriptions не обновляют view.
- **Codex:** per-tool `output_token_limit`; default не документирован (unverified).
- **JSON:** размер линейно растёт от числа источников × метрик, items, takes, entities; нет negotiated compression; `structuredContent` может дублировать text; base64/binary не читается текстовой моделью.
- **Модель:** контекст конечен; длинный контекст неравномерен (Lost in the Middle); compaction может выбросить view.

### 9.2 Измеренные границы

| проект | view | envelope | 48 KB | 96 KB | 144 KB |
|---|---:|---:|---|---|---|
| 42 источника (эталон, текущий lean) | 43 576 | 45 431 | **проходит** | проходит | проходит |
| 42 источника (исторический full view) | 47 540 | 49 395 | отказ | проходит | проходит |
| 100 треков / 93 item | 84 746 | 86 601 | отказ | проходит | проходит |
| 100 треков / 400 item | 135 943 | 137 800 | отказ | отказ | проходит |
| lean overview 100/93 | 72 280 | ~74 135 (est.) | отказ | проходит | проходит |
| lean overview 100/400 | 72 313 | ~74 168 (est.) | отказ | проходит | проходит |

(Envelope для lean — оценка view + 1 855 B overhead, измеренного на эталоне; точный lifecycle для synthetic не запускался.)

**Вывод:** при 48 KB практический потолок — ~50–56 источников при текущей плотности; 100 треков с 24 метриками — ~72–85 KB, то есть 1.5–1.8× бюджета. Никакая «безстраничная» схема не отменяет арифметику.

### 9.3 Честная деградация

1. **Полный inline** — если envelope ≤ budget: все семьи inline, `status: complete`.
2. **Overview + exact refs** — если полный inline не влезает, но обзор влезает: все треки/master, 24 метрики, content index, temporal levels, FX/routes/automation summaries, audio summary, relations kinds, catalog; items/takes/audio/fx rows — `catalogued_families`; `completeness.exact_delivery.delivered=false` с причиной.
3. **Отказ** — если даже обзор не влезает: `response_over_budget`, никакого view/receipt/context_token/full-view handle; `over_limit` с `measured_bytes`, `sections_bytes`, `families_bytes`, `counts`, `read_scope` (read-only, см. §6.5) и `options`:
   - поднять `max_response_bytes`, если host реально принимает;
   - использовать `read_scope.id` только для `reaper_panorama_query`; это read-only exact query, не полноэкранная панорама и не разрешение на mutation;
   - либо запросить более узкий capture / (в будущем) semantic-scope overview;
   - **не** вызывать `reaper_panorama_query` без `read_scope`/полного receipt: после отказа у модели нет handle, и query-gate обязан отказать;
   - capture shorter section (структура проекта не уменьшится);
   - не выдавать skeleton/link/URI за панораму.

4. **Ошибки:** `panorama_binding_invalid` (sidecar не связан с record), `panorama_capacity_exceeded` (canonical > 700 KB), `stale_context`/`stale_base` (ревизия/identity/root), `query_too_large` (точечный лимит/байты), `media_unresolved`/`capture_master_invalid` (capture), `validation_error` (unknown object/family/metric/kind). Все — с counts/reason/narrowing, без молчаливого усечения.

5. **`capture_read`:** сейчас на эталоне падает `PanoramaCapacityError`; это не честная деградация, а мёртвый инструмент. Либо убрать с model surface, либо вернуть overview, либо поднять внутренний лимит и вернуть структурированный отказ.

### 9.4 Почему нельзя обещать «любой harness»

Разные hosts:
- Hermes заменяет >50k chars на 1.5k preview — модель не видит остальное без `read_file`.
- Claude Code сохраняет >50k chars в файл; предупреждает >10k tokens; 25k default.
- Codex может иметь другой default; per-tool limit настраивается, но неизвестен.
- Resource link не гарантирован; `structuredContent` может дублироваться; sampling deprecated; elicitation требует пользователя; subscriptions не вставляют контент.

Поэтому контракт может обещать только: «мы измерили ответ и либо доставили его целиком, либо вернули измеренный отказ». Он не может обещать, что host не усечёт, что модель прочитает, или что 100 треков влезут в 48 KB.

---

## 10. Риски, анти-паттерны, критерии приёмки

### 10.1 Риски

| риск | почему | mitigation |
|---|---|---|
| Бюджетная регрессия | Добавление `completeness`, `metric_methods`, decimals уже вывело эталон за 48 KB; нет теста на реальном архиве | Regression-тест на archived 42-source capture; бюджетный gate в CI; `budget.measured_bytes` в ответе |
| Скрытая потеря семейства | `catalogued_families` без явного списка; `archive_families.complete` слабый | Явные `inline_families`/`catalogued_families`; checker: каждая каноническая семья ровно в одном списке |
| `-1`/пустые FK | Старый дефект source; новые поля могут повторить | Checker: каждый не-null FK резолвится; ни одна declared column не пуста на capture с объектом |
| Ложные нули | Глобальное округление 3 decimals | Per-metric decimals; `min_abs_inline`; `rounded_to_zero` вычислять, а не `[]` |
| Ложная сравнимость | `compare.verdict="comparable"` без method/window/stage | `comparability`; запрет numeric delta при mismatch |
| Позиционные delta | `_diff_values` ломается при вставке/переупорядочении | id-keyed ops; property test `apply(base,delta)==target` |
| Второе представление | `capture_read` возвращает canonical 1.38 MB и падает | Убрать/переключить на overview; capacity refusal |
| Host spill | 48 KB политика, не гарантия | Измерять, не обещать; host-cap параметр; документировать |
| Некомпетентность формата | Columnar view не проходил comprehension test (D6-10b) | Task-based проверка: 4 вопроса по view без query; если провал — пересмотреть формат |
| Ложный receipt | Refusal сохраняет `receipt_guarantee`/`next_action` про несуществующий token | На отказе удалять/переписывать эти поля; checker |
| Дублирование envelope | `automation`/`envelopes` один tempo map с двумя id | Один id или `same_as`; dedupe query |
| Неизмеренные 2 ms events | Только counts; нет family/ref | Добавить `events` family или явно `counts_only` |

### 10.2 Анти-паттерны (не делать)

- Обязательные страницы/курсоры/`next_page`/`offset` для панорамы; «часть 1 из N».
- Lossy spine/top-N/«только флаги»/«важные метрики»; скрывать метрики ради байтов.
- Resource link/URI/UI expander как единственный путь к обязательному факту.
- Молчаливое усечение, `null` вместо absent, `0` вместо unmeasured, `-1` для присутствующего объекта.
- Catalog bytes, измеренные на другом объекте; `measured` без измерения.
- Числовая delta между разными method/window/stage; `comparable` по умолчанию.
- Model-facing строка с именем удалённого инструмента.
- Называть отказ «complete»/«delivered»; минтить receipt/token на невыданный view.
- Полагаться на auto-read resources/structuredContent/UI expander.
- Зашитый жанровый рецепт/score/quality verdict.

### 10.3 Критерии приёмки

| id | критерий | как проверить |
|---|---|---|
| A1 | На эталонном 42-source архиве `open` возвращает `status: complete`, `delivery.fits: true`, `view` со всеми 42 track rows, 24 metrics, 0 unmapped | **повторяемый** интеграционный тест на archived capture с проверкой `view_bytes`/`response_bytes` и хэша fixture; если архива нет — тест явно `skip` с `unverified`, а не «зелёный» |
| A1b | Бюджет и токенизация воспроизводятся автоматически, а не одним ручным прогоном | regression-тест на 41/42-source fixture и отдельный 100-track synthetic fixture: измерять `build_view`/`single`/`canonical` bytes; если доступен токенизатор — токены, иначе только bytes + `token_estimate_method`; фиксировать baseline и падать при росте без явного решения |
| A2 | Envelope `delivery.response_bytes == len(canonical(response))`; `fits` false ровно при `measured > max` | boundary test |
| A3 | Over-budget: нет `view/receipt/context_token/handle`; нет `receipt_guarantee`/`next_action` про несуществующий token | refusal checker |
| A3b | Refusal dead-end closed: либо 42-source view проходит, либо отказ содержит `read_scope`; `reaper_panorama_query` принимает `read_scope` и возвращает весь exact scope; тот же `read_scope` отвергается mutation gates | archived fixture + mutation-gate matrix |
| A3c | После `response_over_budget` без `read_scope` любой `reaper_panorama_query` обязан вернуть typed refusal, а не validation crash/скрытый успех; README не содержит недостижимого next call | transport test + docs scan |
| A4 | Нет page/cursor/`next_page`/`offset` в model-facing panorama; query не возвращает `next` | scan + schema |
| A5 | Каждый `source`/`*_i` резолвится; нет `-1` для present; ни одна declared column не пуста на capture с объектом | fixture + checker |
| A6 | 24 units, 24 decimals, 24 method descriptors; `method.version != null`; stage/window/channel на каждой строке | fixture |
| A7 | `absent` counts + `null`, никогда `0`; `rounded_to_zero`/`min_abs_inline` отражает фактическое округление | mutation test |
| A8 | `query` success: `scope.returned == matching` для rows и points; `hash_verified` true для exact; отказ — `query_too_large` с measured bytes и без `data` | selector matrix |
| A9 | `series` все 3 семьи round-trip; `temporal_profile` не падает; `scope` points не 0 | tests |
| A10 | Catalog: `bytes_method=measured` только для реально сериализованного объекта; estimate назван и не меньше/не больше выдаёт себя за measured | checker |
| A11 | Delta: id-keyed; reorder не даёт ложных changes; `not_comparable` при method/window/stage mismatch; stale base reasons | property test |
| A12 | 100-track synthetic: либо complete в declared budget, либо `response_over_budget` с measured counts; нет partial view | **повторяемый** synthetic fixture в тестах (не ручная проекция), фиксированный seed/GUID-набор; тот же тест проверяет bytes и, если доступен, токены |
| A13 | `capture_read`: не падает `PanoramaCapacityError`; либо overview, либо structured refusal | archived capture |
| A14 | Нет model-facing имён `reaper_panorama_(page|ack|detail|slice|delta_ack)` | scan |
| A15 | Документация: все числа с selector/date; DECISION/VALIDATION/README/ARCHITECTURE/FULL-CAPTURE согласованы | docs review |
| A16 | Comprehension: 4 вопроса (highest crest, master LUFS, missing LUFS, item count) отвечаются по view без query | human/model test; **не выполнен** |

---

## 11. Нерешённые решения, требующие владельца

1. **Потолок при 48 KB.** Оставить 48 KB и честно отказывать для >~50–56 источников, или разрешить host-specific `max_response_bytes` (96 KB для 100/93, 144 KB для 100/400) как штатный путь? Это меняет продукт: «один вызов для любого проекта» vs «один вызов для типичного проекта + отказ/явный cap для большого».
2. **Items/takes в первом вызове.** Обязательны ли все item/take строки для «целостной картины», или per-track `content_index` + exact query по треку достаточно? Для 100/400 текущий view 135 943 B; lean overview 72 313 B. Это определение «полной панорамы».
3. **FX parameters inline.** TZ §3 говорит «FX, параметры»; текущий view inline только ≤4, остальное `params_omitted` + query. Какой порог K и допустим ли query для больших цепочек?
4. **MIDI/automation points inline vs summary+query.** Для музыкального понимания MIDI-нот и формы автоматизации может быть недостаточно `note_n`/`point_n`; но полные ноты/точки — query. Это осознанный компромисс или потеря значимого факта?
5. **`reaper_capture_read`.** Убрать с model surface, вернуть overview или оставить архивным инструментом вне модели? Сейчас он падает на флагмане и возвращает 1.38 MB canonical, если бы влез.
6. **`reaper_analyze` exact-capture receipt.** Ужесточить (передавать `expected_capture_id`) или явно объявить, что analysis re-measures live project и receipt нужен только на project/revision/freshness?
7. **`partial_loudness_share`.** Сделать capture-relation (реализовать в `_relations_domain`) или оставить analyze-only вне панорамы? Сейчас вид говорит `not_measured_in_capture`, что честно, но владелец может ожидать её в панораме.
8. **Default budget.** 48 KB (текущий), ниже (~31 KB, чтобы держаться ниже Claude Code 10k-token warning при 3.07 B/token) или выше для больших проектов? Это политика, не протокол.

---

## Приложение A. Воспроизведение измерений

```python
from pathlib import Path
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.panorama import build_panorama
from reaper_audio.context.panorama_view import build_view, view_bytes, canonical
from reaper_audio.context.query import run_query
from reaper_audio.context.lifecycle import PanoramaLifecycle

root = Path("work/benchmark/himmp/capture-archive")
cap = "cap_01M79659V69NDC5YNAA0SMPX51"
rec = CaptureArchive(root).read(cap)
doc = build_panorama(rec, record_path=f"captures/{cap}/record.json",
                     max_bytes=10**12, max_tokens=10**12)
view = build_view(doc, record_path=f"captures/{cap}/record.json")
print(len(canonical(doc)), view_bytes(view))   # 1378352 47540
```

Envelope: current `PanoramaLifecycle(...).single(project_id)` at `max_response_bytes=48000` → `complete`, lean 43,576 B / envelope 45,431 B with 42 sources and 24 metrics. The historical full view at the same budget returned `response_over_budget` (47,540/49,395); it is kept only as a comparison. Tests use `state_root` outside the archive so the raw capture is not modified.

Synthetic 100-track: документ собран из строк реального архива (doubling GUIDs, items/takes/audio/analysis/FX/routes/temporal buckets), `build_view` на нём; это **не** live REAPER capture и не `build_panorama` из нового record. Поэтому synthetic numbers — измерение проекции, а не полного capture pipeline.

## Приложение B. Что осталось непроверенным

- Реальный token count view токенизатором модели; все token-числа — пересчёт по 3.07/4/2.2 B/token.
- Default `output_token_limit` в текущем Codex; документация указывает только override.
- Фактическое поведение Claude Code/Codex/Hermes на 43 576/45 431 B; live host session не запускалась.
- Comprehension-тест D6-10b (4 вопроса по view) — не выполнялся; только проверено наличие полей.
- `reaper_capture_read` с валидным receipt на большом архиве — не проверялся; capacity failure воспроизведён без receipt.
- Полный pytest suite не перезапускался; targeted 32 passed; в контексте задачи — 1029 passed, 5 skipped.
- Synthetic 100-track canonical (2 082 577 / 2 481 366 B) — проекция из строк архива, не `build_panorama` нового record; может отличаться от реального capture.
- `events_2ms` addressability, `series_meta` loss, `transient_count` unit mismatch — код/данные проверены, но не исправлены (design-only).
