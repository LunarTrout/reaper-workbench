# Сабагент 2 — ноты, партии и временные события

## Решение, для которого нужен ответ

Как получить из записанных дорожек проверяемое описание исполнения: ноты и высоты, начало и длительность, аккорды/многоголосие, атаки, остановки и артикуляции? Как добиться полезного результата на быстрых, низко настроенных, palm-muted и сильно искажённых гитарах, басе и плотных барабанах?

## Что исследовать в нашем проекте

- Модули analysis temporal, events, transients, pitch/chroma/F0 и модели Music Flamingo/Ossicle-4B. Какие выходы являются только спектральными признаками, какие претендуют на музыкальные события?
- Имеются ли в MCP структурированные note/event records или только summary/audio/model answer?
- Тесты и известные ошибки для низкого строя, перегруза, harmonics, tremolo picking, chugs, double tracking, быстрых нот и одновременных инструментов.
- Как используются исходные и обработанные рендеры; не уничтожает ли distortion, compression или transient shaping данные, нужные транскрипции.

## Вопросы внешнего исследования

1. Разделить задачи: onset detection, monophonic F0, multipitch, polyphonic note transcription, tablature/fingering, chord detection, instrument/technique recognition и drum transcription. Какие из них нужны для действий в REAPER, а какие могут отвлекать?
2. Какие текущие открытые модели и библиотеки имеют проверенные результаты на электрогитаре, басе и металле? Изучить статьи, репозитории, benchmark'и, issues и пользовательские проверки; не ограничиваться популярностью.
3. Какую стратегию выбрать: direct transcription, source separation → transcription, несколько specialized models, DSP+model, музыкальная модель с audio encoder? Сравнить end-to-end ошибки.
4. Как учитывать временные смещения: проектный time, source time, take offset, stretch markers, latency/FX? Как представить uncertainty каждого события?
5. Что из этого реально запускать на одной V100 16 GB; какие квантования доступны, лицензии, VRAM и latency; есть ли CPU fallback, который противоречит ограничению.
6. Какие метрики использовать: onset F1/tolerance, note precision/recall, pitch error, duration error, event identity, measure/bar accuracy? Какие ground-truth наборы существуют для distorted guitar/death/black metal?
7. Как совмещать автоматическую транскрипцию с экспертной поправкой и превращать результат в редактируемые MIDI/event objects, не выдавая вероятностные ноты за истину?

## Результат

Матрица методов по типам инструментов и музыкальной плотности; применимость к metal; лучшие варианты для V100; предложенная общая схема note/event representation; вероятные ошибки и detection/abstention policy; benchmark и критерии проверки. Обязательно указать, какие задачи библиотека уже закрывает частично и чего MCP пока не предоставляет как структурированный результат.

