# REAPER Workbench — руководство (прочитать один раз за сессию)

Что умеет библиотека, как её вызывать и какие ошибки выглядят разумно, но ломают
работу. `reaper_measure`, `reaper_edit`, `reaper_midi` и `reaper_analyze`
отказывают с `HANDBOOK_REQUIRED`, пока не прочитан `reaper://handbook`; гейт
сбрасывается при изменении этого файла.

## 1. Ментальная модель

Библиотека — **измерительный и монтажный инструмент**, не советчик: она сообщает
факты (уровни, спектры, позиции, маршрутизацию, параметры, квитанции) и не
оценивает звук. Пороги существуют только если владелец положил их в
`policy.json`; они приходят как `violations[]` с `source: "policy"`. Каждое число,
которое вы цитируете, должно происходить из замера, evidence-токена или квитанции.

| Объект | Значение |
|---|---|
| `revision` | `project_id` + `change_count` + `path`. Любая правка меняет её. |
| evidence `token` | Единицы за каждой ячейкой одного ответа `reaper_measure`. |
| `receipt` | Доказательство, что батч правок применён, со значениями read-back. |
| `structural_token` | Дешёвое доказательство, что вы видели карту проекта; разрешает правки. |

## 2. Первые вызовы

```text
reaper_session(action="health")                    # мост и активный проект
reaper_session(action="context", archive_root=...) # единый контекст: карта, health, факты, FX, изменения
reaper_session(action="status", detail="summary")  # ревизия, дорожки, маршрутизация, темп
reaper_session(action="state", ...)                # уже измеренные факты без рендера
reaper_capabilities(mode="tools")                   # 11 групп и их инструменты
reaper_structural_token(project_id=...)             # перед любой мутацией
```

- `context(archive_root, detail="summary|full", max_facts=50, since=token, pages=false)`:
  единственный контекст проекта из `state.json` — проект/ревизия, компактная карта,
  health, сводка фактов, FX, изменения, аудио-ссылки. Предпочитайте его отдельным
  чтениям status/panorama. `since=state_token` отдаёт только
  project/health/changes/next и живую ревизию (сотни токенов), без карты.
  `pages=true` отдаёт страницы и не дублирует полный контекст.
- `state(archive_root, source=..., stage=..., window=..., metrics=..., status=..., since=token)`:
  факты, которые уже измерены, без рендера. `state_detail="full"` — полные строки,
  по умолчанию компактные и только запрошенные метрики.
- `preflight(allow_solo=[...], check_focus=false)` возвращает `blockers`, `warnings`,
  `info` с кодами, без вердикта. `runtime.stale` → `reaper_session(action="app",
  app_command="restart")` до правки.
- Каждый ответ повторяет `mode` (`strict` / `advisory` / `off`).

## 3. Группы

Одиннадцать групп: `session`, `project`, `capture`, `panorama`, `media`, `analysis`,
`measure`, `inspect`, `edit`, `discovery`, `research`.
`reaper_capabilities(mode="tools")` перечисляет инструменты и требуемый контекст.

**session** — `reaper_session` (`action`: `health`, `status`, `context`, `state`,
`preflight`, `transport`, `project`, `settings`, `app`, `wait_for_change`,
`list_instances`, `list_projects`, `dirty`), `reaper_checkpoint_save`,
`reaper_bridge_reload`, `reaper_bridge`, `reaper_mode`, `reaper_structural_token`.

- `transport`: `transport_action="play|stop|pause"`, `position_s`; `app`:
  `app_command="ensure|restart|focus"`; `wait_for_change`: `since={"revision"|"transport": ...}`,
  `timeout_s`.
- `project`: `project_command="open|save|save_as"`, `path`, `overwrite_existing`;
  `settings`: `settings_command="read|write"`.
- `reaper_checkpoint_save` пишет только новый файл; `reaper_save(mode="save_current|save_as", ...)`;
  `reaper_mode` — решение пользователя; `reaper_structural_token(project_id)` — одно
  чтение дайджеста, без рендера.

**project** — `reaper_project` читает полный документ проекта; `reaper_timeline_context`
возвращает темп, маркеры и регионы; `reaper_save` сохраняет текущий проект или новый файл.

**capture** — `reaper_capture_snapshot` создаёт один неизменяемый capture аудио,
привязанный к ревизии; `reaper_capture_read` читает его запись. Capture — основа
архивного анализа и точных строк; для ориентации сначала `session(context)`.

**panorama** — `reaper_panorama_open/page/ack` доставляют полную карту одного
capture, `reaper_panorama_query` — точные строки по смыслу,
`reaper_panorama_changes_since` и `reaper_panorama_refresh` — обновления. Квитанция
панорамы доказывает доставку страниц, но не понимание и не прослушивание.

**measure** — `reaper_measure`, `reaper_freshness`, `reaper_coverage`:

```text
reaper_measure(archive_root=..., project_id=..., sources=[guid, ...] | "all" | "master" |
    [{"kind":"file","path":"ref.wav"}], intervals="all" | "regions" | "bars:8" | [[12,20],[20,28]],
    metrics=["integrated_lufs","peak","crest","bands"], bands="octave" | "third" | [[lo,hi],...],
    stage="via_master", channels="stereo", sample_rate=None, tail_ms=0,
    analysis_workers=None, step_s=None, auto_transport=false)
```

- `sources` — GUID дорожек, `"all"`, `"master"` (со `stage="master"`) или файлы;
  имена не разрешаются. `start_s`/`end_s` — одно окно, не вместе с `intervals`.
- `stage`: `via_master` (по умолчанию, что слышит слушатель), `master`,
  `external_file`; `premaster` отклоняется (`stage_not_rendered`), не подменяется.
- Ответ: `table.rows`, `table.cells`, `table.units`, `table.aggregates`,
  `evidence.token`, `measurement_id`, `revision`, `revision_before`, `self_noise`,
  `notes[]`. `step_s` добавляет профиль, `auto_transport=true` возвращает транспорт.
- `reaper_freshness` и `reaper_coverage` показывают, что ещё описывают сохранённые
  факты. Рендер временно меняет настройки и возвращает их, поэтому `change_count`
  растёт на фиксированный self-noise; он указан в `self_noise`, а
  `revision_before` остаётся пригодной для правок.

**edit** — `reaper_edit(mode="preview|apply|undo|sends|envelope_points", project_id,
expected_revision, ...)`; `project_id` и `expected_revision` обязательны всегда:

```text
reaper_edit(mode="preview", project_id=..., expected_revision=..., reason="...",
  operations=[{"kind":"track.set","target_guid":"<guid>","values":{"volume":0.9}},
              {"kind":"send.set","target_guid":"<guid>","values":{"destination_guid":"<guid>","volume":0.36}}])
→ {"plan_id": "...", "plan": {...}, "next_call": {...}}
```

- `apply`: с `plan_id` и базовой `expected_revision` → `update` + следующий
  `context_token`; с `operations` без `plan_id` применяет сразу и возвращает квитанцию.
- `undo`: `plan_id` плюс ревизия, которую произвёл apply, или `receipt="r-..."`.
- Каждая мутация требует текущий `structural_token`; `advisory` сообщает об
  отсутствии, `off` пропускает.
- Операции: `track.set/create/delete/reorder`, `item.set/create/delete/split/duplicate`,
  `take.set`, `midi.create`, `fx.set_enabled/set_parameter/insert/delete/move/set_preset/offline`,
  `send.connect/set/delete`, `envelope.set`, `envelope.level`, `marker.create`,
  `region.delete`, `tempo.set` — до 256 за батч.
- `send.connect` идемпотентен; `send.set` адресует строку по `destination_guid` или
  `send_index`. Батч не повторяет connect, не смешивает `send.delete` с другими
  изменениями сендов одного источника и не удаляет дорожку, которую меняет.
- `envelope.set`: `name`, `points`, `mode`, `clear`, `guard` (`min_gain_db`,
  `forbid_silence`). В `preview` `envelope.level` строит кривую по измеренному
  профилю, а `fx.set_parameter` с `{"band":3,"target":"-2 dB"}` разрешается через
  таблицу калибровки — сначала preview, потом apply.
- `mode="sends"` (`source_guid`) и `mode="envelope_points"` (`owner_guid`, `envelope`,
  `interval`, `scaling="amplitude|db"`) читают без правки.
- `verify` объектом (`sources`, `interval`, `metrics`, `tolerance`) рендерит источники
  до и после батча через тот же измеритель и возвращает `verify.before/after`.
  `true` оставляет только read-back. Числа реальные; сравнивайте, не предполагайте знак.
- Каждый ответ несёт `readback` и `receipt`. `undo` принимает квитанцию, не только plan id.
- FX summary в контексте по плагину: `total`, `set_params_count`,
  `available_params_count`, `reference`. Эффекты сравниваются с заново измеренным
  дефолтом; инструменты и плагины с generic-параметрами остаются `first_seen`, пока
  их не отредактируют.
- `apply` и `undo` требуют непустой `journal.why` (или `reason`); иначе
  `journal_required`. Запись уходит в `projects/<key>/journal.jsonl` с квитанцией,
  read-back, verify, ревизиями и `undoes`.

**media** — `reaper_media(action="import|encode", source=...)`:

- `import`: локальный путь или URL становится WAV в `<archive_root>/references` с
  измеренным профилем (LUFS, LRA, peak, true peak, crest, centroid, полосы, side/mid).
  `label` задаёт имя; `sha256` идентифицирует файл.
- `encode`: WAV кодируется в `format`/`bitrate` (mp3 320 по умолчанию), результат
  измеряется; если true peak выше `ceiling_dbtp`, громкость снижается и кодирование
  повторяется (ограниченное число итераций). В ответе `true_peak`, `lufs`, `iterations`.

**analysis** — `reaper_analyze` гоняет анализаторы по capture или живому проекту;
`reaper_mix_evidence` возвращает доказательства микса; `reaper_edit_map` строит карту
целей правки; `reaper_render_candidate(plan_id, project_id, revision_after, start_s,
end_s, max_duration_s=120)` рендерит мастер применённого плана; `reaper_release`
освобождает арендованный артефакт.

**inspect** — `reaper_evidence` цитирует единицы по evidence-токену; `reaper_calibrate`
записывает карту параметра плагина; `reaper_midi` читает и правит MIDI-тейки.
`reaper_timing` измеряет клик и атаки, строит карту ритма и проверяет монтаж
из айтемов на скорости 1×. Для этой работы прочитай `reaper://guide/timing`.
`waveform` показывает чистый DI даже без отметки детектора. Проследи один и тот
же фронт через план, фейды и записанные клипы. Уточняй спорные места локально
и отдельно отмечай проверку геометрии и прослушивание. См. [Тайминг аудио](TIMING.ru.md).

**discovery** — `reaper_capabilities` перечисляет инструменты и анализаторы;
`reaper_api_search` + `reaper_api_call` дают доступ к каталогу REAPER API; `reaper_action`
вызывает нативные Actions.

**research** — `reaper_flamingo_*` и `reaper_spatial_*` — опциональные слуховые и
пространственные бэкенды. Они не нужны для замера или правки.

## 4. Единицы

| Ключ (алиас) | Единица |
|---|---|
| `integrated_lufs` (`lufs`), `loudness_range_lu` (`lra`) | LUFS / LU |
| `peak_dbfs` (`peak`), `true_peak_estimate_dbfs` (`true_peak`) | dBFS / dBTP |
| `rms_dbfs` (`rms`), `crest_db` (`crest`) | dB |
| `spectral_centroid_hz` (`centroid`), `side_mid_db` (`side_mid`), `stereo_correlation` (`stereo`) | Hz / dB / −1..1 |
| `band_level_dbfs` (`bands`) | dBFS на полосу + `band_aggregate` |
| `clips`, `transients`, `events`, `tempo`, `dc` | count / ratio / 1/s / BPM / linear |

`metrics=None` возвращает все скалярные метрики; `"all"`/`"*"` добавляет полосы;
неизвестное имя отклоняется со списком допустимых. Стадии: `via_master`, `master`,
`external_file` — не подменяйте одну другой. Время аудио в секундах; MIDI идёт по
темп-карте — не предполагайте постоянный темп.

## 5. Контекст и факты

1. `reaper_session(action="context", archive_root=...)` — проект/ревизия, компактная
   карта, health, сводка фактов, FX, изменения и аудио-ссылки. Этого достаточно для
   ориентации.
2. `since=<state_token>` — только дельта: project/health/changes/next. Используйте
   после правки вместо повторного чтения карты.
3. `reaper_session(action="state", ...)` — точные измеренные факты с фильтрами, без
   рендера. Отсутствующее значение значит «не измерено в этом окне», не «нет сигнала».
4. `pages=true` делит контекст на страницы; в ответе только страницы, без дубля.

Факт актуален для fingerprint, который его произвёл. Если цепочка дорожки,
маршрутизация или мастер изменились, `freshness` помечает факт устаревшим;
перемеряйте только то, что нужно решению. Отсутствие значения не доказывает отсутствие.

## 6. Ошибки

Каждый сбой — `{"ok": false, "error": {"code", "message", "details", "next_action",
"recovery"?}}`; ошибки контекста/кандидата добавляют `recovery.retry_limit`.

| Код | Что делать |
|---|---|
| `HANDBOOK_REQUIRED` | прочитать `reaper://handbook` и повторить |
| `PREFLIGHT_REQUIRED` | выполнить `reaper_session(action="preflight")` |
| `journal_required` | передать `journal.why` или `reason` |
| `stale_revision` / `stale_context` | перечитать контекст/ревизию и построить новый план |
| `bridge_unavailable` | проверить `reaper_session(action="health")`, поднять мост |
| `validation_error` | `details` содержит допустимые имена/индексы; не повторять без изменений |
