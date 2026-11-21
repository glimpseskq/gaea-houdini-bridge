# GH-Bridge — мост Gaea ⇄ Houdini

[English](README.md) | **Русский**

Ноды Houdini, которые отправляют HeightField в **Gaea 2**, собирают проект Gaea через
`Gaea.Swarm.exe` (Gaea не открывается) и возвращают результат в Houdini: эродированный
рельеф с масками (Erosion2 / Erosion) или цветовую карту (SuperColor).

```
HeightField ─▶ input_height.exr ─▶ Gaea: File ─▶ Erosion2 / SuperColor ─▶ Export-ноды
Рельеф + маски / цвет ◀── output/output_*.exr ◀─────────────────────────────┘
```

## Возможности

- **Одна кнопка**: Build (экспорт > Gaea > импорт), в фоне; **Build Chain** собирает
  по порядку все ноды-мосты выше, неизменённые берутся из кэша.
- **Эрозионные ноды, четыре выхода**: Terrain, Flow, Wear, Sediment (маски готовы для
  HeightField-нод).
- **Нода SuperColor**: цветовая карта как текстурированный меш (или цвет точек) и как
  слои `color_r/g/b` HeightField; выбранная палитра Gaea видна прямо в Houdini.
- **Параметры ноды Gaea в Houdini**: меняете и пересобираете; сам проект Gaea
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
   В меню Tab появятся три ноды: **Gaea Erosion2 Bridge**, **Gaea Erosion Bridge**
   (старая нода Erosion) и **Gaea SuperColor Bridge**. Работают одинаково, у каждой
   параметры своей ноды Gaea.

## Проект Gaea (один раз)

`File` (любой путь, мост выставит сам) → **Erosion2** / **Erosion** / **SuperColor** →
Export-ноды с именами:

| Выход Gaea | Export-нода |
|------------|-------------|
| Erosion2 Out / Flow / Wear / Deposits | `output_height` / `output_flow` / `output_wear` / `output_sediment` |
| цветовая карта (ноды Tint → Combine), необязательно | `output_tint_combine` |
| SuperColor Out | `output_supercolor` |
| порт Mask / Texture ← вторая `File`-нода | читает `<Bridge Dir>/input_mask.exr` |

Формат экспорта EXR. Мост собирает копию проекта и сам выставляет в ней все пути
(входной File, File маски, Export-ноды), так что сохранённый проект не меняется и не
обязан указывать на папку моста. Каждая нода-мост работает в своей папке
(по умолчанию `C:/Temp/gh_bridge/<имя ноды>`).

## Использование

1. Подключите к ноде HeightField (и при желании маску).
2. Вкладка **Paths**, один раз: **Gaea Exe** (общий для всех нод-мостов). Проект каждая
   нода находит сама (`erosion2.terrain` / `erosion.terrain` / `supercolor.terrain`,
   иначе любой проект с такой нодой) в папке сцены или рядом с
   `houdini_gaea_bridge.py`, включая подпапки;
   **Gaea Project** задавайте только ради конкретного файла. **Check Paths** показывает,
   что используется.
3. Нажмите **Build**. Если ноды-мосты идут одна за другой, нажмите **Build Chain** на
   последней.

Вкладки (на каждой одна строка Build | Build Chain | Cancel и Status):

- **Bridge** — разрешение экспорта и сборки, слой маски; *Gaea GUI Workflow*
  (Export / Open in Gaea / Import, Auto Import) для сборки в окне Gaea;
  **Clear Import** и **Clear Cache**, который удаляет все файлы ноды из её Bridge Dir.
- **Erosion** / **SuperColor** — *Use ... Template* открывает все параметры ноды Gaea
  из проекта; *Auto Build on Change* пересобирает после каждой правки (нода
  заблокирована до импорта). У SuperColor видна выбранная палитра (читается из
  `Resources/Color/*.clut` Gaea); **Choose Palette...** открывает окно со всеми
  палитрами библиотеки в виде миниатюр, клик выбирает.
- **Tint** (эрозионные ноды) — *Build Tint* (по умолчанию выключена) собирает
  `output_tint_combine` с настройками Tint из самого проекта; без галки ноды тинта в
  сборку не попадают вообще. **Show / Hide Tint** превращает выход Terrain в такой же
  цветной меш, как у ноды SuperColor (*Color Mode*, *Mesh Resolution*); в скрытом
  состоянии это обычный HeightField.
- **Gaea Layers** (эрозионные ноды) — превью Show Mask, для каждой маски Normalize /
  Range / Gamma / Blur, Keep Input Resolution.
- **Color** (SuperColor) — выход 1: HeightField полигонами с цветовой картой как
  текстурой (*Color Mode: Texture*, Principled Shader внутри ноды) или цветом точек
  (*Point Color*); *Mesh Resolution* ограничивает число полигонов по оси. Выход 2:
  HeightField со слоями `color_r` / `color_g` / `color_b`.

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
