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
- **Параметры Erosion2 / Erosion в Houdini**: меняете и пересобираете; сам проект Gaea
  не меняется (собирается копия).
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
   В меню Tab появятся две ноды: **Gaea Erosion2 Bridge** (для проекта с Erosion2) и
   **Gaea Erosion Bridge** (для старой ноды Erosion). Работают одинаково, у каждой
   параметры своей ноды.

## Проект Gaea (один раз)

`File` (любой путь, мост выставит сам) → **Erosion2** (или **Erosion**) → Export-ноды
с именами:

| Выход Erosion2 | Export-нода |
|----------------|-------------|
| Out / Flow / Wear / Deposits | `output_height` / `output_flow` / `output_wear` / `output_sediment` |
| цветовая карта (ноды Tint → Combine), необязательно | `output_tint_combine` |
| порт Mask ← вторая `File`-нода | читает `<Bridge Dir>/input_mask.exr` |

Формат экспорта EXR. Мост собирает копию проекта и сам выставляет в ней все пути
(входной File, File маски, Export-ноды), так что сохранённый проект не меняется и не
обязан указывать на папку моста. Каждая нода-мост работает в своей папке
(по умолчанию `C:/Temp/gh_bridge/<имя ноды>`).

## Использование

1. Подключите к ноде HeightField (и при желании маску).
2. Вкладка **Paths**, один раз: **Gaea Exe** (общий для всех нод-мостов). Проект каждая
   нода находит сама (`erosion2.terrain` / `erosion.terrain`, иначе любой проект с такой
   нодой) в папке сцены или рядом с `houdini_gaea_bridge.py`, включая подпапки;
   **Gaea Project** задавайте только ради конкретного файла. **Check Paths** показывает,
   что используется.
3. Нажмите **Export > Build > Import**.

Вкладки:

- **Bridge** — разрешение экспорта, опции сборки, пошаговые кнопки, Auto Import.
- **Erosion** — *Use Erosion Template* открывает все параметры ноды эрозии проекта
  (Erosion2 или Erosion, смотря какая нода-мост); *Auto Build on
  Change* пересобирает после каждой правки (нода заблокирована до импорта).
- **Tint** — *Build Tint* (по умолчанию выключена) собирает `output_tint_combine` с
  настройками Tint из самого проекта; без галки ноды тинта в сборку не попадают вообще. **Show / Hide Tint**
  показывает карту как маску Terrain (яркость).
- **Gaea Layers** — превью Show Mask, для каждой маски Normalize / Range / Gamma / Blur,
  Keep Input Resolution.

## Тесты

```powershell
& "C:\Program Files\Side Effects Software\Houdini XX.X.XXX\bin\hython.exe" tests/test_bridge_logic.py
```

`tools/houdini_compat_check.py` проверяет, есть ли в версии Houdini всё, что нужно мосту.

## Ограничения

- Сам граф Gaea редактируется в Gaea; из Houdini меняются только значения ноды эрозии.
- Формат `.terrain` не публичный API; новая версия Gaea может потребовать обновления.

---

Проект не связан с QuadSpinner или SideFX. Gaea и Houdini — товарные знаки их владельцев.
