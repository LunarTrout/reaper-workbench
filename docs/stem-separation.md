# SOTA сепарации стемов для целей по ролям — отчёт под V100 16 ГБ / Windows / Python

Дата: 2026-11-12. Всё, что помечено **[ИЗМЕРЕНО]**, проверено на этой машине:
Tesla V100-SXM2-16GB (SM 7.0), драйвер 528.89, torch 2.11.0+cu126, CUDA 12.6, Windows 11.
Скрипты замеров: `stem-probe/` (probe_sw.py, probe_mega.py).

---

## 0. TL;DR

1. **Рабочий стек под роли**: `BS-RoFormer SW 6-stem` (bass/drums/other/vocals/guitar/piano) +
   `MVSep Mega-53` (accordion, electric-guitar, acoustic-guitar, lead-vocal, back-vocal, kick/snare/toms/hh) +
   `DrumSep MDX23C 5-stem` (для суб-стемов бочки/снейра/томов). Всё три — на 16 ГБ влезают по очереди.
2. **124-band BS-RoFormer от MVSep (SDR vocals 12.33) — закрытые веса**, только платный доступ на сервере.
   Открытая альтернатива того же класса — **BS PolarFormer 124 bands** (SDR vocals 12.02), веса открыты, **[ИЗМЕРЕНО]** работает на V100.
3. **Металла в публичных бенчмарках нет**: в MUSDB18 test 4 металлических трека из 49 (8 %), в Multisong MVSep металла нет вообще.
   Заявленные SDR (11–14 дБ) — про pop/rock/hard rock, не про плотный металл. Переносить их на свой корпус нельзя, нужна своя калибровка.
4. **Стемы референсов дают только те роли, которые сепаратор реально различает**: бас, бочка, снейр, томы, «гитары вместе», «вокал вместе» (+ lead/back), аккордеон (только Mega-53).
   Отдельных «4 гитар» и «скрим vs чистый» не даёт ни одна открытая модель — цели по этим осям строить нельзя.
5. **Лицензии — главный риск**: у BS-RoFormer SW веса `license: unknown` (аккаунт автора удалён, зеркала без лицензии), у DrumSep лицензии нет вовсе.
   Лицензионно чистый стек: PolarFormer + SCNet XL IHF + inagoy/drumsep (MIT).

---

## 1. Модели: лицензия, размер, качество, запуск на V100

SDR — с Multisong-лидерборда MVSep (100 одноминутных треков, 40+ жанров) и из TISMIR-статьи про платформу MVSep.
Формат «bass/drums/other/vocals/instrum».

| Модель | Архитектура | Лицензия кода / весов | Размер (ckpt / параметры) | SDR (Multisong) | V100 16 ГБ | Вывод |
|---|---|---|---|---|---|---|
| **BS-RoFormer SW 6-stem** (jarredou, зеркало `enerjazzer/BS-ROFO-SW-Fixed`) | BS-RoFormer, dim 256, depth 12, 6 stems | MIT (код ZFTurbo/lucidrains) / **unknown** (веса) | 699 МБ / **174.7M** | 14.62 / 14.11 / 8.72 / 11.30 / 17.51 → лидерборд, запись «BS Roformer SW (6 stems)» | **[ИЗМЕРЕНО]** 14.8× realtime, пик 2.65 ГБ | Основная рабочая лошадка для ролей: даёт guitar и piano, которых нет в 4-stem |
| **MVSep Mega-53** (`ZFTurbo/.../releases/tag/v1.0.21`) | BS-RoFormer, dim 256, depth 12, **53 стема** | MIT (релиз в MIT-репозитории) | 1369 МБ / **681.7M** | по стемам не публиковался | **[ИЗМЕРЕНО]** 4.96× realtime, пик 7.56 ГБ (reserved 8.24) | **Единственный открытый источник accordion / electric-guitar / acoustic-guitar / lead-vocal / back-vocal** |
| **BS PolarFormer 124 bands** (`releases/tag/v1.0.20`) | BS-RoFormer + PoPE, 124 бэнда, fp16 | MIT / MIT (релиз) | 102 МБ / **51.1M** | vocals 12.02, instrum 18.33 (MVSep algorithms) | **[ИЗМЕРЕНО]** 8.6× realtime, пик 1.98 ГБ | Лучший **открытый** вокал; нужен `pip install PoPE-pytorch`, flash-attn не требуется |
| BS-RoFormer viperx (vocals) | BS-RoFormer | MIT / веса в релизах ZFTurbo | ~700 МБ | vocals 10.87–11.08 | не мерил, класс тот же | Хуже PolarFormer и Mel-Band Kim, смысла нет |
| **BS-RoFormer 124 bands (MVSep 2026.07)** | BS-RoFormer 124 бэнда | закрыто | — | vocals **12.33**, instrum **18.64** | n/a | **Недоступно**: «available exclusively to paid users», «several times slower» |
| **Mel-Band RoFormer (KimberleyJSN)** | Mel-Band, dim 384, depth 6, 60 бэндов | **MIT** (с 2026-04-22) | 913 МБ / ~228M | vocals 11.01, instrum 17.32 | не мерил; ожидаемо ~6–8× realtime, ~2 ГБ | Лицензионно чистый вокал №1 |
| Mel-Band RoFormer «deux» (becruily) | Mel-Band | нет лицензии | — | vocals 11.35, instrum 17.51 | — | Лучшие цифры среди Mel-Band, но лицензия отсутствует |
| **SCNet XL IHF** (`v1.0.15`) | SCNet | MIT / MIT | 214 МБ | 11.94 / 11.58 / 6.48 / 9.68 (bass/drums/other/voc) | не мерил; лёгкая, ожидаемо 20×+ | Быстрый базовый 4-stem, лицензионно чистый |
| SCNet XL (`v1.0.13`) | SCNet | MIT / MIT | 216 МБ | 11.87 / 11.49 / 6.19 / 9.32 | — | То же, чуть слабее IHF |
| **SubRoformer base 4stem** (`v1.0.23`, 2026-11) | subband + RoFormer | MIT / MIT | 207 МБ | MUSDB: 10.63 (target-specific), 11.59 (4-stem +500 песен) | не мерил | Новинка 2026, но 4-stem и без Multisong-цифр |
| **DrumSep MDX23C 5-stem** (aufr33/jarredou) | MDX23C | **нет лицензии**, оригинальный хост удалён | ~200 МБ (зеркала) | kick **16.66**, snare **11.53**, toms **12.33**, hh **4.04**, cymbals **6.36** | не мерил, MDX23C лёгкий | Лучшие kick/snare/toms; hh/cymbals — мусор |
| DrumSep SCNet XL 5-stem (MVSep) | SCNet XL | закрыто (сервер) | — | kick **17.89**, snare **12.56**, toms **14.14**, hh **3.63**, cymb 6.15 | n/a | Лучшие цифры, но веса не отдают |
| DrumSep htdemucs (inagoy) | HTDemucs | **MIT** (код) | 167 МБ | kick 10.52, snare 6.05, toms 4.68 | — | Слабый, но лицензионно чистый |
| HTDemucs4 / Demucs4 HT (baseline) | HTDemucs | MIT | 4×~80 МБ | drums 11.4, guitar 5.24 (6-stem) | — | Только как baseline/сравнение |

Обёртки для интеграции:

> Про лицензии весов: «MIT / MIT» у PolarFormer, SCNet и Mega-53 означает, что чекпоинт лежит в релизе MIT-репозитория
> ZFTurbo; отдельного заявления о лицензии именно весов там нет. Это слабее, чем явный `license: mit` у Mel-Band Kim,
> но заметно лучше, чем `license: unknown` у SW и полное отсутствие лицензии у DrumSep.

| Инструмент | Лицензия | Что даёт | Ограничения |
|---|---|---|---|
| **`msst` (pip)** | MIT | `Separator(...).separate_file()` → `dict[str, np.ndarray]`, 20+ архитектур, метрики `bleedless/fullness` | `python<3.14`, `torch<2.12`; chunk/overlap только через YAML-конфиг |
| `ZFTurbo/Music-Source-Separation-Training` (репо) | MIT | Полный контроль, `utils.model_utils.demix()` | Свой набор зависимостей; beartype требует tuple в конфиге |
| `nomadkaraoke/python-audio-separator` | MIT | Простой CLI/API, качает модели по имени | Зоопарк UVR-центричный, меньше контроля над chunk/overlap |
| `openmirlab/bs-roformer-infer` | MIT | Только BS-RoFormer, sha256-реестр чекпоинтов | 1 архитектура |

---

## 2. Что реально отделяется в плотном металле

Прямого бенчмарка на металле нет — это проверяемый факт, а не мнение:

* В MUSDB18 **test** 49 треков, из них `Heavy Metal` — **4** (Pop/Rock 35, Electronic 4, Rap 3, Reggae 2, Rock 1):
  <https://zenodo.org/records/13271525/files/musdb18.test.json>
* В Multisong MVSep (главный современный бенчмарк, 100 треков) список жанров металла не содержит:
  «Acoustic, Folk, … 1960s Rock, Alternative, **Hard Rock**, Punk, …» — <https://transactions.ismir.net/articles/10.5334/tismir.366> §4.
* Значит агрегированный SDR 11–12 дБ по вокалу на 90 % набран на pop/rock; металлические 8 % треков почти не влияют на среднее и не показывают деградацию.

Косвенные количественные ориентиры (TISMIR, таблицы 3–7; DrumSep-таблица):

| Роль | Лучший публичный SDR | Источник | Пригодность для целей |
|---|---|---|---|
| Бас | 14.6–14.9 | Multisong leaderboard | **Высокая**: уровень, полосы, crest |
| Бочка | 16.7–17.9 | DrumSep (TISMIR) | **Высокая** |
| Снейр | 11.5–12.6 | DrumSep | Средняя: уровень/атака — да, тембр — осторожно |
| Томы | 12.3–14.1 | DrumSep | Средняя |
| Вокал (в микс) | 11.0–12.3 | Multisong / MVSep algorithms | Средняя: уровень и полосы — да; f0/HNR на скриме — нет |
| «Гитара» как класс | **7.1** (BS-RoFormer), 7.5 (ансамбль) | TISMIR Table 4 (Guitar benchmark) | Низкая-средняя: только грубый баланс, ±1.5–2 дБ шум. Отдельный guitar-стем SW публично не измерен — ожидать 5–7 дБ |
| Пианино/клавиши | 5.7–6.2 | TISMIR Table 5 | Низкая |
| Bowed strings | 2.9–4.3 | TISMIR Table 6 | **Непригодно** |
| Wind | 3.6 (лучший) | TISMIR Table 7 | **Непригодно** |
| Hi-hat | **3.4–5.1** | DrumSep | **Непригодно** (только «есть/нет») |
| Cymbals | 6.1–7.1 | DrumSep | Непригодно для баланса |
| `other` (помойка) | 5.6–8.7 | Multisong | Только как «остаток», не как角色 |
| Аккордеон | бенчмарка нет | — | Только Mega-53, качество неизвестно → обязательна калибровка |

Дополнительно: слушательский тест (Bake-Off, <https://arxiv.org/html/2507.06917v1>) показывает, что **ΔSDR не предсказывает предпочтение для инструментала** (ρ = −0.03), для вокала — предсказывает (ρ = 0.68). То есть по инструментальным стемам SDR вообще не годится как критерий качества цели.

---

## 3. Статистика по стемам референсов: что устойчиво, что нет

### 3.1. Три уровня метрик

**Tier A — устойчивые к протечке (можно строить цели):**
* Полосный RMS в 6–8 октавных полосах после нормализации по LUFS (ITU-R BS.1770). Ошибка сепаратора — это в основном аддитивная утечка энергии в полосе, она складывается с целевой, а не модулирует её.
* Соотношения полос внутри одного стема (спектральный наклон) — инвариантны к общей ошибке усиления.
* Crest factor по полосам (peak/RMS на 1–3 с окнах) — мера компрессии; устойчив, пока маска не «сглаживает» атаки (у бочки/баса — да, у гитар — частично).
* Отношения роль↔роль (бас/гитары, вокал/гитары) — частично когерентные ошибки сокращаются, если обе роли идут через один сепаратор с одинаковой геометрией масок.
* Суммарные (mix-level) метрики — эталон: их вообще не надо сепарировать.

**Tier B — хрупкие (не строить цели, максимум sanity check):**
* Хвосты/реверберация: маски съедают хвост (модели обучены на «сухих» стемах) → систематическое занижение.
* Тарелки, hi-hat, «воздух» (>10 кГц): SDR 3–5 дБ.
* Ширина/корреляция по стему: стерео-маски меняют фазовые соотношения; ширина систематически «сужается».
* f0/HNR по скриму/гроулу: шумовая вокализация + артефакты разделения → бессмысленно.
* Маскировка между стемами: двойная ошибка (две оценки).
* Разложение `other` на гитары/клавиши/синт: помойка.

### 3.2. Матрица протечки: как измерять

Строится на **своих настоящих стемах** (33 дорожки), поэтому ground truth точный:

1. Сгенерировать N = 60–100 синтетических миксов: случайные подмножества 2–6 стемов, случайные gains ±6 дБ, случайные панорамы, опционально лёгкий bus-компрессор/лимитер (чтобы имитировать мастеринг референсов).
2. Прогнать через сепаратор, получить оценки `ŝ_i`.
3. Для каждой полосы `b` решить задачу неотрицательных наименьших квадратов по амплитудным спектрам:
   `|ŝ_i(b,t)| ≈ Σ_j a_ij(b) · |s_j(b,t)|`, `a ≥ 0` — по блокам кадров (например, 1 с).
4. Медиана `a_ij(b)` по миксам → матрица `L(b)` в дБ. Диагональ = fullness (потеря цели), вне диагонали = bleed.
5. Разброс (IQR) `a_ij(b)` по миксам → неопределённость калибровки `σ_ij(b)`.

Готовые метрики того же смысла уже есть в MSST: `bleedless`, `fullness` (+ `_mr` варианты), см. `docs/python_api.md`. Их можно использовать как быстрый sanity check, но для поправки целей нужна именно полосная матрица `L(b)`.

### 3.3. Как калибровать цели

Для измеренного на референсе отношения ролей `Δ_obs(i,j,b)`:

```
Δ_true(i,j,b) ≈ Δ_obs(i,j,b) − [G_i(b) − G_j(b)]      # G = диагональ L, дБ
условие надёжности:  |Σ_{k≠i} L_ik(b)| < ε  и  |Σ_{k≠j} L_jk(b)| < ε
```

* `ε` выбирается из `σ` (например, `ε = 2σ`).
* Если условие не выполняется — цель помечается `reliable=false` и в статистику корпуса не идёт (или идёт как «мягкая» с весом).
* Оценка неопределённости цели: `σ_Δ = sqrt(σ_ij² + σ_ji² + σ_gain²)`, где `σ_gain` — разброс усиления стема (обычно 0.3–0.8 дБ, тоже измеряется на синтетике).

### 3.4. Кросс-модельная проверка (дешёвая и сильная)

Прогнать референсы **двумя структурно разными** сепараторами (BS-RoFormer SW и SCNet XL IHF). Если роль-отношение совпадает в пределах 0.5–1 дБ — цель модельно-независима, `reliable=true`. Если расхождение больше — это и есть мера ошибки, цель в мусор.

### 3.5. Статистика: не путать кадры с наблюдениями

* 100 мс кадры **сильно автокоррелированы** (ошибка сепаратора меняется на масштабе ~0.5–2 с). Эффективное число независимых наблюдений на трек — порядка `длительность_с / 1с`, а не `×10`.
* Правильно: сначала **медиана и IQR по кадрам внутри трека**, потом статистика **по трекам** (n = 20–30).
* Сравнение корпуса и своего микса — по распределениям медиан (bootstrap CI на медиане, ~`±1.25·IQR/√n`), а не по t-тесту на кадрах.
* Один трек = одна точка. Иначе получите «значимость» из шума.

---

## 4. Замеры на этой V100 (16 ГБ)

Условия: torch 2.11.0+cu126, fp16 autocast, `num_overlap=2`, batch 1, стерео 44.1 кГц.
Аудио — синтетический 30 с и 240 с микс (перф-тест, качество разделения тут не оценивается).

| Модель | Параметры | 30 с → время | 240 с → время | Realtime-фактор | Пик VRAM (alloc / reserved) |
|---|---|---|---|---|---|
| BS-RoFormer SW 6-stem | 174.7M | 3.8 с | 16.2 с | 7.9× / **14.8×** | 2.09 / 2.34 ГБ (30 с); 2.65 / 3.48 ГБ (240 с) |
| MVSep Mega-53 | 681.7M | 7.0 с | 48.4 с | 4.3× / **4.96×** | 8.52 / 9.75 ГБ (30 с); 7.56 / 8.24 ГБ (240 с) |
| BS PolarFormer 124b | 51.1M | 3.5 с | — | **8.6×** | 1.98 / 2.19 ГБ |

Бюджет на корпус 20–30 референсов (средний трек 4 мин → 120 мин аудио):

| Проход | Время на 120 мин аудио |
|---|---|
| SW 6-stem | ~8 мин |
| Mega-53 | ~24 мин |
| PolarFormer (вокал) | ~14 мин |
| DrumSep (по стему барабанов) | ~10–15 мин (оценка) |
| **Всё последовательно, один проход** | **~55–60 мин** |
| + повторный проход вторым сепаратором для кросс-проверки | +~10 мин (SCNet XL IHF) |
| С TTA ×3 на финальном прогоне | ×3 к соответствующему проходу |

Вывод по железу: **16 ГБ хватает с запасом**; Mega-53 (самый тяжёлый) занимает 8.5 ГБ. Одновременный запуск Mega + SW (≈11 ГБ) возможен, но не рекомендуется из-за фрагментации — гонять последовательно.

### 4.1. Совместимость со SM 7.0 (проверено)

* `torch 2.11.0+cu126`: `torch.cuda.get_arch_list()` = `['sm_50','sm_60','sm_61','sm_70','sm_75','sm_80','sm_86','sm_90']` → V100 поддерживается, fp16 matmul работает. **[ИЗМЕРЕНО]**
* Код RoFormer сам определяет `compute capability < 8.0` и уходит на math/mem-efficient SDPA:
  в логе `GPU Compute Capability below 8.0, using math or mem efficient attention`. **flash-attn не нужен.**
* `torch.cuda.amp.autocast` в пайплайне = **fp16** (не bf16) → на V100 корректно; bf16 на SM70 не поддерживается железом.
* Осторожно с апгрейдом torch: в cu128-сборках с 2.8 убраны Maxwell/Pascal (sm_50/60) — <https://github.com/pytorch/pytorch/issues/157517>, <https://pytorch.org/blog/pytorch-2-8/>;
  CUDA 13.x сборки — риск для Volta. Проверено только то, что **cu126 + torch 2.11 содержит sm_70**; перед любым апгрейдом смотреть `torch.cuda.get_arch_list()`.
  Практический вывод: **сидеть на cu124/cu126, не уходить на cu130**.
* `msst` требует `torch<2.12` и `python<3.14` (<https://pypi.org/project/msst/>). На Python 3.14 пакет не поставится — тогда брать репозиторий напрямую (так и сделаны замеры).

---

## 5. Рекомендуемый стек

### Вариант A — «максимум по ролям» (рекомендуется для целей)

| Шаг | Модель | Роли | Зачем |
|---|---|---|---|
| 1 | BS-RoFormer SW 6-stem | bass, drums, vocals, guitar, piano, other | база: даёт guitar/piano, которых нет в 4-stem |
| 2 | MVSep Mega-53 | accordion, electric-guitar, acoustic-guitar, lead-vocal, back-vocal, kick, snare, toms, hh | роли, которых нет больше нигде (аккордеон!) + второе мнение по гитарам/вокалу |
| 3 | DrumSep MDX23C 5-stem (по стему `drums` из шага 1) | kick, snare, toms, hh, cymbals | точные суб-стемы барабанов |
| 4 | BS PolarFormer 124b (опционально) | vocals | когда вокальная цель критична: 12.02 SDR против 11.3 у SW |

Trade-off: ~55 мин на 30 треков, пик 8.5 ГБ, но у SW лицензия весов `unknown`.

### Вариант B — «лицензионно чисто» (если коммерция)

PolarFormer (MIT) + SCNet XL IHF (MIT) + inagoy/drumsep htdemucs (MIT).
Trade-off: теряете guitar/piano/accordion как отдельные роли (у SCNet только 4 стема), барабанные суб-стемы сильно слабее
(kick 10.5 / snare 6.05 / toms 4.68). Для целей по ролям это означает: **гитары и аккордеон в целевой статистике недоступны**,
остаются бас/бочка/снейр/вокал.

### Вариант C — «быстро и дёшево»

SCNet XL IHF (214 МБ, 4 стема) — один прогон, ~10 мин на корпус, лицензия MIT.
Trade-off: нет гитар/аккордеона; `other` = 6.5 SDR, то есть гитарный баланс только косвенно.

---

## 6. План интеграции (пошагово)

1. **Окружение.** Отдельный venv Python 3.12, `torch==2.11.0+cu126` (или оставить текущий 2.6+cu124 — код совместим),
   `pip install "msst[bs_roformer]"`. Если Python 3.14 — клонировать репозиторий и ставить минимальный набор
   (`numpy soundfile ml_collections omegaconf einops pyyaml tqdm beartype rotary_embedding_torch librosa packaging`).
2. **Реестр моделей** (`models.json`): `id`, `url`, `sha256`, `config_sha256`, `arch`, `stems`, `license`, `chunk_size`.
   Уже посчитанные хеши:
   * `BS-Rofo-SW-Fixed.ckpt` — 699 412 152 байт, `sha256 24e7d35ee9c64415673d3fd33e06a67cac2c103c5df6267ba1576459c775916e`
   * `mvsep_mega_model_bs_roformer_53_stems_v1.ckpt` — 1 368 919 887 байт, `sha256 c62820893bbf86d4e734f966bd142d9157cfc8bb8e79e9d8f9ea553f3ff3519f`
   Скачивать только по хешу, хранить локально (оригинальные хосты умирают: аккаунт jarredou удалён, 9 из 10 URL в реестре bs-roformer-infer отдавали 404 — <https://github.com/openmirlab/bs-roformer-infer>).
3. **Обёртка** (тонкая, ~100 строк, как в `stem-probe/probe_sw.py`):
   `yaml → ConfigDict → BSRoformer(**model_cfg) → load_state_dict → .to('cuda').eval()`,
   затем `utils.model_utils.demix(cfg, model, mix, device, 'bs_roformer')` → `dict[str, np.ndarray]`.
   Важно: конфиг из репозитория — тренировочный, блок `inference` (chunk_size / num_overlap / batch_size) надо дописать;
   beartype требует **tuple**, а не list для `freqs_per_bands` и `multi_stft_resolutions_window_sizes`.
4. **Кэш по хешу.** Ключ = `sha256(аудио) + model_id + sha256(ckpt) + sha256(config) + chunk + overlap + dtype + версия кода`.
   Артефакты: `cache/stems/<key>/<role>.wav` (float32) + `meta.json` (модель, чанк, время, хеши, версия).
   Сепарация детерминирована → кэш полностью безопасен; метрики кэшировать отдельно по `(stem_key, metric_version)`,
   чтобы смена метрик не требовала пере-сепарации.
5. **Role mapping** (stem → роль проекта):
   `kick/snare/toms` ← DrumSep(drums); `bass` ← SW; `guitars` ← SW.guitar (основное) + Mega.electric-guitar/acoustic-guitar (проверка);
   `accordion` ← Mega.accordion; `vocals` ← SW.vocals / PolarFormer; `lead/back` ← Mega.lead-vocal/back-vocal.
6. **Пост-обработка**: LUFS-нормализация каждого стема, полосная матрица 100 мс (6–8 полос), crest, спектральный наклон.
7. **QA-гейты** перед записью в корпус: (а) residual-тест `mix − Σ stems`; (б) кросс-модельное расхождение; (в) матрица `L(b)`;
   (г) флаг `reliable` по правилу из §3.3.

### 6.1. Скелет кода

```python
from msst import Separator          # py3.12, torch<2.12
with Separator(model_type="bs_roformer",
               config_path="models/bs_rofo_sw.yaml",   # + inference.chunk_size/num_overlap
               checkpoint_path="models/BS-Rofo-SW-Fixed.ckpt",
               device_ids=0) as sep:
    stems = sep.separate_file("ref.wav")   # dict[str, np.ndarray] (channels, samples)
```

Без msst (как в замерах):

```python
cfg = ConfigDict(yaml.safe_load(yaml_text_with_inference_block))
kw = {k: (tuple(v) if isinstance(v, list) else v) for k, v in dict(cfg.model).items()}
model = BSRoformer(**kw).load_state_dict(torch.load(ckpt, map_location="cpu")).to("cuda").eval()
out = demix(cfg, model, mix, torch.device("cuda"), "bs_roformer", pbar=False)
```

---

## 7. Подводные камни

| # | Проблема | Что делать | Источник |
|---|---|---|---|
| 1 | **Chunking**: шаг = `chunk_size // num_overlap`, fade = `chunk_size // 10`; на стыках возможны слышимые артефакты | overlap 2 достаточно: overlap 8 даёт **+0.02–0.05 дБ SDR** при кратном росте времени | <https://mvsep.com/en/news> |
| 2 | **Ресемплинг**: все модели ждут 44.1 кГц, референсы бывают 48 кГц | один ресемпл (soxr) на входе, работать в float32; не ресемплить туда-обратно | конфиги моделей: `sample_rate: 44100` |
| 3 | **Стерео**: модели stereo-обучены, маски общие на каналы | не суммировать в моно перед сепарацией; моно дублировать в 2 канала | `num_channels: 2` в конфиге |
| 4 | **Фаза**: `instrumental = mix − vocals` даёт артефакты и «фазовую грязь» | использовать собственный выход модели, а не вычитание; вычитание — только как fallback | <https://mvsep.com/en> (Deux / phase-corrected instrumental) |
| 5 | **Стемы не суммируются в микс**: у Mega-53 прямо заявлено перекрытие информации | не делать null-тест как критерий качества; residual использовать только как диагностику | release notes v1.0.21 |
| 6 | **`other` — помойка** (SDR 5.6–8.7; в одной из реализаций падает до 3.69) | никогда не строить цели по `other`; только «остаток» | <https://mvsep.com/quality_checker/multisong_leaderboard> |
| 7 | **Длинные треки**: аккумуляторы overlap-add могут съесть VRAM | код сам уводит буферы на CPU, если нужно >50 % свободной памяти (`_get_accumulation_device`) | `utils/model_utils.py` |
| 8 | **Мастеринг/клиппинг референсов**: лимитер и клип добавляют широкополосную грязь, которой не было в тренировочных миксах | калибровать на синтетике, прогнанной через такой же лимитер | — (методика §3.2) |
| 9 | **Исчезающие веса**: аккаунт jarredou удалён, `github.com/jarredou/models` 404 | пиновать sha256 + локальное зеркало | <https://github.com/openmirlab/bs-roformer-infer> |
| 10 | **Версии**: msst `torch<2.12`, `python<3.14`; cu128+2.8 убрал sm_50/60 | фиксировать окружение, не апгрейдить «на всякий случай» | <https://pypi.org/project/msst/>, <https://github.com/pytorch/pytorch/issues/157517> |
| 11 | **PoPE**: PolarFormer требует `PoPE-pytorch` | `pip install PoPE-pytorch` (flash-attn не нужен) | проверено: **[ИЗМЕРЕНО]** |
| 12 | **TTA**: ×3 времени ради долей дБ | включать только на финальном прогоне, не на калибровке | `--use_tta` в CLI |

---

## 8. Риски и что проверить первым

1. **Лицензии.** SW-веса — `unknown`, DrumSep — без лицензии. Если проект коммерческий — вариант B, и тогда роли «гитары/аккордеон» из статистики выпадают. Решение принимает владелец проекта.
2. **Качество Mega-53 на металле неизвестно.** В release notes прямо: «performance on individual stems may be lower than the specialized models». Проверять на синтетических миксах из своих стемов, а не на слух.
3. **Нет металла в бенчмарках** → все цифры SDR из таблиц — верхняя граница. Ожидать падения 2–5 дБ на плотном миксе (гипотеза, требует проверки на своём корпусе).
4. **Отдельных гитар и скрим/чистый не даёт никто.** Цели формулировать на уровне «гитары вместе», «вокал вместе» (+ lead/back). Иначе получите цель, которую нечем измерить.
5. **Автокорреляция кадров** — самая вероятная ошибка в статистике: 100 мс матрица даст ложную точность. Агрегировать по трекам.

### Порядок работ (первые 3 шага)

1. Собрать `stem-probe`-окружение, прогнать 3 своих трека через SW и Mega, снять VRAM/время на реальном материале (30 мин).
2. Построить матрицу `L(b)` на 60 синтетических миксах из своих стемов (полдня).
3. Посчитать по 5 референсам роль-отношения двумя моделями, сравнить расхождение с `σ` из шага 2 → получить первые `reliable`-цели.

---

## Источники

* MVSep Algorithms (таблицы SDR, включая 124 bands и PolarFormer): <https://mvsep.com/en/algorithms>
* MVSep Multisong Leaderboard: <https://mvsep.com/quality_checker/multisong_leaderboard>
* MVSep News (124-band premium, overlap 8 → +0.02–0.05 дБ, PolarFormer): <https://mvsep.com/en/news>
* Solovyev et al., «Evaluation Platform and Benchmarks for Music Source Separation», TISMIR 2026: <https://transactions.ismir.net/articles/10.5334/tismir.366>
* ZFTurbo MSST, список весов и SDR: <https://github.com/ZFTurbo/Music-Source-Separation-Training/blob/main/docs/pretrained_models.md>
* MSST Python API: <https://github.com/ZFTurbo/Music-Source-Separation-Training/blob/main/docs/python_api.md>
* MSST на PyPI (ограничения версий): <https://pypi.org/project/msst/>
* BS-RoFormer (Lu et al.): <https://arxiv.org/abs/2309.02612>
* Mel-Band RoFormer (Wang et al.): <https://arxiv.org/abs/2310.01809>
* SubRoformer (2026): <https://arxiv.org/abs/2611.08130>
* Dynamic Band Splitting (2026): <https://arxiv.org/abs/2610.14993>
* PoPE (ICML 2026): <https://arxiv.org/abs/2509.10534>
* Musical Source Separation Bake-Off: <https://arxiv.org/html/2507.06917v1>
* MoisesDB (жанры/стемы): <https://arxiv.org/html/2307.15913>
* MUSDB18 test, жанры: <https://zenodo.org/records/13271525/files/musdb18.test.json>
* Mega-53 release: <https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/tag/v1.0.21>
* BS PolarFormer release: <https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/tag/v1.0.20>
* SCNet XL IHF release: <https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/tag/v1.0.15>
* Mel-Band RoFormer Kim (MIT): <https://huggingface.co/KimberleyJSN/melbandroformer>
* BS-RoFormer SW (зеркало, license unknown): <https://huggingface.co/enerjazzer/BS-ROFO-SW-Fixed>
* bs-roformer-infer (история с исчезнувшими хостами): <https://github.com/openmirlab/bs-roformer-infer>
* python-audio-separator (MIT): <https://github.com/nomadkaraoke/python-audio-separator>
* PyTorch 2.8: удаление Maxwell/Pascal в cu128: <https://github.com/pytorch/pytorch/issues/157517>, <https://pytorch.org/blog/pytorch-2-8/>
