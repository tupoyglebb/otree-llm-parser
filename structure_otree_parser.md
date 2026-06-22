# Структура и архитектура otree_parser.py

## Оглавление
1. [Общая структура файла](#общая-структура-файла)
2. [Структуры данных (Data Classes)](#структуры-данных-data-classes)
3. [Основные классы](#основные-классы)
4. [Публичные функции](#публичные-функции)
5. [Процесс формирования анкеты (детально)](#процесс-формирования-анкеты-детально)
6. [Взаимосвязи классов и функций](#взаимосвязи-классов-и-функций)
7. [Особенности реализации](#особенности-реализации)

---

## Общая структура файла

### Импорты и зависимости (строки 1-27)
- **Стандартные библиотеки**: `dataclasses`, `pathlib`, `zipfile`, `tempfile`, `ast`, `re`
- **Специальные библиотеки**: `importlib.util` (для динамического импорта), `BeautifulSoup` (опционально)
- **Флаг `HAS_BS4`**: определяет наличие BeautifulSoup для парсинга HTML

---

## Структуры данных (Data Classes)

### `FieldMetadata` (строки 33-40)
Метаданные поля модели Django/oTree:
- `name: str` - имя поля
- `field_type: str` - тип поля (IntegerField, StringField и т.д.)
- `verbose_name: Optional[str]` - текст вопроса/метка поля
- `choices: Optional[List[Tuple]]` - варианты ответов (список кортежей)
- `help_text: Optional[str]` - подсказка для поля

### `Question` (строки 43-51)
Представляет один вопрос на странице:
- `field_name: str` - имя поля модели
- `question_text: Optional[str]` - текст вопроса
- `field_type: Optional[str]` - тип поля
- `answer_options: List[str]` - список вариантов ответов
- `help_text: Optional[str]` - подсказка
- `is_required: bool` - обязательность вопроса

### `PageInfo` (строки 54-64)
Информация о странице в oTree приложении:
- `class_name: str` - имя класса страницы
- `app_name: str` - имя приложения
- `form_model: Optional[str]` - модель формы ('player', 'group', 'subsession')
- `form_fields: List[str]` - список полей формы
- `template_name: Optional[str]` - имя HTML шаблона
- `template_text: Optional[str]` - извлеченный текст из шаблона
- `is_displayed_conditional: bool` - есть ли условие отображения (метод is_displayed)
- `questions: List[Question]` - список вопросов на странице

### `Treatment` (строки 67-74)
Представляет одно условие/лечение эксперимента:
- `name: str` - имя treatment
- `display_name: str` - отображаемое имя
- `app_sequence: List[str]` - последовательность приложений
- `visible_pages: List[PageInfo]` - список видимых страниц
- `config: Dict[str, Any]` - конфигурация treatment

### `Experiment` (строки 77-82)
Полная структура эксперимента:
- `project_name: str` - имя проекта
- `treatments: List[Treatment]` - список условий эксперимента
- `app_models: Dict[str, Dict[str, List[FieldMetadata]]]` - модели всех приложений
  - Структура: `app_name -> {Player/Group/Subsession -> [FieldMetadata]}`

---

## Основные классы

### `MockObjectFactory` (строки 89-161)

**Назначение**: Создает моки объектов для тестирования методов `is_displayed()` страниц.

**Методы**:
- `create_mock_player()` - создает мок объект Player
- `create_mock_group()` - создает мок объект Group
- `create_mock_subsession()` - создает мок объект Subsession
- `create_mock_page_instance()` - создает полный мок экземпляр страницы со всеми зависимостями

**Использование**: Используется классом `QuestionnaireBuilder` для проверки видимости страниц.

---

### `OTreeInspector` (строки 168-1127)

**Назначение**: Главный класс для инспекции структуры oTree проекта.

#### Методы извлечения проекта

##### `extract_otreezip(path: str) -> Path` (строки 177-264)
- Распаковывает `.otreezip` файл (поддерживает ZIP и TAR.GZ форматы)
- Определяет формат по magic bytes
- Рекурсивно ищет директорию с `settings.py`
- Возвращает путь к корню проекта

##### `import_project(project_path: Path)` (строки 266-282)
- Динамически импортирует `settings.py`
- Добавляет путь проекта в `sys.path`
- Сохраняет `settings_module` для последующего использования

#### Методы инспекции моделей

##### `inspect_models(app_name: str) -> Dict[str, List[FieldMetadata]]` (строки 284-373)

**Универсальный алгоритм** проверяет все возможные места:
1. `models.py` (стандартное место)
2. `__init__.py` (может содержать модели)
3. Другие `.py` файлы в директории приложения

**Процесс**:
1. Пробует загрузить модуль динамически (если зависимости доступны)
2. Если не удалось → парсит исходный код через AST
3. Ищет классы `Player`, `Group`, `Subsession`
4. Извлекает поля через `_extract_fields_from_model()` или `_extract_fields_from_source_code()`

##### `_extract_fields_from_model(model_class) -> List[FieldMetadata]` (строки 375-467)

Извлекает поля из загруженного класса модели:
- **Метод 1**: Использует Django ORM introspection (`_meta.get_fields()`)
- **Метод 2**: Прямой доступ к атрибутам класса (fallback)

##### `_extract_fields_from_source_code(file_path: Path) -> Dict[str, List[FieldMetadata]]` (строки 469-544)

**AST парсинг моделей** (работает даже если otree не установлен):
- Парсит Python код через `ast.parse()`
- Ищет классы `Player`, `Group`, `Subsession`
- Находит определения полей (например, `age = models.IntegerField(label='...')`)
- Извлекает: имя поля, тип, `label`, `choices`, `help_text`

##### `_extract_choices_from_ast(node) -> Optional[List]` (строки 546-565)
Извлекает список choices из AST узла (обрабатывает кортежи `(value, label)`).

##### `_extract_value_from_ast(node) -> Any` (строки 567-579)
Извлекает значение из AST узла (строки, числа, константы).

#### Методы инспекции страниц

##### `inspect_pages(app_name: str, app_models: ...) -> List[PageInfo]` (строки 673-810)

**Универсальный алгоритм** проверяет все возможные места:
1. `pages.py` (стандартное место)
2. `__init__.py` (может содержать страницы)
3. Другие `.py` файлы в директории приложения
4. HTML шаблоны (fallback)

**Процесс**:
1. Пробует импортировать `otree.api.Page` (если доступен)
2. Для каждого `.py` файла:
   - **Метод 1**: Динамический импорт модуля → поиск классов, наследующихся от `Page`
   - **Метод 2**: AST парсинг → `_extract_pages_from_source_code()`
3. Если страницы не найдены → `_inspect_pages_from_templates()` (fallback)

##### `_extract_pages_from_source_code(file_path, app_name, app_models) -> List[PageInfo]` (строки 581-671)

**AST парсинг страниц**:
- Парсит код через AST
- Ищет классы с базовым классом `Page`
- Извлекает `form_model`, `form_fields` из атрибутов класса
- Создает `PageInfo` с вопросами через `_extract_questions_from_fields()`

##### `_inspect_pages_from_templates(app_name, app_models) -> List[PageInfo]` (строки 812-875)

**Fallback метод**: извлекает страницы из HTML шаблонов:
- Ищет `.html` файлы в директории приложения
- Извлекает текст через BeautifulSoup или regex
- Создает `PageInfo` без `form_fields` (только текст шаблона)

##### `_extract_questions_from_html(html_content, app_name, app_models) -> List[Question]` (строки 877-977)

Пытается извлечь вопросы из HTML контента:
- Использует `app_models`, если доступны
- Ищет вопросоподобный текст в HTML (элементы с `?` или вопросительными словами)
- Пытается найти связанные элементы формы (radio, checkbox, select)

##### `_extract_page_info(page_class, app_name, app_models) -> PageInfo` (строки 979-1027)

Извлекает информацию из загруженного класса Page:
- Получает `form_model`, `form_fields`, `template_name`
- Проверяет наличие метода `is_displayed()`
- Извлекает текст шаблона через `extract_template_text()`
- Создает вопросы через `_extract_questions_from_fields()`

##### `_extract_questions_from_fields(form_fields, form_model, app_name, app_models) -> List[Question]` (строки 1029-1078)

Создает объекты `Question` из полей формы:
- Сопоставляет `form_fields` с метаданными из `app_models`
- Преобразует `form_model` в имя класса модели (`'player'` → `'Player'`)
- Извлекает варианты ответов из `choices` полей
- Создает `Question` объекты с полной информацией

##### `extract_template_text(app_name, template_name) -> Optional[str]` (строки 1080-1127)

Извлекает текст из HTML шаблона:
- Ищет шаблон по нескольким возможным путям
- Использует BeautifulSoup (если доступен) или regex для очистки HTML
- Удаляет теги `<script>`, `<style>`, HTML разметку
- Возвращает чистый текст

---

### `QuestionnaireBuilder` (строки 1134-1249)

**Назначение**: Строит структуру анкеты, определяя видимые страницы.

#### `determine_visible_pages(treatment, inspector, app_models) -> List[PageInfo]` (строки 1141-1219)

Определяет, какие страницы видимы для данного treatment:
1. Собирает все страницы из всех приложений в `app_sequence`
2. Пытается получить `page_sequence` из `pages.py` каждого приложения
3. Для каждой страницы:
   - Если есть `page_sequence` → использует его порядок
   - Иначе → проверяет все страницы приложения
   - Вызывает `_is_page_visible()` для проверки видимости

#### `_is_page_visible(page_info, page_class, app_name, session_config) -> bool` (строки 1221-1243)

Проверяет видимость страницы:
- Если нет метода `is_displayed()` → страница всегда видима
- Иначе → создает мок экземпляр страницы и вызывает `is_displayed()`
- Возвращает результат или `True` (fallback)

#### `build_questionnaire(treatment, app_models) -> Treatment` (строки 1245-1249)

Обогащает `Treatment` видимыми страницами:
- Вызывает `determine_visible_pages()`
- Присваивает результат `treatment.visible_pages`

---

## Публичные функции

### `parse_otreezip(otreezip_path: str) -> Experiment` (строки 1256-1327)

**Главная функция парсинга**:

1. Создает экземпляры классов:
   - `OTreeInspector(Path())`
   - `MockObjectFactory(Path())`
   - `QuestionnaireBuilder(inspector, mock_factory)`

2. Распаковывает `.otreezip`:
   - `inspector.extract_otreezip(otreezip_path)` → `project_path`

3. Импортирует проект:
   - `inspector.import_project(project_path)`

4. Извлекает конфигурации:
   - Получает `SESSION_CONFIGS` из `settings.py`
   - Получает `PROJECT_NAME`

5. Для каждого treatment:
   - Инспектирует модели всех приложений → `app_models`
   - Создает `Treatment` объект
   - Строит анкету через `builder.build_questionnaire()`

6. Создает `Experiment` и сохраняет `inspector` и `project_path` для последующего использования

### `extract_questionnaire_texts(experiment, treatment_name=None) -> List[str]` (строки 1334-1360)

Возвращает простой список текстов всех вопросов анкеты.

### `format_questionnaire_for_llm(experiment, treatment_name=None) -> str` (строки 1363-1419)

Форматирует эксперимент в текстовый формат для отправки в LLM (Markdown формат).

### `get_all_questions_with_options(experiment, treatment_name=None) -> List[Dict]` (строки 1422-1467)

Возвращает структурированный список вопросов с вариантами ответов в виде словарей.

### `build_questionnaire_for_api(experiment, treatment_name=None) -> List[Dict]` (строки 1470-1672)

**Создает полную структурированную анкету для последовательной отправки в LLM API**:

Для каждой видимой страницы:
1. **Извлечение текста страницы**:
   - Пытается использовать `page.template_text`
   - Если нет → `inspector.extract_template_text()`
   - Если нет → читает HTML напрямую из файла

2. **Извлечение инструкций**:
   - Парсит HTML через BeautifulSoup
   - Убирает Jinja2 синтаксис (`{{ }}`, `{% %}`)
   - Берет текст до `{{ formfields }}` как инструкции

3. **Для каждого вопроса на странице**:
   - Формирует `full_context`:
     - `Инструкция: [текст страницы]`
     - `Вопрос: [текст вопроса]`
     - `Варианты ответов: [список вариантов]`
     - `Подсказка: [если есть]`
   - Создает элемент анкеты со всеми метаданными

**Возвращает**: Список словарей, где каждый элемент - один вопрос для отправки в API.

---

## Процесс формирования анкеты (детально)

### Этап 1: Распаковка и инициализация