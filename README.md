# Merck Veterinary Manual parser

Готовый HTTP-парсер для раздела **Sections** на сайте Merck Veterinary Manual.

Стартовая страница:
`https://www.merckvetmanual.com/veterinary-topics`

## Что делает скрипт

Скрипт:

1. открывает страницу `Veterinary Topics`;
2. берёт только блок `Sections`;
3. собирает верхнеуровневые разделы, например `Behavior`;
4. проходит по вложенным страницам и подпунктам;
5. создаёт папки по иерархии сайта;
6. сохраняет для каждой страницы отдельный текстовый файл `_index.txt`;
7. внутрь файла пишет **основной текст статьи вместе с заголовками**.

## Как выглядит результат

Пример структуры выгрузки:

```text
merck_vet_manual/
  Behavior/
    Behavioral Medicine Introduction/
      _index.txt
      Overview of Behavioral Medicine in Animals/
        _index.txt
      Integrating Behavior Services Into Veterinary Practice/
        _index.txt
    Behavior of Cats/
      _index.txt
```

## Структура репозитория

```text
.
├─ parser.py
├─ requirements.txt
├─ run.bat
├─ .gitignore
└─ README.md
```

## Установка

### 1. Установить Python
Нужен Python 3.11+.

### 2. Создать виртуальное окружение

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Установить зависимости

```bash
pip install -r requirements.txt
```

## Запуск

Обычный запуск:

```bash
python parser.py
```

Запуск с параметрами:

```bash
python parser.py --output merck_vet_manual --delay 0.5 --max-pages 5000 --log-level INFO
```

На Windows можно просто запустить:

```bash
run.bat
```

## Параметры

- `--start-url` — стартовая страница, по умолчанию `https://www.merckvetmanual.com/veterinary-topics`
- `--output` — папка выгрузки
- `--delay` — пауза между запросами
- `--max-pages` — лимит страниц для защиты от зацикливания
- `--log-level` — уровень логов

## Важные замечания

Парсер сделан **без браузера**, только через обычные HTTP-запросы.

Так как сайт может менять HTML-разметку, селекторы и эвристики могут потребовать лёгкой корректировки в будущем.

В этой среде у меня нет доступа к внешней сети, поэтому код подготовлен аккуратно, но фактический прогон против сайта нужно сделать уже у вас локально или на сервере.

## Что проверить после первого запуска

1. Создалась ли папка `merck_vet_manual`.
2. Появились ли папки верхнего уровня, например `Behavior`.
3. Есть ли внутри `_index.txt`.
4. Не попали ли в выгрузку лишние служебные разделы.
5. Корректно ли идёт вложенность подпунктов.

Если структура сайта отличается от ожиданий, первым делом правятся функции:

- `extract_top_sections()`
- `extract_child_links()`
- `extract_article_text()`

## Логика сохранения

Для каждой страницы создаётся отдельная папка по её месту в дереве сайта.

Внутри этой папки создаётся файл:

```text
_index.txt
```

Это сделано специально, чтобы можно было одновременно:

- сохранить иерархию сайта папками;
- хранить текст страницы отдельно;
- без конфликтов создавать вложенные подпункты.
