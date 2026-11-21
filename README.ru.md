# REAPER Workbench

[English](README.md) · [Русский](README.ru.md)

Python-библиотека и локальный MCP-сервер для живого проекта REAPER: читать
проект, измерять реально отрендеренный звук, править типизированными батчами и
держать один документ состояния, который говорит агенту, что известно, а что
устарело.

Модель не видит скриншотов и GUI. Она общается с resident ReaScript-мостом через
локальный файловый mailbox; REAPER может оставаться в фоне.

## Что умеет

- **Состояние проекта.** Один `state.json` на проект: карта, fingerprints
  объектов, измеренные факты и их свежесть. `capture` и `measure` пишут его,
  status/freshness/coverage/context читают.
- **Один вызов контекста.** `reaper_session(action="context")` отдаёт проект,
  ревизию, компактную карту, health, сводку фактов, FX и изменения.
  `since=<state_token>` — только дельта; `pages=true` — страницы без повторного
  полного вида.
- **Измерять, а не угадывать.** `reaper_measure` рендерит через REAPER и
  возвращает таблицу источники × интервалы × метрики: LUFS, пики, crest,
  полосы, стерео, тайминг, evidence-токен и ревизию.
- **Факты без повторного рендера.** `reaper_session(action="state")` читает уже
  измеренное с фильтрами по источнику, стадии, окну и метрике. Устаревшие факты
  остаются помеченными.
- **Типизированные правки.** `reaper_edit` preview/apply/undo для дорожек,
  айтемов, тейков, FX, сендов, огибающих, MIDI, маркеров, регионов и темпа.
  Каждый батч несёт receipt, readback и ревизию; apply/undo требуют
  `journal.why` и пишут append-only журнал с basis.
- **Асинхронный capture.** Задания по источникам сообщают прогресс и не
  блокируют канал управления; offline-медиа будится перед рендером.
- **Доставка.** `reaper_media` импортирует референсы или кодирует аудио с
  проверкой true peak после кодека.

MCP-поверхность сгруппирована: **42 инструмента в 11 группах** (`session`,
`measure`, `edit`, `midi`, `calibrate`, `analyze`, `action`, `api`, `flamingo`,
`spatial`, `media`). Актуальный список — `reaper_capabilities(mode="tools")`;
схемы — `tools/list`.

## Установка

См. [Установку](docs/INSTALLATION.ru.md). Коротко:

```powershell
git clone https://github.com/LunarTrout/reaper-workbench.git
Set-Location reaper-workbench
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[mcp]"
```

Загрузи `scripts/reaper_bridge.lua` в REAPER (Actions → Load ReaScript, затем
Run). Скрипт держит heartbeat и обслуживает Python-клиент.

## MCP

```json
{
  "mcpServers": {
    "reaper-workbench": {
      "command": "python",
      "args": ["-m", "reaper_audio.mcp_server"],
      "env": { "PYTHONPATH": "src" }
    }
  }
}
```

В начале сессии прочитай ресурс `reaper://handbook` (или `docs/HANDBOOK.md`).
measure/edit/midi/analyze отказывают с `HANDBOOK_REQUIRED`, пока он не прочитан.

## Документация

- [Руководство](docs/HANDBOOK.ru.md) — текущий рабочий контракт
- [Handbook (English)](docs/HANDBOOK.md)
- [Установка](docs/INSTALLATION.ru.md) · [English](docs/INSTALLATION.md)
- [Политика](docs/POLICY.md) — пороги задаёт владелец
- [Безопасность](docs/SAFETY.ru.md) — границы доверия и гарантии состояния
- [Тайминг и планирование по сетке](docs/TIMING.md)
- [Partial loudness](docs/PARTIAL-LOUDNESS-GATE.md) — гейт и метод остатка
- [Flamingo](docs/flamingo.md), [Ossicle](docs/ossicle.md) — опциональные слуховые бэкенды

## Тесты

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
```

Набор офлайн: mock-мосты и синтетическое аудио; живые проверки REAPER отдельно.

## Лицензия и веса моделей

Веса моделей не входят в репозиторий. Flamingo — NVIDIA OneWay Noncommercial
Research; перед использованием вне исследований проверь лицензию.
