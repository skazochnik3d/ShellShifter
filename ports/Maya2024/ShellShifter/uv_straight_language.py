"""Tooltip language for the English ShellShifter interface."""
from html import escape, unescape
import re

import maya.cmds as cmds
from PySide2 import QtCore, QtGui, QtWidgets
from uv_straight_qt5 import signals_blocked


OPTION = 'shellShifterLanguage'
TOOLTIP_TEXT_WIDTH = 310
_locale = None

TOOLTIPS = {
    'Pins': 'Показать пины',
    'Show Done': 'Показать готовые меши',
    'Texel': 'Тексель',
    'Texel Groups': 'Группы текселя',
    'Keep Texel Groups': 'Сохранять тексель групп',
    'Pack at the exact Texel Group densities. If the shells do not fit, UVs stay unchanged.': 'Пакует с заданным текселем групп. Если шеллы не помещаются, UV остаются без изменений.',
    'Shell Groups': 'Группы шеллов',
    'Texture Transfer': 'Настройки переноса текстур',
    'Transfer & Connect Textures': 'Перенести и подключить текстуры',
    'Select in Tile': 'Выделить шеллы в тайле',
    'Assign…': 'Назначить текстуры…',
    'Clear Material Textures': 'Отсоединить все карты материала',
    'Convert': 'Конвертировать текстуры',
    'View': 'Показать типы карт',
    'Refresh Assigned Materials': 'Обновить список материалов',
    'Toggle H Pin Axis': 'Переключить горизонтальные пины',
    'Toggle V Pin Axis': 'Переключить вертикальные пины',
    'Restore the default keymaps and pie menu layout. Use Apply Keymaps to save shortcut changes.': 'Вернуть стандартные хоткеи и раскладку пай-меню. Нажми Apply Keymaps, чтобы сохранить изменения хоткеев.',
    'Show or hide the overlap results. The report and selection are kept.': 'Показать или свернуть результаты проверки наложений. Отчёт и выделение сохраняются.',
    'Unwrap': 'Развёртка',
    'Unwrap Mesh': 'Развернуть меш',
    'Unfold': 'Развернуть',
    'Relax': 'Релакс',
    'Flip': 'Отразить',
    'Rotate': 'Повернуть',
    'Packing': 'Упаковка',
    'Pack UVs': 'Упаковать UV',
    'Choose UV Tile': 'Выбрать тайл',
    'To Square': 'В квадрат',
    'To Rectangle': 'В прямоугольник',
    'H / V Pins': 'Пины H / V',
    'Unpin': 'Снять',
    'Edit Pins': 'Редактировать пины',
    'Straighten Quads': 'Квады',
    'Orient Shells': 'Ориентация',
    'Checker': 'Чекер',
    'Apply Checker': 'Нанести чекер',
    'Gray Squares': 'Серые квадраты',
    'Color Labels': 'Цветные метки',
    'Remove Checker': 'Убрать чекер',
    'Checker Tiling': 'Повторы чекера',
    'Display': 'Отображение',
    'Wireframe': 'Каркас',
    'Show Pins': 'Показать пины',
    'Show Color': 'Показать цвет',
    'Clear Color': 'Убрать цвет',
    'Get': 'Взять',
    'Set': 'Зад.',
    'Clear': 'Сброс',
    'Materials': 'Материалы',
    'UV Checks': 'Проверка UV',
    'Find Small / Thin Shells': 'Найти мелкие и тонкие шеллы',
    'Check Hard Edges': 'Проверить жёсткие рёбра',
    'Select Overlaps': 'Выделить наложения',
    'Fix Butterfly Overlaps': 'Разрезать бабочки',
    'Fix Butterfly': 'Разрезать бабочки',
    'Bad Overlaps': 'Плохие наложения',
    'Cleanup': 'Очистить UV-наборы',
    'Finished Unwrap Meshes': 'Меши с готовой развёрткой',
    'Mark Done': 'Готово',
    'Clear Done': 'Снять отметку',
    'Move Overlaps': 'Сдвиг наложений',
    'Set Selection as Original': 'Назначить оригиналы',
    'Set Selected': 'Назначить',
    'Clear Originals': 'Сбросить оригиналы',
    'Analyse': 'Анализ',
    'Stack Shells': 'Совместить',
    'Analyse Select': 'Анализ и выбор',
    'Updates': 'Обновления',
    'Check for Updates': 'Проверить обновления',
    'Open ShellShifter': 'Открыть ShellShifter',
    'Keymaps': 'Горячие клавиши',
    'H: orange | V: cyan | shared angle: white\nWheel: zoom. Drag: pan. Refresh: frame all.': 'H: оранжевый | V: голубой | общий угол: белый\nКолесо — масштаб. Перетаскивание — панорама. Refresh — показать всё.',
    'Pin this section open': 'Закрепить раздел открытым.',
    'Drag to move the window': 'Перетащи, чтобы переместить окно.',
    'Pin the window to keep it open while working in Maya': 'Закрепить окно, чтобы оно оставалось открытым при работе в Maya.',
    'Close window': 'Закрыть окно.',
    'Mark selected meshes as done. Show Done displays them in green. The mark is saved in the scene.': 'Пометить выбранные меши как готовые. Show Done показывает их зелёным. Отметка сохраняется в сцене.',
    'Clear done marks and hide the yellow Originals highlight on selected meshes. This is saved with the scene.': 'Снять отметку готовности и скрыть жёлтую подсветку оригиналов на выбранных мешах. Изменения сохраняются в сцене.',
    'Show green completion colors on marked meshes. Turning this off keeps their marks.': 'Показать зелёную подсветку готовых мешей. При выключении отметки сохраняются.',
    'Change the checker texture while preserving checker density.': 'Сменить текстуру чекера с сохранением его плотности.',
    'Add a custom texture to the checker library': 'Добавить свою текстуру в библиотеку чекеров.',
    'Straighten selected quad grids and suitable strips with triangles or n-gons.\nStrips preserve UV area and receive H/V guides along their long sides.\nStrips then run constrained Unfold; quad grids do not.': 'Выпрямить выбранные сетки из квадов и подходящие полосы с треугольниками или n-гонами.\nУ полос сохраняется площадь UV, а вдоль длинных сторон добавляются H/V-направляющие. После этого Unfold запускается для полос с направляющими.',
    'Rotate the whole shell to the nearest U or V axis, preserving shape and scale.\nTwo selected UV points define a line; larger selections use selected boundary directions.\nRotation is at most 45 degrees. A single selected UV uses the shell boundary.': 'Повернуть весь шелл к ближайшей оси U или V с сохранением формы и масштаба.\nДве выбранные UV-точки задают линию. При большем выделении учитываются направления границ.\nПоворот ограничен 45°. При одной выбранной точке используется граница шелла.',
    'Preserve existing UV seams, cut selected and hard edges, Unfold without pins, then Orient Shells and pack.\nH/V pins are not assigned automatically. Existing pins are removed.': 'Сохранить существующие UV-швы, разрезать по выбранным и жёстким рёбрам, выполнить Unfold без пинов, затем ориентировать и упаковать шеллы.\nСуществующие пины удаляются.',
    'Unfold the selection. Shift + wheel pins make Unfold affect the entire shell.': 'Развернуть выделенные UV. С пинами Shift + колесо разворачивается весь шелл.',
    'Reduce distortion in the selection. Shift + wheel pins make Relax affect the entire shell.': 'Уменьшить искажения выделенных UV. С пинами Shift + колесо обрабатывается весь шелл.',
    'Shared rotation angle for selected shells about their combined UV-vertex center.': 'Общий угол поворота выбранных шеллов вокруг центра всех их UV-вершин.',
    'Show or hide wireframe on selected shaded meshes in 3D views.\nUVs, selection and preview colors are preserved.': 'Показать или скрыть каркас выбранных мешей в 3D-видах.\nUV, выделение и цвета предпросмотра сохраняются.',
    'Show H/V pins in the UV Editor.': 'Показать пины H/V в UV Editor.',
    'Show distortion on selected visible objects in real time. The UV Editor shows selected meshes or shells. Deselect to clear the display.': 'Показывать искажения на выделенных видимых объектах в реальном времени. UV Editor отображает выбранные меши или шеллы. Сними выделение, чтобы убрать подсветку.',
    'Click an edge to toggle pins.\nH/V: choose axis. Both: auto. Neither: off.\nShift + wheel: pin by angle.': 'Клик по ребру включает или снимает пины.\nH/V: выбор оси. Обе: авто. Ни одной: выкл.\nShift + колесо: пины по углу.',
    'Get average texel density of selected shells, with equal weight per shell': 'Получить средний тексель выбранных шеллов. Каждый шелл имеет одинаковый вес.',
    'Show texel density on selected meshes. Colors and groups update after UV edits, geometry changes and Undo/Redo until Clear Color is clicked.': 'Показать тексель на выбранных мешах. Цвета и группы обновляются после изменения UV, геометрии и Undo/Redo до нажатия Clear Color.',
    'Select whole shells whose overall dimension or local polygon-strip width is below the threshold.': 'Выделить целиком шеллы, у которых общий размер или ширина отдельной полосы полигонов ниже порога.',
    'Select whole UV shells collapsed to a line or point in the current UV set.': 'Выделить шеллы, схлопнутые в линию или точку в текущем наборе UV.',
    'Find hard edges without UV cuts. Use the arrows to frame one edge at a time in the viewport.': 'Найти жёсткие рёбра без UV-швов. Стрелки приближают каждое ребро по очереди в 3D-вьюпорте.',
    'Focus the previous hard edge without a UV cut.': 'Приблизить предыдущее жёсткое ребро без UV-шва.',
    'Focus the next hard edge without a UV cut.': 'Приблизить следующее жёсткое ребро без UV-шва.',
    'Restore sewn seams where neighboring UV faces fold onto the same side. Cut and select shell boundaries on selected meshes or fully selected shells. UVs stay in place; current UV sets are used. Supports Undo.': 'Восстановить швы там, где соседние UV-полигоны сложились на одну сторону. Разрезать и выделить границы на выбранных мешах или полностью выделенных шеллах. Положение UV сохраняется. Используются текущие наборы UV. Поддерживается Undo.',
    'Distance moved per arrow click, in UV tiles.': 'Расстояние, на которое стрелка сдвигает шеллы за одно нажатие, в UV-тайлах.',
    'Add selected meshes or UV shells to the originals.': 'Добавить выбранные меши или UV-шеллы в список оригиналов.',
    'Remove only selected meshes or UV shells from the originals.': 'Убрать из списка оригиналов выбранные меши или UV-шеллы.',
    'Show assigned originals in yellow in the model viewport.': 'Подсветить назначенные оригиналы жёлтым в окне 3D-вида.',
    'Find overlapping copies only among selected UV shells, in any tiles.\nOne source remains in each stack; flipped copies are selected first.\nSelecting a mesh or group checks all its shells. UVs are not moved.': 'Найти наложенные копии среди выделенных шеллов в любых тайлах.\nВ каждом стаке остаётся один оригинал, зеркальные копии выделяются первыми.\nПри выборе меша или группы проверяются все их шеллы. Положение UV сохраняется.',
    'Select whole visible shells inside a tile on selected meshes or in a group.\nPoint at a tile in the UV Editor and left-click. Esc or right-click cancels.\nShells crossing tile boundaries are not selected.': 'Выделить целые видимые шеллы внутри тайла на выбранных мешах или в группе.\nНаведи курсор на тайл и нажми ЛКМ. Esc или ПКМ отменяет выбор.\nШеллы, пересекающие границу тайла, пропускаются.',
    'Square texture size: 2, 4, 8, …, 4096, 8192. Arrows double or halve the size.': 'Размер квадратной текстуры: 2, 4, 8, …, 4096, 8192. Стрелки удваивают или уменьшают размер вдвое.',
    'Square: U 0–1, V 0–1. Half (lower half): U 0–1, V 0–0.5.\nShell proportions are preserved.\nThe area is relative to the selected tile. Gap and Margin are calculated in pixels using Size.': 'Square: U 0–1, V 0–1. Half: U 0–1, V 0–0,5.\nПропорции шеллов сохраняются.\nОбласть отсчитывается от выбранного тайла. Gap и Margin рассчитываются в пикселях по значению Size.',
    'Unlimited searches until Stop and Keep Best is clicked. Set a time in seconds to limit the search. The best complete layout is always retained.': 'Искать раскладку до нажатия Stop and Keep Best. Можно задать лимит в секундах. Лучшая полная раскладка сохраняется.',
    'Completed iteration limit; 0 is unlimited. One batch of GPU candidates counts as one iteration. The first time or iteration limit reached stops the search and retains the best complete layout.': 'Лимит завершённых итераций. 0 означает поиск без лимита. Один пакет вариантов на GPU считается одной итерацией. Поиск остановится по первому достигнутому лимиту времени или итераций.',
    'Rebuild textures for a new UV layout.\n'
    'Same Mesh: enable before Pack UVs to save the original UVs.\n'
    'Duplicate Mesh: pick Source with old UVs and textures, then Target with new UVs. No packing needed.\n'
    'Choose a resolution and output folder, then click Transfer & Connect Textures.': (
        'Пересчитать текстуры под новую развёртку.\n'
        'Same Mesh — включи перед Pack UVs, чтобы сохранить исходные UV того же меша.\n'
        'Duplicate Mesh — укажи в Source меш со старыми UV и текстурами, а в Target — меш с новыми UV. Паковка не нужна.\n'
        'Выбери разрешение и папку сохранения. Нажми Transfer & Connect Textures, чтобы подключить новые карты.'),
    'Same Mesh saves the original UVs when you pack. Duplicate Mesh uses the Source and Target you pick.': 'Same Mesh сохраняет старые UV при паковке. В Duplicate Mesh выбери Source со старыми UV и Target с новыми.',
    'Rebuild textures from saved UVs and connect them to the same mesh.': 'Пересчитать текстуры по сохранённым UV и подключить их к тому же мешу.',
    'Transfer textures from the selected Source UVs to Target UVs. No packing is required. Source and unrelated objects keep their materials.': 'Пересчитать карты с UV меша Source на UV меша Target. Паковка не нужна. Новые карты подключаются только к Target.',
    'Pack selected whole shells into the target tile, respecting visible fixed shells on selected meshes and UV Area. Unselected shells stay fixed. Selecting an object includes all its visible shells.\nRunning again with the same shells and settings continues from the best result. UV, geometry, visibility or material changes reset preparation.': 'Упаковать целые выделенные шеллы в целевой тайл с учётом неподвижных видимых шеллов и UV Area. При выборе объекта учитываются все его видимые шеллы.\nПовторный запуск с теми же шеллами и настройками продолжает поиск от лучшего результата. Изменения UV, геометрии, видимости или материалов запускают подготовку заново.',
    'Choose the packing target: point at a whole tile in the UV Editor and left-click. Esc or right-click cancels. Shell selection is preserved.': 'Выбрать тайл для паковки. Наведи курсор на тайл в UV Editor и нажми ЛКМ. Esc или ПКМ отменяет выбор. Выделение шеллов сохраняется.',
    'Stretch selected shells from the lower half to the full square: V ×2 about V=0; U is unchanged. Selecting any part includes the whole shell. One Undo restores the layout.': 'Растянуть шеллы из нижней половины тайла на весь квадрат. V удваивается относительно V=0, U сохраняется. Частичное выделение включает весь шелл. Поддерживается Undo.',
    'Compress selected shells into the lower half: V ×0.5 about V=0; U is unchanged. Selecting any part includes the whole shell. One Undo restores the layout.': 'Сжать шеллы в нижнюю половину тайла. V уменьшается вдвое относительно V=0, U сохраняется. Частичное выделение включает весь шелл. Поддерживается Undo.',
    'A preliminary estimate is shown during the search. The final result uses the UV union area: overlapping regions count once. Padding is included in free space.': 'Во время поиска показана оценка. В итоговом расчёте пересечения считаются один раз, а отступы входят в свободное место.',
    'GPU: completed search cycles; one GPU batch counts once. CPU: completed independent trials. The two counters are shown separately.': 'GPU считает завершённые циклы поиска по одному на пакет вариантов. CPU считает независимые попытки. Счётчики показаны отдельно.',
    'Total complete layouts from GPU and CPU. Improvements count changes to their shared best result, including accepted CPU improvements once.': 'Число полных раскладок с GPU и CPU. Каждое улучшение общего лучшего результата учитывается один раз.',
    'Stop searching and apply the best complete layout. If Fill Gaps is enabled, a final pass redistributes small shells; that pass can be stopped separately.': 'Остановить поиск и применить лучшую полную раскладку. Если включён Fill Gaps, заключительное перераспределение мелких шеллов можно остановить отдельно.',
    'Stop redistribution and apply the complete layout with the moves already made.': 'Остановить перераспределение и применить раскладку с уже выполненными перемещениями.',
    'Free area estimated from the UV union. Overlapping regions count once; padding is included in free space.': 'Свободное место оценено по объединению UV-областей. Пересечения учитываются один раз, отступы входят в свободное место.',
    'Preliminary estimate by UV groups. Fixed overlaps count once; overlaps within moving groups may affect the result.': 'Предварительная оценка по UV-группам. Фиксированные наложения считаются один раз. Наложения в подвижных группах могут влиять на оценку.',
    "Show or hide this group's shells only in the UV Editor. The 3D model remains visible.": 'Показать или скрыть шеллы группы в UV Editor. Модель остаётся видимой в 3D.',
    'Change the group color.': 'Изменить цвет группы.',
    'Target density in px/unit for both Density modes. Packing fits the layout with a shared scale. Hover to see current density.': 'Целевой тексель в пикселях на единицу при любом положении Density. Раскладка масштабируется целиком. Наведи курсор, чтобы узнать текущий тексель.',
    'Eye: shell visibility in the UV Editor. Color and value on the right: group settings. Right-click → Rename.': 'Глаз управляет видимостью шеллов. Справа — цвет и настройки группы. ПКМ → Rename переименовывает группу.',
    "Select the group's shells in the current UV sets.": 'Выделить шеллы группы в текущих наборах UV.',
    'Toggle shell colors by group. This is independent of shell visibility (the eye in each row).': 'Показать цвета групп. Видимость шеллов регулируется значком глаза в каждой строке.',
    'Rename the group.': 'Переименовать группу.',
    'Click a result to select and frame its UV shells.\nCtrl / Shift selects multiple groups.': 'Нажми на результат, чтобы выделить и показать его шеллы.\nCtrl или Shift позволяет выделить несколько групп.',
    'Select the source mesh first, then the target meshes, and click Transfer UVs.\nUV coordinates and seams from the first mesh are copied to the current UV sets of the others.\nIncompatible meshes are skipped with a notification.\nThe source stays unchanged. All changes are one Undo. Click again to cancel the search.': 'Выбери меш-источник, затем целевые меши и нажми Transfer UVs.\nКоординаты и швы UV копируются в текущие UV-наборы целей. Несовместимые меши пропускаются с уведомлением.\nИсточник сохраняется. Перенос отменяется одним Undo. Повторный клик останавливает поиск.',
    'Stack selected shells with matching topology and similar geometry.\nThe least distorted shell is the reference for the UV alignment.\nOnly current UV sets change. Click again to stop the search.': 'Совместить выделенные шеллы с одинаковой топологией и похожей геометрией.\nЗа образец берётся шелл с наименьшими искажениями. Операция применяется к текущим наборам UV. Повторный клик останавливает поиск.',
    'Refresh assigned materials': 'Обновить список назначенных материалов.',
    'Choose a material to select its UVs and view its textures.': 'Выбери материал, чтобы выделить его UV и увидеть его текстуры.',
    'Assign multiple textures by set name and map type. Drop files or a folder onto Assign or Materials. Only files directly inside a folder are read.': 'Назначить сразу несколько текстур по имени сета и типу карты. Перетащи файлы или папку на Assign или Materials. Из папки читаются файлы верхнего уровня.',
    'Disconnect all textures from the selected material. One Undo restores them.': 'Отсоединить все текстуры выбранного материала. Одно Undo восстанавливает подключения.',
    'Choose image files, conversion settings, and an output folder.': 'Выбрать файлы, настройки конвертации и папку для результата.',
    'Show or hide each map type across all texture sets.': 'Показать или скрыть каждый тип карт во всех текстурных сетах.',
    'Output size on the longest side. A 2:1 map at 2K becomes 2048 × 1024.': 'Размер по длинной стороне. Карта 2:1 при выборе 2K получится размером 2048 × 1024.',
    'Output file format': 'Формат выходного файла.',
    'Bits per channel. TGA supports 8 bit.': 'Битность канала. TGA поддерживает 8 бит.',
    'Choose the output folder and convert the selected files.': 'Выбрать папку и конвертировать выбранные файлы.',
    'Close': 'Закрыть.',
    'AO is added to Opacity only when every UDIM tile has an actual alpha channel.': 'Карта AO подключается к Opacity, если у каждого тайла UDIM есть альфа-канал.',
    'Choose RGB, an individual color channel, or the image alpha channel.': 'Выбрать RGB, отдельный цветовой канал или альфа-канал.',
    'Materials and maps are read from Source. Transfer connects new material copies only to Target.': 'Материалы и карты читаются из Source. Копии материалов подключаются к Target.',
    'Yellow: UVs unchanged; red: error; green: transfer enabled. Click a ready material to disable or re-enable its transfer. Gray: transfer cancelled or completed.': 'Жёлтый — UV без изменений. Красный — ошибка. Зелёный — перенос включён. Нажми на готовый материал, чтобы включить или исключить его. Серый — перенос отменён или завершён.',
    'Output texture width in pixels. Arrows double or halve it.': 'Ширина выходной текстуры в пикселях. Стрелки удваивают или уменьшают значение вдвое.',
    'Output texture height in pixels. Arrows double or halve it.': 'Высота выходной текстуры в пикселях. Стрелки удваивают или уменьшают значение вдвое.',
    'Link output texture width and height.': 'Связать ширину и высоту выходной текстуры.',
    'Configure green-channel direction and storage format for input and output normal maps.': 'Настроить направление зелёного канала и формат хранения исходных и выходных карт нормалей.',
    'Normal-map green-channel direction: +Y (OpenGL) or −Y (DirectX).': 'Направление зелёного канала карты нормалей: +Y (OpenGL) или −Y (DirectX).',
    'Normal-map format: three RGB channels or two RG channels with blue reconstruction.': 'Формат карты нормалей: RGB или два канала RG с восстановлением синего.',
    "Changing a material's normal-map format creates a compatible copy.": 'При смене формата нормалей создаётся совместимая копия материала.',
    'Output folder for transferred textures.': 'Папка для перенесённых текстур.',
    'Choose an output folder for textures.': 'Выбрать папку для сохранения текстур.',
    'Open the selected folder. Output PNGs are saved directly into it with unique names.': 'Открыть выбранную папку. PNG сохраняются в ней с уникальными именами.',
    'Transfer runs on the GPU. If OpenCL is unavailable, CPU is used and the report explains why. Bit depth is preserved per source map; mixed UDIMs use the highest depth. Padding rounds up to whole pixels. Input: 8/16-bit PNG/TIFF, TGA and JPEG. TIFF without alpha or float channels.': 'Перенос выполняется на GPU. Если OpenCL недоступен, используется CPU с указанием причины в отчёте. Битность берётся из исходной карты, для смешанных UDIM — максимальная. Отступ округляется вверх до целого пикселя. Входные форматы: PNG/TIFF 8 или 16 бит, TGA и JPEG. Для TIFF поддерживаются каналы без альфы и без плавающей точки.',
    'Transfer available maps of enabled materials from source UVs to repacked UVs and connect the new files. Individual map errors produce warnings; other maps are saved. Yellow Ready with Warnings indicates partial availability. Gray materials are excluded. Other materials are preserved.': 'Перенести доступные карты включённых материалов на новые UV и подключить файлы. Ошибки отдельных карт попадают в предупреждения, остальные карты сохраняются. Жёлтая строка означает частичную доступность. Серые материалы пропускаются.',
    'Add a shortcut for a ShellShifter action': 'Добавить сочетание для команды ShellShifter.',
    'Click to record a shortcut, then click Apply Keymaps. Custom mouse-button or wheel shortcuts require Ctrl, Alt or Shift.': 'Запиши сочетание и нажми Apply Keymaps. Для кнопок мыши или колеса нужен Ctrl, Alt или Shift.',
    'Already added to Keymaps': 'Команда уже добавлена в Keymaps.',
    'Click to record a new mouse shortcut. Esc cancels.': 'Записать новое сочетание с кнопкой мыши. Esc отменяет запись.',
    'Enable or disable this shortcut immediately.': 'Включить или отключить сочетание сразу.',
    'Add a UV set to preserve': 'Добавить набор UV, который нужно сохранить.',
    'The first tag is the target UV set for cleanup. Additional tags protect existing UV sets without changing their UVs. Distortion threshold is fixed at 5%.': 'Первый тег задаёт целевой UV-набор. Остальные теги защищают соответствующие наборы и их UV. Порог искажений составляет 5%.',
    'Check selected meshes or groups, merge suitable shells into the UV set from the first Keep sets tag, and delete unprotected UV sets. The entire operation supports Undo.': 'Проверить выбранные меши или группы, объединить подходящие шеллы в первый защищённый UV-набор и удалить остальные незащищённые наборы. Поддерживается Undo.',
    'Tooltip language': 'Язык подсказок',
    'Unpin section': 'Открепить раздел.',
    'Unpin the window to close it when clicking outside': 'Открепить окно. Оно будет закрываться при клике снаружи.',
    'On: normalize ungrouped shells to the base density. Off: retain their original relative sizes. Texel Group targets apply in both modes. The packer scales the complete layout together to fit.': 'Включено: шеллы вне групп приводятся к базовому текселю. Выключено: их относительные размеры сохраняются. Цели Texel Groups действуют в обоих режимах. Для вписывания вся раскладка масштабируется целиком.',
    'Allow 90-degree rotations for tighter packing. Packing favors horizontal elongated shells when Rotation is enabled. UV Stacks rotate as units. Shell shapes and mirrored orientation are preserved.': 'Разрешить повороты на 90° для плотной упаковки. Вытянутые шеллы по возможности размещаются горизонтально. Стаки поворачиваются целиком. Форма и зеркальность сохраняются.',
    'Preserve all UV overlaps: full, partial and nested. Shells connected by overlapping area move, rotate and scale as a group. Partial group selection includes overlapping visible shells on selected meshes. Spacing is measured between groups. Density normalization uses one shared factor.': 'Сохранить полные, частичные и вложенные наложения UV. Пересекающиеся шеллы перемещаются, поворачиваются и масштабируются вместе. Частичное выделение включает видимые наложенные шеллы выбранных мешей. Зазоры считаются между группами, а масштаб текселя общий для группы.',
    'Run up to 4 CPU searches alongside the GPU.\nRequires Time ≥ 5 s and unlimited Iters.': 'До 4 поисков на CPU параллельно с GPU.\nTime ≥ 5 с, Iters без лимита.',
    'After packing, fill free pockets: redistribute compact small shells, then fit narrow strips and small L-shaped shells into remaining gaps. Placement uses actual outlines and preserves minimum Gap/Margin. Large shells, scale and rotations remain unchanged. With UV Stacks, stacks move as units. This pass takes up to 10 seconds and changes distribution, not total occupied area.': 'После упаковки перераспределить мелкие компактные шеллы и заполнить оставшиеся зазоры полосами и небольшими Г-образными шеллами. Используются реальные контуры и минимальные Gap/Margin. Положение крупных шеллов, масштаб и повороты сохраняются. Стаки перемещаются целиком. Проход занимает до 10 секунд, общая занятая площадь сохраняется.',
    "Absolute: pack the group's selected visible shells as one unit. Relative positions, rotations and scale remain fixed. The entire group may scale and rotate together.": 'Absolute: упаковать выбранные видимые шеллы группы одним блоком. Взаимное положение, повороты и масштаб сохраняются. Блок может поворачиваться и масштабироваться целиком.',
    'Relative: keep shells close together, similar to Cohesion 85%. Shells may move and rotate independently for tighter packing.': 'Relative: по возможности держать шеллы рядом, примерно как при Cohesion 85%. Для плотной упаковки шеллы могут перемещаться и поворачиваться отдельно.',
    'Create a group from selected whole shells. A name is assigned automatically.': 'Создать группу из выделенных шеллов. Имя назначается автоматически.',
    'Add selected shells to the current group. They will be removed from another group of the same type.': 'Добавить выбранные шеллы в текущую группу. Они будут перенесены из другой группы того же типа.',
    'Remove selected shells only from the current group.': 'Убрать выбранные шеллы из текущей группы.',
    'Delete the group. UVs and geometry are preserved.': 'Удалить группу. UV и геометрия сохраняются.',
    'Right-click → Rename to rename the group.': 'ПКМ → Rename, чтобы переименовать группу.',
    'Expand texel density groups.': 'Раскрыть группы текселя.',
    'Expand packing groups.': 'Раскрыть группы упаковки.',
    'Current density is unavailable in the active UV set.': 'Текущий тексель недоступен в активном наборе UV.',
    'Group targets apply with Density on or off. Packing adjusts the overall scale to the available space.': 'Цели групп учитываются в обоих режимах Density. Паковка подгоняет общий масштаб под доступное место.',
    'Previous conflicting pair in this group.': 'Предыдущая конфликтующая пара в группе.',
    'Next conflicting pair in this group.': 'Следующая конфликтующая пара в группе.',
    'Drag to swap positions. Drop an action or menu item here.': 'Перетащи, чтобы поменять позиции. Сюда можно перетащить команду или пункт меню.',
    'Drag into the marking menu. Click to add.': 'Перетащи в круговое меню или нажми, чтобы добавить.',
    'Click to add a shortcut.': 'Нажми, чтобы добавить сочетание.',
    'Drop here to remove the action': 'Перетащи сюда, чтобы удалить команду',
    'Add an action to the marking menu': 'Добавить команду в круговое меню',
    'Drag to move. Drop onto the trash to remove.': 'Перетащи, чтобы переместить. Для удаления перетащи в корзину.',
    'RGB uses the full normal map. A single channel is interpreted as bump height.': 'RGB использует всю карту нормалей. Один канал интерпретируется как высота рельефа.',
    'Choose the source channel of this file.': 'Выбрать исходный канал этого файла.',
    'Expand or collapse maps for the selected material. The state is remembered.': 'Раскрыть или свернуть карты выбранного материала. Состояние сохраняется.',
    'Select a material to show its maps.': 'Выбери материал, чтобы увидеть его карты.',
    'Return to connected maps': 'Показать подключённые карты',
    'Show all texture slots': 'Показать все слоты текстур',
    'No texture connected': 'Текстура не подключена',
    'Maya shader input.': 'Вход шейдера Maya.',
    'Auxiliary material map; does not affect shading.': 'Вспомогательная карта материала. На шейдинг она не влияет.',
    'Left-click to choose a file, or drag and drop a file.': 'Выбери файл по ЛКМ или перетащи его сюда.',
    'Could not read the preview. Check the file path and image format.': 'Не удалось прочитать превью. Проверь путь и формат файла.',
    'Left-click to select UV shells in this density range.': 'Нажми ЛКМ, чтобы выделить шеллы в этом диапазоне текселя.',
    'Right-click: collapse subranges.': 'ПКМ: свернуть поддиапазоны.',
    'Right-click: show up to four subranges.': 'ПКМ: показать до четырёх поддиапазонов.',
    'All shells in this range have the same density.': 'У всех шеллов в этом диапазоне одинаковый тексель.',
    'Source UV set': 'Исходный набор UV',
    'Target UV set': 'Целевой набор UV',
    'Use the selected mesh or group as Source': 'Использовать выбранный меш или группу как источник',
    'Use the selected mesh or group as Target': 'Использовать выбранный меш или группу как цель',
    'Choose set': 'Выбрать сет',
    'Choose map type': 'Выбрать тип карты',
    'Duplicate: keep one': 'Дубликат. Оставь одну карту.',
    'Assign': 'Назначить',
    'Replace': 'Заменить',
    'Skip': 'Пропустить',
    'Set the texel density of selected shells at the current Map Size.': 'Установить тексель выбранных шеллов для текущего Map Size.',
    'Clear texel-density colors from the viewport.': 'Убрать подсветку текселя в окне просмотра.',
    'Remove pins from the selection.': 'Убрать пины с выбранных элементов.',
}

_PATTERNS = tuple((re.compile(source), target) for source, target in (
    (r'Find folds within shells and overlapping shells with less than (\d+%) agreement of their combined UV area\.\nMatching stacks and touching borders are ignored\. Click again to stop\.',
     r'Найти складки внутри шеллов и наложения с совпадением площади менее \1.\nСовпадающие стаки и соприкасающиеся границы пропускаются. Нажми ещё раз, чтобы остановить проверку.'),
    (r'Relax selected UVs only along ([UV]) \((?:horizontal|vertical)\)\. The other coordinate stays fixed\. Shift \+ wheel pins process the entire affected shell; otherwise only selected UVs change\.',
     r'Уменьшить искажения вдоль оси \1. Другая координата сохраняется. С пинами, назначенными через Shift + колесо, обрабатывается весь шелл. В остальных случаях меняются выбранные UV.'),
    (r'Flip each selected shell along ([UV]) around its center\.', r'Отразить каждый выбранный шелл по оси \1 относительно его центра.'),
    (r'Flip selected UV shells (?:horizontally|vertically) along ([UV])\.', r'Отразить выбранные UV-шеллы по оси \1.'),
    (r'Assigned originals: (\d+) UV shells', r'Назначено оригиналов (UV-шеллов): \1'),
    (r'Target: (.+) px/unit\. Map Size: (\d+)\.', r'Цель: \1 пикс./ед. Размер карты: \2.'),
    (r'Current density: (.+) px/unit \(range (.+)\); (\d+) shells\.', r'Текущий тексель: \1 пикс./ед. (диапазон \2). Шеллов: \3.'),
    (r'Current density: (.+) px/unit; (\d+) shells\.', r'Текущий тексель: \1 пикс./ед. Шеллов: \2.'),
    (r'Current density is unavailable: (.+)', r'Текущий тексель недоступен: \1'),
    (r'Intensity: (.+)×\. Drag either marker; the other mirrors it\. Toward the ends is stronger\. Arrow keys fine-tune\.', r'Интенсивность: \1×. Маркеры движутся зеркально. Ближе к краям цвет сильнее. Стрелки позволяют точно настроить значение.'),
    (r'Contrast: (.+)×\. Drag either marker; the other mirrors it\. Farther from the center increases color differences\. Arrow keys fine-tune\. Green: (.+) px/unit at the start of analysis\. Blue/cyan: below reference\. Yellow/red: above\. Only colors change; density values and UVs are preserved\.', r'Контраст: \1×. Маркеры движутся зеркально. Чем дальше от центра, тем заметнее разница цветов. Зелёный: \2 пикс./ед. в начале анализа. Синий и голубой — ниже, жёлтый и красный — выше. Тексель и UV сохраняются.'),
    (r'Contrast: (.+)×\. Drag either marker; the other mirrors it\. Farther from the center increases color differences\. Arrow keys fine-tune\. Green: reference density\. Blue/cyan: below reference\. Yellow/red: above\. Only colors change; density values and UVs are preserved\.', r'Контраст: \1×. Маркеры движутся зеркально. Чем дальше от центра, тем заметнее разница цветов. Зелёный — эталон. Синий и голубой — ниже, жёлтый и красный — выше. Тексель и UV сохраняются.'),
    (r'(\d+) of (\d+) assigned maps visible', r'Видимые карты: \1 из \2'),
    (r'File channel: (.+)', r'Канал файла: \1'),
    (r'Composite network: (.+)', r'Составная сеть: \1'),
    (r'Disconnect (.+) from this slot\. One Undo restores the map\.', r'Отсоединить \1 от этого слота. Одно Undo восстанавливает карту.'),
    (r'Show (.+) on the material', r'Показать \1 на материале'),
    (r'Hide (.+) on the material', r'Скрыть \1 на материале'),
    (r'Show (.+) in all texture sets', r'Показать \1 во всех текстурных сетах'),
    (r'Hide (.+) in all texture sets', r'Скрыть \1 во всех текстурных сетах'),
    (r'Remove (.+) from the preserved UV sets', r'Убрать \1 из сохраняемых UV-наборов'),
    (r'Target UV set for cleanup\. (.+)', r'Целевой UV-набор для очистки. \1'),
    (r'Protected UV set\. Its UVs stay unchanged\. (.+)', r'Защищённый UV-набор. Его UV сохраняются. \1'),
))

for direction, translation in (('up', 'вверх'), ('down', 'вниз'), ('left', 'влево'), ('right', 'вправо')):
    TOOLTIPS[f'Move all selected UV shells {direction} by Step.'] = f'Сдвинуть выбранные UV-шеллы {translation} на значение Step.'
for direction, translation in (('Counterclockwise', 'против часовой стрелки'), ('Clockwise', 'по часовой стрелке')):
    TOOLTIPS[f'{direction} by the specified angle around the combined UV-vertex center of the selected shells.'] = (
        f'Повернуть {translation} на заданный угол вокруг центра всех UV-вершин выбранных шеллов.')


def language():
    global _locale
    if _locale is None:
        _locale = 'ru' if cmds.optionVar(exists=OPTION) and cmds.optionVar(query=OPTION) == 'ru' else 'en'
    return _locale


def _translate(source):
    if source in TOOLTIPS:
        return TOOLTIPS[source]
    for pattern, target in _PATTERNS:
        match = pattern.fullmatch(source)
        if match is not None:
            return match.expand(target)
    for separator in ('<br>', '\n', ' · '):
        if separator in source:
            return separator.join(_translate(part) for part in source.split(separator))
    return source


def _wrap_line(text, font):
    if QtGui.QFontMetricsF(font).horizontalAdvance(text) <= TOOLTIP_TEXT_WIDTH:
        return [text]
    layout = QtGui.QTextLayout(text, font)
    option = QtGui.QTextOption()
    option.setWrapMode(QtGui.QTextOption.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    # QTextLine offsets count UTF-16 code units, including in user file names.
    encoded = text.encode('utf-16-le')
    lines = []
    layout.beginLayout()
    while (line := layout.createLine()).isValid():
        line.setLineWidth(TOOLTIP_TEXT_WIDTH)
        start = line.textStart()
        end = start + line.textLength()
        lines.append(encoded[start * 2:end * 2].decode('utf-16-le').rstrip())
    layout.endLayout()
    return lines


def _render_tooltip(source, locale):
    image = ''
    if source.startswith('<div><img src="data:image/') and source.endswith('</div>'):
        # Leave the embedded thumbnail intact. Wrap only its description.
        image, _, description = source[:-6].partition('<br>')
        source = unescape(description.replace('<br>', '\n'))
    text = _translate(source) if locale == 'ru' else source
    paragraphs = text.split('\n')
    font = QtWidgets.QToolTip.font()
    lines = [line for paragraph in paragraphs for line in _wrap_line(paragraph, font)]
    if not image and lines == paragraphs:
        return text
    body = '<br>'.join(escape(line, quote=False) for line in lines)
    return image + '<br>' + body + '</div>' if image else '<qt>' + body + '</qt>'


def _refresh(control, locale):
    value = control.toolTip()
    if not value:
        return
    previous = getattr(control, '_ssTooltip', None)
    if previous is not None and value == previous[1]:
        source = previous[0]
        if previous[2] == locale:
            return
    else:
        source = value
    translated = _render_tooltip(source, locale)
    # Qt emits ToolTipChange synchronously when setting this property.
    control._ssTooltip = (source, translated, locale)
    if translated != value:
        control.setToolTip(translated)


def apply(root, locale=None):
    """Mark a ShellShifter window and refresh its existing hover text."""
    root.setProperty('ssTooltipScope', True)
    locale = language() if locale is None else locale
    for widget in (root, *root.findChildren(QtWidgets.QWidget)):
        _refresh(widget, locale)


def refresh_hover(widget):
    """Catch controls and tooltip text that are created after the window."""
    if not isinstance(widget, QtWidgets.QWidget) or not widget.toolTip():
        return
    parent = widget
    while parent is not None:
        if parent.property('ssTooltipScope'):
            _refresh(widget, language())
            return
        parent = parent.parentWidget()


def change(locale):
    global _locale
    if locale not in ('en', 'ru'):
        raise ValueError('Unknown ShellShifter tooltip language.')
    cmds.optionVar(stringValue=(OPTION, locale))
    _locale = locale
    for window in QtWidgets.QApplication.allWidgets():
        if not window.property('ssTooltipScope'):
            continue
        selector = window.findChild(QtWidgets.QComboBox, 'shellshifterLanguage')
        if selector is not None:
            with signals_blocked(selector):
                selector.setCurrentIndex(selector.findData(locale))
        apply(window, locale)
