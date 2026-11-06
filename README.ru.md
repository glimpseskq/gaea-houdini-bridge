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
- **Пропорции 1 в 1** — высоты пересчитываются под размер мира Gaea, поэтому рельеф
  выглядит одинаково в обеих программах и возвращается в Houdini в исходном масштабе.
- **Управление разрешением** — экспорт в полном разрешении HeightField или в 1/2, 1/3, 1/4.
- **Кэш по хэшу** — мост считает сигнатуру входа, разрешения и набора слоёв. Если ничего
  не изменилось и выходы Gaea уже есть, он сообщает, что пересборка не нужна.
- **Манифест задачи** — `gh_bridge_manifest.json` описывает, что было выгружено, в каком
  разрешении и с какой кодировкой высот.
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
3. На вкладке **Paths** укажите **Bridge Dir** (по умолчанию `C:/Temp/gh_bridge`) и
   **Gaea Project** (`.terrain`; из него мост читает размер мира Gaea). Для кнопки
   **Open Project in Gaea** дополнительно задайте **Gaea Exe**.
4. Выберите **Resolution**: Full, 1/2, 1/3 или 1/4 от разрешения HeightField.
5. Нажмите **1. Export to Gaea**. В `Bridge Dir` появятся `input_height.exr`,
   `gh_bridge_manifest.json` и пустая папка `output/`.

   Слой `height` записывается как серый RGB EXR. Высоты пересчитываются под размер мира
   Gaea, чтобы пропорции рельефа совпадали 1 в 1 (см. [Масштаб высот](#масштаб-высот)).

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

Нажмите **2. Import Results**. Нода выдаст HeightField с найденными слоями, а в поле
**Status** будет список импортированных и пропущенных слоёв. Рельеф возвращается в
исходном размере и по высоте; разрешение равно разрешению картинок, которые выгрузила
Gaea.

### Параметры ноды

| Параметр | Что делает |
|----------|------------|
| **Resolution** | Full / 1/2 / 1/3 / 1/4 от разрешения входного HeightField. Размер террейна не меняется, уменьшается только число пикселей (HeightField 500×500 на 1/2 выгрузится как 250×250). |
| **Import Layers** | Какие слои ожидать при импорте. |
| **Paths** | Рабочая папка моста, проект Gaea, путь к `Gaea.exe`. |
| **Gaea Terrain Size** | Ширина и высота мира Gaea в метрах. 0 = взять из проекта Gaea. |

Сама эрозия настраивается в GUI Gaea, нода только переносит данные. Gaea собирает в
разрешении из своего Terrain Definition — уменьшите и его, если нужны быстрые сборки.

### Масштаб высот

Gaea показывает значение пикселя `v` (0..1) как `v × Height` метров на террейне шириной
`Width` метров (Project ▸ Terrain Definition). Мост кодирует высоты Houdini так, чтобы
соотношение высоты и ширины в обеих программах было одинаковым:

```
span = ширина_houdini × gaea_height / gaea_width
v    = (h − min) / span        # экспорт
h    = v × span + min          # импорт
```

Размер мира Gaea берётся по порядку: параметры **Gaea Terrain Size** на ноде (если не 0)
→ файл `.terrain` из **Gaea Project** → 5000 × 2500 м. Кодировка сохраняется в манифест
(`height_encoding`), и **Import** по ней восстанавливает исходные высоты, так что рельеф
возвращается в масштабе Houdini. Если рельеф выше, чем позволяет Height в Gaea, в Status
появится предупреждение с минимальной высотой, которую нужно выставить.

Совет: эрозия в Gaea зависит от размера мира. Для физически корректного результата
выставьте ширину в Terrain Definition равной размеру террейна в Houdini — мост подстроится
сам.

### Кэш

При повторном **Export** мост сравнивает сигнатуру (хэш входного EXR, разрешение, набор
слоёв) с манифестом. Если всё совпадает и выходные файлы на месте, в Status будет
сообщение, что пересборка в Gaea не нужна. Любое изменение рельефа, разрешения или
размера мира Gaea сбрасывает кэш.

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
  "downsample": 2,
  "gaea_exe": "C:/Program Files/QuadSpinner/Gaea 2.0/Gaea.exe",
  "gaea_project": "D:/Projects/erosion.terrain",
  "gaea_width": 0,
  "gaea_height": 0,
  "output_maps": {
    "height": "output_height.exr",
    "flow": "output_flow.exr"
  }
}
```

`downsample`: 1 (полное), 2 (1/2), 3 (1/3) или 4 (1/4). `gaea_width`/`gaea_height`, равные
0, означают «взять из `gaea_project`».

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

Ожидаемый результат: `Ran 27 tests ... OK`.

## Ограничения

- Сборка в Gaea выполняется вручную в GUI — мост не запускает расчёт сам.
- Имена выходных файлов в нодах Export Gaea должны совпадать с ожидаемыми (см. таблицу
  или `output_maps` в конфиге).

---

Проект не связан с QuadSpinner и SideFX. Gaea и Houdini являются товарными знаками
соответствующих правообладателей.
