# GH-Bridge — мост Gaea ⇄ Houdini

[English](README.md) | **Русский**

Нода Houdini, которая отправляет HeightField в **Gaea 2**, собирает проект Gaea через
`Gaea.Swarm.exe` (Gaea не открывается) и возвращает в Houdini эродированный рельеф, его
маски и карты тинтов.

```
HeightField ─▶ input_height.exr ─▶ Gaea: File ─▶ Erosion2 ─▶ Export-ноды
Рельеф + маски ◀── output/output_*.exr ◀──────────────────────────┘
```

## Возможности

- **Одна кнопка**: Export > Build > Import, в фоне.
- **Четыре выхода**: Terrain, Flow, Wear, Sediment (маски готовы для HeightField-нод).
- **Параметры Erosion2 и Tint в Houdini**: меняете и пересобираете; сам проект Gaea не
  меняется (собирается копия).
- **Вход маски**: второй вход ограничивает, где Gaea размывает (любая маска или геометрия).
- **Масштаб 1:1**, разрешение экспорта Full–1/4, сборка до 16K, кэш сборок.
- Houdini 19.5+ (проверено на 20.5), Gaea 2.3, Windows.

## Установка

1. Сделайте модуль видимым для Houdini: добавьте в `houdini.env`
   ```
   PYTHONPATH = "C:/path/to/BridgeHGH;$PYTHONPATH"
   ```
   или скопируйте `houdini_gaea_bridge.py` в `<Documents>/houdiniXX.X/scripts/python/`.
2. Соберите HDA в Python Shell Houdini (повторять после обновлений):
   ```python
   exec(open(r"C:/path/to/BridgeHGH/build_gaea_bridge_hda.py").read())
   ```
   В меню Tab появится нода **Gaea Erosion2 Bridge**.

## Проект Gaea (один раз)

`File` (`<Bridge Dir>/input_height.exr`) → **Erosion2** → Export-ноды с именами:

| Выход Erosion2 | Export-нода |
|----------------|-------------|
| Out / Flow / Wear / Deposits | `output_height` / `output_flow` / `output_wear` / `output_sediment` |
| то же через ноду **Tint** | `output_tint_height` / `_flow` / `_wear` / `_sediment` |
| порт Mask ← вторая `File`-нода | читает `<Bridge Dir>/input_mask.exr` |

У Export-нод: Format EXR, Location Custom, Output Path `<Bridge Dir>/output`. При
включённых Use Erosion Template или Use Tint Parameters мост выставляет пути сам.
Сохраните проект.

## Использование

1. Подключите к ноде HeightField (и при желании маску).
2. Вкладка **Paths**: Bridge Dir, Gaea Project (`.terrain`), Gaea Exe.
3. Нажмите **Export > Build > Import**.

Вкладки:

- **Bridge** — разрешение экспорта, опции сборки, пошаговые кнопки, Auto Import.
- **Erosion** — *Use Erosion Template* открывает все параметры Erosion2; *Auto Build on
  Change* пересобирает после каждой правки (нода заблокирована до импорта).
- **Tint** — *Use Tint Parameters* открывает Tint каждого слоя. Чтобы увидеть тинт,
  выберите его в меню **Tint** и нажмите **Load Tint** (повторное нажатие
  перезагружает; None или **Reset** скрывает). Тинт показывается как маска Terrain
  (яркость карты).
- **Gaea Layers** — превью Show Mask, для каждой маски Normalize / Range / Gamma / Blur,
  Keep Input Resolution.

## Тесты

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tests/test_bridge_logic.py
```

`tools/houdini_compat_check.py` проверяет, есть ли в версии Houdini всё, что нужно мосту.

## Ограничения

- Сам граф Gaea редактируется в Gaea; из Houdini меняются только значения Erosion2/Tint.
- Формат `.terrain` не публичный API; новая версия Gaea может потребовать обновления.

---

Проект не связан с QuadSpinner или SideFX. Gaea и Houdini — товарные знаки их владельцев.
