# R3-01 — план реализации звукового свидетельства и моделей-слушателей

## Владелец направления

Перевести R2-01 в кодовый план для верного получения, описания и доставки аудио-свидетельств. Главный потребитель результата — единый контекст LLM; основные downstream-потребители — R3-02 (события) и R3-04 (микс/референс). Передача sound lineage и повторного capture возвращается через R3-05.

## Прочитать

00-IMPLEMENTATION-READINESS-CONTRACT.md; все R2-01…R2-06 финалы; все R3-01…R3-06 брифы. Затем фактические реализации и тесты, как минимум: src/reaper_audio/analysis/audio.py, bundle.py, artifacts.py, flamingo.py, ossicle.py, metrics.py, phase.py, timeline_context.py; src/reaper_audio/reaper/bridge_client.py; resident Lua bridge, relevant render/capture methods; tests/test_analysis_bundle.py, test_artifacts.py, test_ossicle_stereo.py, test_phase_robust_analysis.py, test_bridge_client.py плюс реально найденные связанные tests.

## Что нужно решить

1. Проследить текущие capture entry points от MCP/Python вызова до Lua/ReaScript и звуковых байтов. Составить матрицу фактических stage/source/capture_method: item/take, track, folder/bus/send/receive, master, pre/post-FX, fader/pan, mute/solo, PDC, channel layout и tail. Особо не смешать take_sum_pre_fx, actual track render, DI и master render.
2. Предложить стабильные интерфейсы Asset/Capture/ListenRequest/ListenResult: project revision, object GUID, source_kind, capture_method, stage, stereo/channel view, sample rate, project/file/sample windows, latency/PDC alignment, settings, hash, lease/lifecycle и доказательство фактической доставки аудио listener-провайдеру. Сверить поля с общим контрактом и конкретными потребителями.
3. Разделить DSP measurements, embedded audio delivered to model, model transcript/description, human annotation и audio-to-audio comparison. Для каждого указать, какие свойства выводимы, а какие нет. Ossicle/Music Flamingo роли из R2 сохранять как проверяемые назначения, не объявлять судьями качества.
4. Спроектировать компактное многомасштабное звуковое представление: полная карта проекта плюс синхронные windows/sections с дорожками и master. Обосновать уровень/стерео-представление и возможность drill-down. Не предлагать одну mono склейку без проверки пространственной потери.
5. Сопоставить R2 capture findings с текущим кодом, указать точные переиспользуемые функции и безопасные границы новых модулей. Отметить конкретные расхождения между docs/ARCHITECTURE.md и MCP-README-LLM.md при описании capture.

## Coding plan и критерии

Каждая задача в backlog должна назвать точные file::symbol, меняемую семантику, кто потребляет результат и совместимость. Разбить работу на: canonical evidence types; capture adapter/stage mapping; transaction/restore/cleanup; audio assets/leases; listener adapter и delivery acknowledgement; calibration/quality report. Не дублировать существующий bundle/artifact слой, если найдено его точное расширение.

План тестов (только описать): known-signal equivalence; all-track + master with master FX; separate pre/post stages; fader/pan/solo/mute/routing; PDC and tail; mono/stereo/multichannel; source/hash/revision invalidation; cleanup and no accumulation; actual MCP AudioContent delivery and explicit failed delivery. Для будущей live проверки определить допустимую ошибку/align tolerance и как исключить routing leak. Не выбирать пороги без обоснования; не запускать тесты или render сейчас.

## Передать соседям

R3-02 получает идентичность источника, stage, aligned stereo evidence windows, latency and channel metadata. R3-04 получает те же stage/time/level semantics для сопоставимого сравнения. R3-05 получает канонические asset/evidence IDs, lease, project revision, provenance и delivery status. R3-06 получает спецификацию capture как side-effecting transaction с restore/readback/cleanup. Уточнить у R3-05, как сохраняется крупный audio payload без вложения всех байтов в контекст.

