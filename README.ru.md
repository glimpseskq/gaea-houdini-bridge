# GH-Bridge — файловый мост Gaea ⇄ Houdini

[English](README.md) | **Русский**

Мост между **SideFX Houdini** и **QuadSpinner Gaea 2.0**, работающий через файлы на диске.
Он автоматизирует сторону Houdini: выгружает HeightField для Gaea и забирает готовые карты
эрозии обратно как именованные слои HeightField.

```
Houdini                         диск                          Gaea 2.0 (GUI)
───────                         ────                          ──────────────
HeightField ── Export ──▶  input_height.exr  ──▶  File-нода ─▶ граф эрозии
                           manifest.json                          │ Build (F8)
HeightField ◀── Import ──  output/output_*.exr ◀── Export-ноды ◀──┘
 + слои height/erosion/flow/wear/sediment
```

## Возможности

- **Export / Import одной кнопкой** — HDA `Gaea Erosion Bridge` с UI прямо в сети SOP.
- **Именованные слои** — `height`, `erosion`, `flow`, `wear`, `sediment` приходят в
  HeightField как отдельные слои. Отсутствующие файлы пропускаются с предупреждением,
  без ошибки.
- **Кэш по хэшу** — мост считает сигнатуру входа, разрешения и параметров. Если ничего
  не изменилось и выходы Gaea уже есть, он сообщает, что пересборка не нужна.
- **Манифест задачи** — `gh_bridge_manifest.json` описывает, что и с какими параметрами
  было выгружено.
- **Безопасная очистка** — `cleanup_temp` никогда не удаляет файлы за пределами рабочей
  папки моста.
- **Без вмешательства в Gaea** — мост не использует CLI-движок, SwarmHost, лицензию или
  защиту. Gaea запускается только как обычное приложение (опционально), поэтому мост
  работает на любой лицензионной сборке.

## Требования

- Houdini с Python 3 (проверено на Houdini 20.5).
- Gaea 2.0.
- Windows (пути по умолчанию рассчитаны на Windows, но меняются в конфиге или на ноде).

## Состав репозитория

| Файл | Назначение |
|------|------------|
| `houdini_gaea_bridge.py` | Ядро: экспорт/импорт, кэш, манифест, очистка. |
| `build_gaea_bridge_hda.py` | Однократно запускается в Houdini и собирает HDA `gaea_erosion_bridge`. |
| `tests/test_bridge_logic.py` | Юнит-тесты логики, не требующие запущенного Houdini. |

## Установка

### 1. Сделать модуль доступным для Houdini

Выберите один из вариантов.

**Вариант А — через `houdini.env`** (удобно, если планируете обновлять репозиторий).
Добавьте в `<Документы>/houdiniXX.X/houdini.env`:

```
PYTHONPATH = "C:/path/to/BridgeHGH;$PYTHONPATH"
```

**Вариант Б — копированием.** Положите `houdini_gaea_bridge.py` в
`<Документы>/houdiniXX.X/scripts/python/` (создайте папку, если её нет).

Перезапустите Houdini и проверьте в Python Shell:

```python
import houdini_gaea_bridge as ghb; print(ghb.__file__)
```

### 2. Собрать HDA

В Houdini откройте **Windows ▸ Python Shell** и выполните:

```python
exec(open(r"C:/path/to/BridgeHGH/build_gaea_bridge_hda.py").read())
```

В консоли появится `Installed HDA: ...\otls\gaea_erosion_bridge.hda`, а в сети SOP по
**Tab** — нода **Gaea Erosion Bridge**.

HDA устанавливается в пользовательскую папку `otls`. Чтобы удалить ассет, удалите оттуда
файл `gaea_erosion_bridge.hda`.

## Использование

### В Houdini: экспорт

1. Соберите террейн, например `heightfield` ▸ `heightfield_noise`.
2. Создайте **Gaea Erosion Bridge** и подключите HeightField к его входу.
3. На вкладке **Paths** укажите **Bridge Dir** (по умолчанию `C:/Temp/gh_bridge`).
   Для кнопки **Open Project in Gaea** дополнительно задайте **Gaea Project** (`.terrain`)
   и **Gaea Exe**.
4. Нажмите **1 ▸ Export to Gaea**. В `Bridge Dir` появятся `input_height.exr`,
   `gh_bridge_manifest.json` и пустая папка `output/`.

### В Gaea: сборка

1. Откройте проект (вручную или кнопкой **Open Project in Gaea**).
2. Подключите `input_height.exr` через ноду **File** и соберите граф эрозии.
3. На нужные выходы повесьте ноды **Export**, указав папку `<Bridge Dir>/output/` и имена
   файлов из таблицы ниже.
4. Нажмите **Build (F8)**.

| Слой в Houdini | Файл в `<Bridge Dir>/output/` |
|----------------|-------------------------------|
| `height`   | `output_height.exr` |
| `erosion`  | `output_erosion.exr` |
| `flow`     | `output_flow.exr` |
| `wear`     | `output_wear.exr` |
| `sediment` | `output_sediment.exr` |

Подключать все выходы не обязательно: лишние слои можно выключить на вкладке
**Import Layers**, и мост не будет их ждать.

### В Houdini: импорт

Нажмите **2 ▸ Import Results**. Нода выдаст HeightField с найденными слоями, а в поле
**Status** будет список импортированных и пропущенных слоёв.

### Параметры ноды

| Параметр | Что делает |
|----------|------------|
| **Gaea Preset** | Какой граф вы собираетесь строить в Gaea (Erosion / Hydro / Thermal / Texturing). |
| **Resolution** | 1k / 2k / 4k / 8k. |
| **Seed**, **Simulation Controls** | Duration, Erosion Scale, Sedimentation, Rainfall. |
| **Import Layers** | Какие слои ожидать при импорте. |
| **Paths** | Рабочая папка моста, проект Gaea, путь к `Gaea.exe`. |

> **Важно:** Gaea Preset, Seed и Simulation Controls **не управляют Gaea напрямую** —
> граф собирается и настраивается вручную в GUI. Эти значения записываются в манифест
> как описание задачи и входят в хэш кэша: при их изменении мост сочтёт результат
> устаревшим и попросит пересобрать.

### Кэш

При повторном **Export** мост сравнивает сигнатуру (хэш входного EXR, разрешение,
параметры, набор слоёв) с манифестом. Если всё совпадает и выходные файлы на месте,
в Status будет сообщение, что пересборка в Gaea не нужна. Любое изменение рельефа или
параметров сбрасывает кэш.

### Без HDA: шелф-инструменты

Модулем можно пользоваться и напрямую, например из шелфа:

```python
import houdini_gaea_bridge as ghb

ghb.run_from_selection()      # экспорт выделенной HeightField-ноды
# ... сборка в Gaea (F8) ...
ghb.import_from_selection()   # импорт результатов рядом с выделенной нодой
```

## Конфигурация

Значения по умолчанию можно задать без правки кода: положите `gh_bridge_config.json`
рядом с `houdini_gaea_bridge.py`. Все поля необязательны.

```json
{
  "bridge_dir": "C:/Temp/gh_bridge",
  "resolution": 4096,
  "gaea_exe": "C:/Program Files/QuadSpinner/Gaea 2.0/Gaea.exe",
  "gaea_project": "D:/Projects/erosion.terrain",
  "output_maps": {
    "height": "output_height.exr",
    "flow": "output_flow.exr"
  }
}
```

Приоритет (от низшего к высшему): значения в коде → `gh_bridge_config.json` →
параметры ноды HDA.

## Разработка и тесты

Тесты покрывают логику, не зависящую от `hou`: конфиг и его оверрайды, построение путей,
хэш-сигнатуру, манифест, `is_cache_valid` и безопасность `cleanup_temp`.

Их можно запустить любым Python 3.7+:

```bash
python tests/test_bridge_logic.py -v
```

или через `hython` из поставки Houdini, если отдельного Python в системе нет:

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tests/test_bridge_logic.py -v
```

Ожидаемый результат: `Ran 15 tests ... OK`.

## Ограничения

- Сборка в Gaea выполняется вручную в GUI — мост не запускает расчёт сам.
- Имена выходных файлов в нодах Export Gaea должны совпадать с ожидаемыми (см. таблицу
  или `output_maps` в конфиге).

---

Проект не связан с QuadSpinner и SideFX. Gaea и Houdini являются товарными знаками
соответствующих правообладателей.
