# Facebook Reels CLI

`facebook-publish` публикует готовое локальное видео от явно привязанной Facebook Page через постоянный профиль Camoufox. Вызывающий сервис определяет видео, подпись, аккаунт, ID запроса и время запуска. Версия берется из `pyproject.toml`, зависимости закреплены в `uv.lock`.

Реальная публикация еще не принята. На 2026-10-06 Page `Caribbean Awesome` / `caribbeanawesome` отсутствует в проверенном переключателе аккаунтов. Шаблон `ui-contract.example.json` содержит пустые, непроверенные селекторы и возвращает `UI_NOT_VERIFIED`. Для работы нужны существующая Page, проверенные селекторы текущего интерфейса и видимая приемка редактора. Личный профиль не принимается как запасной получатель.

## Установка на macOS / Linux

Требуются Python 3.12/3.13, [uv](https://docs.astral.sh/uv/), `ffmpeg` с `ffprobe` и доступ по SSH к закрепленному приватному `metadata-automation`.

```sh
uv sync --locked
uv run --locked python -m camoufox fetch
uv run --locked facebook-publish --version
```

На macOS установить `ffmpeg` через `brew install ffmpeg`; на Debian/Ubuntu — через `sudo apt-get install ffmpeg xvfb`. Linux `--headless` использует `headless="virtual"` и требует Xvfb, macOS — обычный headless. Первичный вход, 2FA и приемка всегда видимы.

Закрепленная пара — `camoufox==0.5.7`, `playwright==1.60.0`; пакет Camoufox требует браузер `official/156.0.1-beta.34`. На macOS arm64 проверен полный сценарий на перехваченной тестовой странице: приемка без Post, подтвержденный тестовый URL, сохраненный успех и неопределенный результат без повторного Post. Linux/Xvfb и живой Meta UI пока не проверены. Код выбирает именно парный браузер и не загружает/не обновляет его при публикации; отсутствие браузера дает `BROWSER_MISSING`. Для изолированной проверки можно задать `CAMOUFOX_EXECUTABLE_PATH` к бинарнику этого же выпуска с соответствующим `version.json`. Camoufox документирует [persistent context](https://camoufox.com/python/usage/) с `user_data_dir` и [виртуальный дисплей](https://camoufox.com/python/virtual-display/); действия используют [проверки доступности Playwright](https://playwright.dev/python/docs/actionability).

## Однократная привязка Page и приемка UI

Скопировать `ui-contract.example.json` в закрытый каталог и заполнить CSS-селекторы после проверки интерфейса нужной Page. Это операторский файл: селекторы не берутся из метаданных. `target.page_url` и `target.display_name` должны точно совпадать с Page. URL редактора в примере также требует живой проверки.

```sh
mkdir -p .local
chmod 700 .local
cp ui-contract.example.json .local/facebook-ui.json
chmod 600 .local/facebook-ui.json

uv run --locked facebook-publish login \
  --account caribbeanawesome \
  --page-url https://www.facebook.com/caribbeanawesome \
  --display-name "Caribbean Awesome" \
  --ui-contract .local/facebook-ui.json

uv run --locked facebook-publish accounts list
```

Пройти вход и 2FA, переключиться на указанную Page в Camoufox и нажать Enter в терминале. Привязка сохраняется после проверки текущего актера по каноническому URL, точному имени и элементу управления Page. Нельзя привязать псевдоним к другой Page или создать второй псевдоним для той же Page. Cookie Firefox не импортируются: один ручной вход создает собственную постоянную сессию.

`current_actor_link` должен указывать именно на текущего актера, а не пункт переключения на другую Page или общедоступную ссылку. `page_management_marker` подтверждает управление Page; `composer_actor_link` — автора в редакторе. `actor_menu_button` может быть пустым, если актер виден без меню. Остальные селекторы обязательны и должны разрешаться в один элемент. Загрузка, подпись, выбранная аудитория и Post ищутся внутри `editor`; `public_option` может находиться во всплывающем меню снаружи. Остальные селекторы относятся ко всей странице. `steps_before_caption` и `steps_after_caption` задают необходимые кнопки переходов внутри редактора.

После записи селекторов выполнить видимую проверку согласованного файла и подписи:

```sh
uv run --locked facebook-publish accept-ui \
  --account caribbeanawesome --video /absolute/path/reel.mp4 \
  --privacy public --metadata-file /absolute/path/metadata.json \
  --ui-contract .local/facebook-ui.json
```

`accept-ui` загружает закрытую копию, проверяет автора, принятие видео, точную подпись, Public и доступность Post через `click(trial=True)`. Финального клика нет. Только успешная проверка со снимком сохраняет контракт для следующих вызовов. Публикация затем использует принятую копию из SQLite; изменение операторского файла требует нового `accept-ui`.

Перед кликами по меню актера, кнопкам переходов и выбору Public проверяется идентичность DOM-элемента с финальным Post, включая его дочерние элементы и область клика. Альтернативный CSS для того же Post отклоняется. Финальный Post должен однозначно присутствовать в DOM перед любым таким кликом; отсутствие или неоднозначность останавливает подготовку. Если на странице проверки личности Post отсутствует, текущий актер должен проверяться без автоматического клика по меню.

Приемка подтверждает подготовку редактора. Селекторы `success`, `result_link`, `post_actor_link`, `post_caption`, `post_public` нужно отдельно проверить на контролируемой странице и реальном согласованном Reel. Неподтвержденная отправка остается `uncertain` и не допускает повторного Post. Изменение Meta UI приводит к ошибке и локальному снимку, если браузер доступен.

## Вызов из сервиса

```sh
uv run --locked facebook-publish publish-reel \
  --account caribbeanawesome --request-id video-2026-10-06-001 \
  --video /absolute/path/reel.mp4 --privacy public \
  --metadata-file /absolute/path/metadata.json --headless --timeout-seconds 1800

uv run --locked facebook-publish status \
  --account caribbeanawesome --request-id video-2026-10-06-001
```

Альтернатива `--metadata-file` — `--caption-file` с точным UTF-8 текстом. Источники взаимно исключаются; без обоих подпись пустая. Полный конверт проверяется `metadata-automation` из Git SHA `a862f19a08f84d051f8359a0a760ab394cb47d33`: версия схемы `1.0.0`, `source`, все девять платформ и отсутствие неизвестных полей. Передается дословно `platforms.facebook.caption`, без повторного добавления хештегов. Метаданные не выбирают Page, видео, ID или аудиторию.

В другом процессе использовать абсолютные пути:

```python
import json
import subprocess

process = subprocess.run(
    ["uv", "run", "--locked", "--project", "/srv/facebook-automation",
     "facebook-publish", "publish-reel", "--account", "caribbeanawesome",
     "--request-id", "video-2026-10-06-001", "--video", "/srv/media/reel.mp4",
     "--privacy", "public", "--metadata-file", "/srv/media/metadata.json", "--headless"],
    capture_output=True, text=True,
)
result = json.loads(process.stdout)
```

По умолчанию состояние лежит в `.local/` проекта. При установке wheel или запуске из сервиса задать один постоянный абсолютный `FACEBOOK_PUBLISH_STATE_DIR` либо `--state-dir` во всех командах. Для одной Page нужен один авторитетный каталог состояния: смена каталога создает независимый журнал и требует новой привязки/приемки. Серверное развертывание локального CLI не определено.

## Результат и повторы

Каждая команда, включая ошибки разбора, `--help` и `--version`, возвращает один JSON-объект одной строкой в stdout; диагностика идет в stderr. Пример:

```json
{"platform":"facebook","account":"caribbeanawesome","request_id":"video-2026-10-06-001","status":"published","post_url":"https://www.facebook.com/reel/123456789","error":null}
```

| Код | Значение публикации |
| --- | --- |
| 0 | Подтвержден конкретный Reel; повтор возвращает сохраненный результат. |
| 2 | Неверные аргументы/медиа, неизвестная или другая Page, отсутствие входа. Post не нажат. |
| 3 | Занятый профиль, непроверенный/измененный UI, технический сбой до отправки. Post не нажат. |
| 4 | Граница отправки пройдена, итог неизвестен (`uncertain`). Повторного клика нет. |

`status` читает журнал без браузера: код 0 для найденной записи любого статуса, 2 для отсутствующей/некорректной пары. Успешные `login`, `accounts list` и `accept-ui` дают 0; ошибки входа/аргументов — 2, технические сбои — 3. `accounts list` содержит псевдоним, URL и имя без сессии. `login` возвращает `authenticated`, `accept-ui` — `ui_accepted` и путь снимка.

Блокировка профиля удерживается на весь вход, приемку, публикацию и восстановление; конкурентный процесс сразу получает 3. Перед Post фиксируется `submitting` с `synchronous=FULL`, затем выполняется один финальный клик. Тайм-аут/обрыв после границы означают `uncertain`; оставшийся после краха `submitting` также читается как `uncertain`. Закрытие редактора не означает успех: нужен новый `/reel/ID`, связанный с результатом, правильным автором, точной подписью и Public.

Ключ `(account, request_id)` уникален. Другие параметры или байты видео/подписи при повторе неуспешной подготовки дают `REQUEST_CONFLICT`. Терминальные `published`/`uncertain` сравнивают исходные аргументы без повторного чтения/загрузки видео; `status` не требует исходных файлов. Исправленный сбой до отправки можно повторить с теми же аргументами и отпечатком. Измененный материал требует нового ID.

При `uncertain` вручную найти именно этот Reel и проверить Page, видео, подпись и Public. После проверки явно подтвердить конкретный URL:

```sh
uv run --locked facebook-publish reconcile \
  --account caribbeanawesome --request-id video-2026-10-06-001 \
  --post-url https://www.facebook.com/reel/123456789 --confirm-matching-reel
```

`reconcile` — явное ручное подтверждение указанного URL: переводит только `uncertain` в `published`, не открывает браузер и не нажимает Post. Не создавать новый ID для автоматического повтора неопределенной отправки.

## Данные и проверка

Профили, стабильный JSON-отпечаток устройства, журнал и снимки хранятся вне Git с правами 700/600. Загружается отдельная проверенная копия MP4/MOV с H.264/H.265; SHA-256 копии включается в отпечаток запроса. Копия удаляется после завершения. Cookie, storage state и полный DOM не выводятся.

Снимки могут содержать личные данные; оператор удаляет старые `accounts/*/diagnostics/` и `requests/*/*/diagnostics/` согласно своей политике хранения. Для резервного копирования остановить CLI-процессы и сохранить весь закрытый каталог состояния: журнал и профили нужны вместе.

```sh
uv run --locked --group dev pytest -q
```

Дополнительные DOM-тесты используют только перехваченные локальные страницы. Для их включения задать `FACEBOOK_TEST_CAMOUFOX_EXECUTABLE_PATH` к проверенному парному браузеру и выполнить ту же команду тестов.

Контрактные тесты проверяют JSON/коды, метаданные, копию видео, блокировку процессов, привязку Page, отпечатки, `submitting`, отмену/крах, отсутствие второго Post и ручную сверку. Контролируемые адаптеры не публикуют в Meta. Живые селекторы, существующая Page, фактический браузер на macOS/Linux и первый согласованный публичный Reel требуют отдельной приемки.
