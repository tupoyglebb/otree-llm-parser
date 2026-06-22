"""
Parser for oTree experiments from .otreezip files.

This module provides functionality to extract and analyze oTree experiment
structures, including models, pages, and session configurations.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import zipfile
import tempfile
import sys
import importlib.util
import warnings
import re
from unittest.mock import Mock
import subprocess
import tarfile
import ast

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    warnings.warn("beautifulsoup4 not installed, template parsing will use regex fallback")

# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class FieldMetadata:
    """Metadata for a model field."""
    name: str
    field_type: str  # IntegerField, CharField, etc.
    verbose_name: Optional[str] = None
    choices: Optional[List[Tuple]] = None
    help_text: Optional[str] = None


@dataclass
class Question:
    """A question/field on a page."""
    field_name: str
    question_text: Optional[str] = None  # Label/verbose_name of the field
    field_type: Optional[str] = None  # IntegerField, CharField, etc.
    answer_options: List[str] = field(default_factory=list)  # Choices/options for the answer
    help_text: Optional[str] = None
    is_required: bool = True


@dataclass
class PageInfo:
    """Information about a page in an oTree app."""
    class_name: str
    app_name: str
    form_model: Optional[str] = None  # 'player', 'group', 'subsession'
    form_fields: List[str] = field(default_factory=list)
    template_name: Optional[str] = None
    template_text: Optional[str] = None  # plain-text from HTML
    is_displayed_conditional: bool = False  # has is_displayed() method
    questions: List[Question] = field(default_factory=list)  # Questions with answer options


@dataclass
class Treatment:
    """A treatment/condition in the experiment."""
    name: str
    display_name: str
    app_sequence: List[str]
    visible_pages: List[PageInfo] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Experiment:
    """Complete experiment structure."""
    project_name: str
    treatments: List[Treatment]
    app_models: Dict[str, Dict[str, List[FieldMetadata]]] = field(default_factory=dict)  # app_name -> {Player/Group/Subsession -> fields}


# ============================================================================
# MockObjectFactory
# ============================================================================

class MockObjectFactory:
    """Factory for creating mock objects to test is_displayed() methods."""
    
    def __init__(self, project_path: Path):
        self.project_path = project_path
    
    def create_mock_player(self, app_name: str, round_number: int = 1) -> Mock:
        """Create a minimal mock Player object."""
        mock_player = Mock()
        mock_player.round_number = round_number
        
        # Create mock participant with vars dict
        mock_participant = Mock()
        mock_participant.vars = {}
        mock_participant.id_in_session = 1
        # Support common participant attributes
        mock_participant.task_order = '[]'  # Default empty JSON array
        mock_participant.manipulation_type = None
        
        mock_player.participant = mock_participant
        
        # Add common fields as None/empty
        mock_player.field_maybe_none = Mock(return_value=None)
        # Support common player fields
        mock_player.answer = None
        mock_player.confidence = None
        mock_player.correct = None
        mock_player.rt = None
        
        return mock_player
    
    def create_mock_group(self, app_name: str) -> Mock:
        """Create a minimal mock Group object."""
        mock_group = Mock()
        return mock_group
    
    def create_mock_subsession(self, app_name: str) -> Mock:
        """Create a minimal mock Subsession object."""
        mock_subsession = Mock()
        mock_subsession.round_number = 1
        return mock_subsession
    
    def create_mock_page_instance(self, page_class, app_name: str, round_number: int, session_config: Dict[str, Any]) -> Any:
        """Create an instance of Page with mock objects."""
        try:
            # Create mock objects
            mock_player = self.create_mock_player(app_name, round_number)
            mock_group = self.create_mock_group(app_name)
            mock_subsession = self.create_mock_subsession(app_name)
            
            # Try to create page instance
            # Some Page classes might have __init__ that requires arguments
            try:
                page_instance = page_class()
            except TypeError:
                # If __init__ requires arguments, try with no args using __new__
                page_instance = page_class.__new__(page_class)
            
            # Set attributes
            page_instance.player = mock_player
            page_instance.group = mock_group
            page_instance.subsession = mock_subsession
            page_instance.round_number = round_number
            
            # Create mock session
            mock_session = Mock()
            mock_session.config = session_config
            page_instance.session = mock_session
            
            return page_instance
        except Exception as e:
            warnings.warn(f"Failed to create mock page instance for {page_class.__name__}: {e}")
            return None


# ============================================================================
# OTreeInspector
# ============================================================================

class OTreeInspector:
    """Inspector for oTree/Django project structure."""
    
    def __init__(self, project_path: Path):
        self.project_path = project_path
        self.project_name = project_path.name
        self.settings_module = None
        self._imported_modules = {}
    
    def extract_otreezip(self, path: str) -> Path:
        """Extract .otreezip file to temporary directory.
        
        Supports both ZIP and TAR.GZ formats (otreezip can be either).
        """
        import tarfile
        
        otreezip_path = Path(path)
        if not otreezip_path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        
        if not otreezip_path.suffix == '.otreezip':
            raise ValueError(f"Expected .otreezip file, got: {path}")
        
        # Create temporary directory
        temp_dir = tempfile.mkdtemp(prefix='otree_parser_')
        temp_path = Path(temp_dir)
        
        # Check file format by reading magic bytes
        with open(otreezip_path, 'rb') as f:
            magic_bytes = f.read(4)
        
        # Try ZIP format first (starts with PK)
        if magic_bytes[:2] == b'PK':
            try:
                with zipfile.ZipFile(otreezip_path, 'r') as zip_ref:
                    zip_ref.testzip()  # Test if zip is valid
                    zip_ref.extractall(temp_path)
            except zipfile.BadZipFile as e:
                raise ValueError(f"File is not a valid ZIP archive: {e}")
            except Exception as e:
                raise RuntimeError(f"Failed to extract ZIP archive: {e}")
        
        # Try TAR.GZ format (gzip files start with 1f 8b)
        elif magic_bytes[:2] == b'\x1f\x8b':
            try:
                with tarfile.open(otreezip_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(temp_path)
            except tarfile.TarError as e:
                raise ValueError(f"File appears to be gzipped but extraction failed: {e}")
            except Exception as e:
                raise RuntimeError(f"Failed to extract TAR.GZ archive: {e}")
        
        # Try plain TAR format
        elif len(magic_bytes) >= 257 and magic_bytes[257:257+5] == b'ustar':
            try:
                with tarfile.open(otreezip_path, 'r') as tar_ref:
                    tar_ref.extractall(temp_path)
            except Exception as e:
                raise ValueError(f"File appears to be a TAR archive but extraction failed: {e}")
        
        else:
            # Unknown format
            raise ValueError(
                f"Unknown archive format. File does not appear to be ZIP, TAR, or TAR.GZ. "
                f"Magic bytes: {magic_bytes.hex()}. "
                f"Please ensure the file was created using 'otree zip' command."
            )
        
        # Find the project directory by looking for settings.py
        def find_project_root(start_path: Path) -> Optional[Path]:
            """Recursively find directory containing settings.py."""
            # Check if settings.py exists in current directory
            if (start_path / 'settings.py').exists():
                return start_path
            
            # Search in subdirectories
            for item in start_path.iterdir():
                if item.is_dir():
                    result = find_project_root(item)
                    if result is not None:
                        return result
            
            return None
        
        # First try to find project root by searching for settings.py
        project_dir = find_project_root(temp_path)
        
        if project_dir is not None:
            return project_dir
    
        # Fallback: use first directory if settings.py not found (for backwards compatibility)
        extracted_dirs = [d for d in temp_path.iterdir() if d.is_dir()]
        if not extracted_dirs:
            raise ValueError("No directories found in .otreezip file and settings.py not found")
        
        project_dir = extracted_dirs[0]
        return project_dir
    
    def import_project(self, project_path: Path):
        """Dynamically import project settings and apps."""
        # Add project path to sys.path
        project_parent = project_path.parent
        if str(project_parent) not in sys.path:
            sys.path.insert(0, str(project_parent))
        
        # Import settings
        settings_path = project_path / 'settings.py'
        if not settings_path.exists():
            raise FileNotFoundError(f"settings.py not found in {project_path}")
        
        spec = importlib.util.spec_from_file_location("settings", settings_path)
        settings_module = importlib.util.module_from_spec(spec)
        sys.modules['settings'] = settings_module
        spec.loader.exec_module(settings_module)
        self.settings_module = settings_module
    
    def inspect_models(self, app_name: str) -> Dict[str, List[FieldMetadata]]:
        """Inspect Player/Group/Subsession models for an app.
        
        Универсальный алгоритм проверяет все возможные места:
        1. models.py (стандартное место)
        2. __init__.py (может содержать модели)
        3. Другие .py файлы в директории приложения
        """
        app_path = self.project_path / app_name
        if not app_path.exists():
                return {}
            
        # Add project parent to path if needed
        if str(self.project_path.parent) not in sys.path:
            sys.path.insert(0, str(self.project_path.parent))
        
        result = {}  # Инициализируем result в начале функции
            
        # Список файлов для проверки (в порядке приоритета)
        files_to_check = [
            app_path / 'models.py',  # Стандартное место
            app_path / '__init__.py',  # Может содержать модели
        ]
        
        # Также проверяем другие .py файлы в директории
        for py_file in app_path.glob('*.py'):
            if py_file not in files_to_check:
                files_to_check.append(py_file)
        
        # Пробуем загрузить модели из каждого файла
        for file_path in files_to_check:
            if not file_path.exists():
                continue
            
            try:
                # Определяем имя модуля
                if file_path.name == '__init__.py':
                    module_name = app_name
                elif file_path.name == 'models.py':
                    module_name = f"{app_name}.models"
                else:
                    module_name = f"{app_name}.{file_path.stem}"
                
                # Метод 1: Пробуем загрузить модуль (если зависимости доступны)
                module_loaded = False
                try:
                    spec = importlib.util.spec_from_file_location(module_name, file_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    
                    # Сохраняем модуль для последующего использования
                    self._imported_modules[module_name] = module
                    module_loaded = True
                    
                    for model_name in ['Player', 'Group', 'Subsession']:
                        if hasattr(module, model_name):
                            model_class = getattr(module, model_name)
                            fields = self._extract_fields_from_model(model_class)
                            if fields:  # Добавляем только если нашли поля
                                result[model_name] = fields
                    
                    # Если нашли хотя бы одну модель, можно прекратить поиск
                    if result:
                        break
                except (ImportError, ModuleNotFoundError):
                    # Метод 2: Парсим исходный код через AST (если модуль не загружается)
                    module_loaded = False
                except Exception:
                    # Любая другая ошибка - пробуем AST
                    module_loaded = False
                
                # Метод 2: Парсим исходный код через AST (всегда пробуем, если модуль не загрузился или не нашли модели)
                if not module_loaded or not result:
                    try:
                        fields_from_ast = self._extract_fields_from_source_code(file_path)
                        for model_name, fields in fields_from_ast.items():
                            if fields:
                                result[model_name] = fields
                        
                        if result:
                            break
                    except Exception:
                        pass
                    
            except Exception:
                # Продолжаем проверку других файлов
                continue
        
        return result
    
    def _extract_fields_from_model(self, model_class) -> List[FieldMetadata]:
        """Extract field metadata from a Django/oTree model.
        
        Универсальный метод извлекает поля из модели, используя:
        1. Django ORM introspection (_meta.get_fields())
        2. Прямой доступ к атрибутам класса (для случаев, когда ORM не работает)
        """
        fields = []
        
        try:
            # Метод 1: Используем Django ORM introspection
            if hasattr(model_class, '_meta'):
                for field in model_class._meta.get_fields():
                    # Skip reverse relations and auto fields like id
                    if hasattr(field, 'name') and not field.auto_created:
                        field_type = field.__class__.__name__
                        
                        # Извлекаем verbose_name/label
                        verbose_name = getattr(field, 'verbose_name', None)
                        if not verbose_name:
                            # Пробуем получить label (для oTree полей)
                            verbose_name = getattr(field, 'label', None)
                        if not verbose_name:
                            verbose_name = field.name
                        
                        # Извлекаем choices
                        choices = getattr(field, 'choices', None)
                        # Если choices - это callable, вызываем его
                        if callable(choices):
                            try:
                                choices = choices()
                            except:
                                choices = None
                        
                        # Извлекаем help_text
                        help_text = getattr(field, 'help_text', None)
                        
                        fields.append(FieldMetadata(
                            name=field.name,
                            field_type=field_type,
                            verbose_name=verbose_name,
                            choices=choices,
                            help_text=help_text
                        ))
            
            # Метод 2: Прямой доступ к атрибутам класса (fallback)
            # Это нужно для случаев, когда поля определены как атрибуты класса,
            # но еще не обработаны Django ORM
            if not fields:
                for attr_name in dir(model_class):
                    # Пропускаем служебные атрибуты
                    if attr_name.startswith('_') or attr_name in ['objects', 'DoesNotExist', 'MultipleObjectsReturned']:
                        continue
                    
                    attr = getattr(model_class, attr_name)
                    # Проверяем, является ли это полем модели (имеет атрибуты Field)
                    if hasattr(attr, 'name') or (hasattr(attr, '__class__') and 'Field' in attr.__class__.__name__):
                        try:
                            # Пробуем получить информацию о поле
                            field_name = getattr(attr, 'name', attr_name)
                            field_type = attr.__class__.__name__
                            
                            # Извлекаем label/verbose_name
                            verbose_name = getattr(attr, 'label', None)
                            if not verbose_name:
                                verbose_name = getattr(attr, 'verbose_name', None)
                            if not verbose_name:
                                verbose_name = field_name
                            
                            # Извлекаем choices
                            choices = getattr(attr, 'choices', None)
                            if callable(choices):
                                try:
                                    choices = choices()
                                except:
                                    choices = None
                            
                            help_text = getattr(attr, 'help_text', None)
                            
                            fields.append(FieldMetadata(
                                name=field_name,
                                field_type=field_type,
                                verbose_name=verbose_name,
                                choices=choices,
                                help_text=help_text
                            ))
                        except Exception:
                            continue
                            
        except Exception as e:
            warnings.warn(f"Failed to extract fields from {model_class.__name__}: {e}")
        
        return fields
    
    def _extract_fields_from_source_code(self, file_path: Path) -> Dict[str, List[FieldMetadata]]:
        """Извлекает поля моделей из исходного кода через AST парсинг.
        
        Это универсальный метод, который работает даже если otree не установлен.
        """
        result = {}
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()
            
            # Парсим AST
            tree = ast.parse(source_code, filename=str(file_path))
            
            # Ищем классы Player, Group, Subsession
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if node.name in ['Player', 'Group', 'Subsession']:
                        fields = []
                        
                        # Проходим по всем узлам в классе
                        for item in node.body:
                            if isinstance(item, ast.Assign):
                                # Это может быть определение поля
                                for target in item.targets:
                                    if isinstance(target, ast.Name):
                                        field_name = target.id
                                        
                                        # Пробуем извлечь информацию из вызова
                                        if isinstance(item.value, ast.Call):
                                            # Это вызов типа models.StringField(...)
                                            field_type = None
                                            label = None
                                            choices = None
                                            help_text = None
                                            
                                            # Получаем тип поля
                                            field_type = None
                                            if isinstance(item.value.func, ast.Attribute):
                                                # models.StringField или models.IntegerField
                                                if hasattr(item.value.func, 'attr'):
                                                    field_type = item.value.func.attr
                                                # Проверяем также value.func.value для случаев models.xxx
                                                if isinstance(item.value.func.value, ast.Name):
                                                    if item.value.func.value.id == 'models':
                                                        field_type = item.value.func.attr
                                            elif isinstance(item.value.func, ast.Name):
                                                field_type = item.value.func.id
                                            
                                            # Извлекаем аргументы
                                            for keyword in item.value.keywords:
                                                if keyword.arg == 'label':
                                                    # Извлекаем строковое значение
                                                    if isinstance(keyword.value, ast.Constant):
                                                        label = keyword.value.value
                                                    elif isinstance(keyword.value, ast.Str):  # Python < 3.8
                                                        label = keyword.value.s
                                                elif keyword.arg == 'choices':
                                                    # Извлекаем список choices
                                                    choices = self._extract_choices_from_ast(keyword.value)
                                                elif keyword.arg == 'help_text':
                                                    if isinstance(keyword.value, ast.Constant):
                                                        help_text = keyword.value.value
                                                    elif isinstance(keyword.value, ast.Str):
                                                        help_text = keyword.value.value
                                            
                                            if field_type and 'Field' in field_type:
                                                fields.append(FieldMetadata(
                                                    name=field_name,
                                                    field_type=field_type,
                                                    verbose_name=label or field_name,
                                                    choices=choices,
                                                    help_text=help_text
                                                ))
                        
                        if fields:
                            result[node.name] = fields
                            
        except Exception as e:
            warnings.warn(f"Failed to parse source code from {file_path}: {e}")
        
        return result
    
    def _extract_choices_from_ast(self, node) -> Optional[List]:
        """Извлекает список choices из AST узла."""
        try:
            if isinstance(node, ast.List):
                choices = []
                for elt in node.elts:
                    if isinstance(elt, (ast.List, ast.Tuple)):
                        # Это список/кортеж [value, label]
                        if len(elt.elts) >= 2:
                            value = self._extract_value_from_ast(elt.elts[0])
                            label = self._extract_value_from_ast(elt.elts[1])
                            choices.append((value, label))
                    else:
                        # Просто значение
                        value = self._extract_value_from_ast(elt)
                        choices.append(value)
                return choices
        except:
            pass
        return None
    
    def _extract_value_from_ast(self, node) -> Any:
        """Извлекает значение из AST узла."""
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Str):  # Python < 3.8
            return node.value
        elif isinstance(node, ast.Num):  # Python < 3.8
            return node.n
        elif isinstance(node, ast.NameConstant):  # Python < 3.8
            return node.value
        elif isinstance(node, ast.Name):
            return node.id
        return None
    
    def _extract_pages_from_source_code(self, file_path: Path, app_name: str, app_models: Optional[Dict[str, Dict[str, List[FieldMetadata]]]] = None) -> List[PageInfo]:
        """Извлекает страницы из исходного кода через AST парсинг.
        
        Это универсальный метод, который работает даже если otree не установлен.
        """
        pages = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()
            
            # Парсим AST
            tree = ast.parse(source_code, filename=str(file_path))
            
            # Ищем классы, которые наследуются от Page
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    # Проверяем базовые классы
                    is_page = False
                    for base in node.bases:
                        if isinstance(base, ast.Name) and base.id == 'Page':
                            is_page = True
                            break
                        elif isinstance(base, ast.Attribute):
                            if base.attr == 'Page' or (isinstance(base.value, ast.Name) and base.value.id in ['Page', 'otree']):
                                is_page = True
                                break
                    
                    if is_page:
                        # Извлекаем информацию о странице
                        class_name = node.name
                        form_model = None
                        form_fields = []
                        
                        # Проходим по телу класса
                        for item in node.body:
                            if isinstance(item, ast.Assign):
                                # Ищем form_model и form_fields
                                for target in item.targets:
                                    if isinstance(target, ast.Name):
                                        if target.id == 'form_model':
                                            # Извлекаем значение
                                            if isinstance(item.value, ast.Constant):
                                                form_model = item.value.value
                                            elif isinstance(item.value, ast.Str):
                                                form_model = item.value.value
                                            elif isinstance(item.value, ast.Name):
                                                form_model = item.value.id
                                        elif target.id == 'form_fields':
                                            # Извлекаем список
                                            if isinstance(item.value, ast.List):
                                                for elt in item.value.elts:
                                                    value = self._extract_value_from_ast(elt)
                                                    if value:
                                                        # Убираем кавычки, если это строка
                                                        value_str = str(value).strip("'\"")
                                                        form_fields.append(value_str)
                        
                        # Определяем имя шаблона
                        template_name = f"{app_name}/{class_name}.html"
                        
                        # Извлекаем текст шаблона
                        template_text = self.extract_template_text(app_name, template_name)
                        
                        # Извлекаем вопросы из form_fields
                        # app_models имеет структуру Dict[app_name, Dict[model_name, List[FieldMetadata]]]
                        # Нужно извлечь модели для этого app
                        app_model_fields = None
                        if app_models and app_name in app_models:
                            app_model_fields = app_models[app_name]
                        
                        questions = self._extract_questions_from_fields(
                            form_fields, form_model, app_name, app_model_fields
                        )
                        
                        page_info = PageInfo(
                            class_name=class_name,
                            app_name=app_name,
                            form_model=form_model,
                            form_fields=form_fields,
                            template_name=template_name,
                            template_text=template_text,
                            is_displayed_conditional=False,
                            questions=questions
                        )
                        pages.append(page_info)
                            
        except Exception as e:
            warnings.warn(f"Failed to extract pages from source code {file_path}: {e}")
        
        return pages
    
    def inspect_pages(self, app_name: str, app_models: Optional[Dict[str, Dict[str, List[FieldMetadata]]]] = None) -> List[PageInfo]:
        """Inspect pages in an app.
        
        Универсальный алгоритм проверяет все возможные места:
        1. pages.py (стандартное место)
        2. __init__.py (может содержать страницы)
        3. Другие .py файлы в директории приложения
        4. HTML шаблоны (fallback)
        """
        try:
            app_path = self.project_path / app_name
            if not app_path.exists():
                return []
            
            # Add project parent to path if needed
            if str(self.project_path.parent) not in sys.path:
                sys.path.insert(0, str(self.project_path.parent))
            
            pages = []
            
            # Try to import Page class first (но не обязательно)
            Page = None
            try:
                from otree.api import Page
            except (ImportError, ModuleNotFoundError):
                # otree не установлен, но мы все равно можем парсить через AST
                pass
            except Exception as e:
                # Другие ошибки при импорте otree (например, проблемы с конфигурацией проекта)
                # Игнорируем и используем AST парсинг
                warnings.warn(f"Could not import otree.api.Page (will use AST parsing): {type(e).__name__}")
                pass
            
            # Список файлов для проверки (в порядке приоритета)
            files_to_check = [
                app_path / 'pages.py',  # Стандартное место
                app_path / '__init__.py',  # Может содержать страницы
            ]
            
            # Также проверяем другие .py файлы в директории
            for py_file in app_path.glob('*.py'):
                if py_file not in files_to_check:
                    files_to_check.append(py_file)
            
            # Пробуем загрузить страницы из каждого файла
            for file_path in files_to_check:
                if not file_path.exists():
                    continue
                
                try:
                    # Определяем имя модуля
                    if file_path.name == '__init__.py':
                        module_name = app_name
                    elif file_path.name == 'pages.py':
                        module_name = f"{app_name}.pages"
                    else:
                        module_name = f"{app_name}.{file_path.stem}"
                    
                    # Метод 1: Пробуем загрузить модуль (если otree доступен)
                    if Page is not None:
                        try:
                            if module_name in self._imported_modules:
                                module = self._imported_modules[module_name]
                            else:
                                spec = importlib.util.spec_from_file_location(module_name, file_path)
                                module = importlib.util.module_from_spec(spec)
                                spec.loader.exec_module(module)
                                self._imported_modules[module_name] = module
                            
                            
                            # Find all classes that inherit from Page
                            for name in dir(module):
                                try:
                                    obj = getattr(module, name)
                                    if (isinstance(obj, type) and 
                                        issubclass(obj, Page) and 
                                        obj != Page):
                                        page_info = self._extract_page_info(obj, app_name, app_models)
                                        pages.append(page_info)
                                except Exception:
                                    # Skip items that can't be checked (might not be classes)
                                    continue
                            # Если нашли страницы, можно прекратить поиск
                            if pages:
                                break
                        except (ImportError, ModuleNotFoundError):
                            # Переходим к AST парсингу
                            pass
                        except Exception:
                            pass
                    
                    # Метод 2: Парсим исходный код через AST (всегда пробуем, даже если модуль загрузился)
                    # Это нужно, потому что даже если модуль загрузился, form_fields могут быть не извлечены правильно
                    try:
                        pages_from_ast = self._extract_pages_from_source_code(file_path, app_name, app_models)
                        # Добавляем только те страницы, которых еще нет
                        existing_names = {p.class_name for p in pages}
                        for page_ast in pages_from_ast:
                            if page_ast.class_name not in existing_names:
                                pages.append(page_ast)
                        
                        if pages:
                            break
                    except Exception:
                        continue
                        
                except Exception:
                    # Пробуем AST как fallback
                    try:
                        pages_from_ast = self._extract_pages_from_source_code(file_path, app_name, app_models)
                        pages.extend(pages_from_ast)
                        if pages:
                            break
                    except:
                        continue
            
            # Fallback: if no pages found, try to infer from HTML templates
            if not pages:
                pages = self._inspect_pages_from_templates(app_name, app_models)
            
            return pages
        except Exception as e:
            # Не показываем полный traceback для обычных ошибок импорта
            error_type = type(e).__name__
            error_msg = str(e)
            # Пропускаем предупреждения для известных проблем с otree
            if (error_type in ['ImportError', 'ModuleNotFoundError', 'ValueError'] or
                'Package' in error_msg or 
                'could not be found' in error_msg or 
                'otree' in error_msg.lower() or
                '__init__.py' in error_msg):
                # Это нормально - otree может быть не настроен, используем AST парсинг
                pass
            else:
                # Показываем только неожиданные ошибки
                warnings.warn(f"Failed to inspect pages for {app_name}: {error_type}: {error_msg}")
            # Fallback to templates
            return self._inspect_pages_from_templates(app_name, app_models)
    
    def _inspect_pages_from_templates(self, app_name: str, app_models: Optional[Dict[str, Dict[str, List[FieldMetadata]]]] = None) -> List[PageInfo]:
        """Fallback: infer pages from HTML template files in app directory."""
        pages = []
        app_path = self.project_path / app_name
        
        if not app_path.exists() or not app_path.is_dir():
            return pages
        
        # Look for HTML files in app directory
        html_files = list(app_path.glob('*.html'))
        
        for html_file in html_files:
            # Extract page name from filename (e.g., Experiment.html -> Experiment)
            page_name = html_file.stem
            
            # Skip common template names
            if page_name.lower() in ['page', 'base']:
                continue
            
            # Try to extract template text (directly from file path)
            try:
                with open(html_file, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                
                # Extract text using BeautifulSoup if available, else regex
                if HAS_BS4:
                    soup = BeautifulSoup(html_content, 'html.parser')
                    # Remove script and style elements
                    for script in soup(["script", "style"]):
                        script.decompose()
                    text = soup.get_text()
                    # Clean up whitespace
                    lines = (line.strip() for line in text.splitlines())
                    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                    template_text = ' '.join(chunk for chunk in chunks if chunk)
                else:
                    # Fallback: simple regex-based extraction
                    text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                    text = re.sub(r'<[^>]+>', '', text)
                    text = re.sub(r'\s+', ' ', text)
                    template_text = text.strip()
            except Exception as e:
                warnings.warn(f"Failed to extract text from template {html_file}: {e}")
                template_text = None
            
            # Try to extract questions from HTML or app_models
            # Но только если нет form_fields - иначе вопросы будут извлечены из form_fields
            questions = []
            
            # Create PageInfo from template
            page_info = PageInfo(
                class_name=page_name,
                app_name=app_name,
                form_model=None,
                form_fields=[],
                template_name=f"{app_name}/{html_file.name}",
                template_text=template_text,
                is_displayed_conditional=False,
                questions=questions
            )
            pages.append(page_info)
            
            return pages
    
    def _extract_questions_from_html(
        self, 
        html_content: str, 
        app_name: str,
        app_models: Optional[Dict[str, List[FieldMetadata]]] = None
    ) -> List[Question]:
        """Try to extract questions from HTML content."""
        questions = []
        
        # If we have app_models, try to use them
        # For pages with {{ formfields }}, we need to know which fields are used
        # This is tricky without pages.py, but we can try to infer from models
        
        if app_models:
            # Try to find Player model fields (most common)
            player_fields = app_models.get('Player', [])
            for field_meta in player_fields:
                answer_options = []
                if field_meta.choices:
                    for choice in field_meta.choices:
                        if isinstance(choice, (tuple, list)) and len(choice) >= 2:
                            answer_options.append(str(choice[1]))
                        else:
                            answer_options.append(str(choice))
                
                question = Question(
                    field_name=field_meta.name,
                    question_text=field_meta.verbose_name or field_meta.name,
                    field_type=field_meta.field_type,
                    answer_options=answer_options,
                    help_text=field_meta.help_text,
                    is_required=True
                )
                questions.append(question)
        
        # If no questions found from models, try to extract from HTML template text
        # Look for text that might be questions (before {{ formfields }})
        if not questions:
            # Extract text content from template (removing Jinja2 syntax)
            # Remove Jinja2 blocks and variables
            text_content = re.sub(r'\{\{.*?\}\}', '', html_content)
            text_content = re.sub(r'\{\%.*?\%\}', '', text_content, flags=re.DOTALL)
            
            # Try to find question-like text (sentences ending with ? or : before formfields)
            # Look for paragraphs or text blocks that might be questions
            if HAS_BS4:
                try:
                    soup = BeautifulSoup(text_content, 'html.parser')
                    # Remove script and style
                    for script in soup(["script", "style"]):
                        script.decompose()
                    
                    # Find text that might be questions
                    # Look for <p>, <h3>, <h4> tags that might contain questions
                    question_elements = soup.find_all(['p', 'h3', 'h4', 'h5', 'label', 'div'])
                    for elem in question_elements:
                        text = elem.get_text(strip=True)
                        # Skip if too short or looks like navigation/button text
                        if len(text) < 10 or text.lower() in ['далее', 'next', 'submit', 'закончить']:
                            continue
                        # Check if it looks like a question (contains question words or ends with ?)
                        if ('?' in text or 
                            any(word in text.lower() for word in ['какой', 'какая', 'какое', 'какие', 'сколько', 'когда', 'где', 'как', 'что', 'кто']) or
                            text.endswith(':')):
                            # Try to find associated form elements
                            answer_options = []
                            
                            # Look for radio buttons, checkboxes, or select options nearby
                            next_elem = elem.find_next(['input', 'select'])
                            if next_elem:
                                if next_elem.name == 'select':
                                    options = next_elem.find_all('option')
                                    for option in options:
                                        opt_text = option.get_text(strip=True)
                                        if opt_text:
                                            answer_options.append(opt_text)
                                elif next_elem.get('type') in ['radio', 'checkbox']:
                                    field_name = next_elem.get('name', '')
                                    # Find all inputs with same name
                                    all_inputs = soup.find_all('input', {'name': field_name, 'type': next_elem.get('type')})
                                    for inp in all_inputs:
                                        # Try to find label
                                        inp_id = inp.get('id')
                                        if inp_id:
                                            label = soup.find('label', {'for': inp_id})
                                            if label:
                                                answer_options.append(label.get_text(strip=True))
                            
                            question = Question(
                                field_name=f"question_{len(questions) + 1}",
                                question_text=text,
                                field_type=None,
                                answer_options=answer_options,
                                help_text=None,
                                is_required=True
                            )
                            questions.append(question)
                except Exception as e:
                    warnings.warn(f"Failed to extract questions from HTML: {e}")
                    warnings.warn(f"Failed to extract questions from HTML: {e}")
    
        return questions
    
    def _extract_page_info(self, page_class, app_name: str, app_models: Optional[Dict[str, Dict[str, List[FieldMetadata]]]] = None) -> PageInfo:
        """Extract information from a Page class."""
        form_model = getattr(page_class, 'form_model', None)
        form_fields = getattr(page_class, 'form_fields', [])
        template_name = getattr(page_class, 'template_name', None)
        
        # Check if is_displayed method exists and is not inherited from base
        # Check if method is defined in this class (not just inherited)
        has_is_displayed = False
        if hasattr(page_class, 'is_displayed'):
            # Check if method is actually defined in this class
            method = getattr(page_class, 'is_displayed')
            if hasattr(method, '__qualname__'):
                # Method is defined in this class if qualname starts with class name
                has_is_displayed = method.__qualname__.startswith(page_class.__name__ + '.')
            elif 'is_displayed' in page_class.__dict__:
                # Fallback: check if it's in the class dict
                has_is_displayed = True
        
        # If template_name not set, use default pattern
        if template_name is None:
            template_name = f"{app_name}/{page_class.__name__}.html"
        
        # Extract template text
        template_text = None
        if template_name:
            template_text = self.extract_template_text(app_name, template_name)
        
        # Extract questions from form fields
        # app_models имеет структуру Dict[app_name, Dict[model_name, List[FieldMetadata]]]
        # Нужно извлечь модели для этого app
        app_model_fields = None
        if app_models and app_name in app_models:
            app_model_fields = app_models[app_name]
        
        questions = self._extract_questions_from_fields(
            form_fields, form_model, app_name, app_model_fields
        )
        
        return PageInfo(
            class_name=page_class.__name__,
            app_name=app_name,
            form_model=form_model,
            form_fields=form_fields if isinstance(form_fields, list) else [],
            template_name=template_name,
            template_text=template_text,
            is_displayed_conditional=has_is_displayed,
            questions=questions
        )
    
    def _extract_questions_from_fields(
        self, 
        form_fields: List[str], 
        form_model: Optional[str],
        app_name: str,
        app_models: Optional[Dict[str, List[FieldMetadata]]] = None
    ) -> List[Question]:
        """Extract questions with answer options from form fields."""
        questions = []
        
        if not form_fields or not form_model:
            return questions
        
        # Get model fields metadata
        # Преобразуем form_model в имя класса модели (player -> Player)
        model_class_name = form_model.capitalize() if form_model else None
        
        model_fields_map = {}
        if app_models and model_class_name and model_class_name in app_models:
            for field_meta in app_models[model_class_name]:
                model_fields_map[field_meta.name] = field_meta
        
        # Create Question objects for each form field
        for field_name in form_fields:
            field_meta = model_fields_map.get(field_name)
            
            # Extract answer options from choices
            answer_options = []
            if field_meta and field_meta.choices:
                # Choices can be a list of tuples like [(value, label), ...]
                # or a list of values
                for choice in field_meta.choices:
                    if isinstance(choice, (tuple, list)) and len(choice) >= 2:
                        # (value, label) format - используем label (второй элемент)
                        label = choice[1] if len(choice) > 1 else choice[0]
                        answer_options.append(str(label))
                    else:
                        # Just value
                        answer_options.append(str(choice))
            
            question = Question(
                field_name=field_name,
                question_text=field_meta.verbose_name if field_meta else field_name,
                field_type=field_meta.field_type if field_meta else None,
                answer_options=answer_options,
                help_text=field_meta.help_text if field_meta else None,
                is_required=True  # oTree fields are typically required
            )
            questions.append(question)
        
        return questions
    
    def extract_template_text(self, app_name: str, template_name: str) -> Optional[str]:
        """Extract plain text from HTML template."""
        # Try to find template file
        app_path = self.project_path / app_name
        template_paths = [
            app_path / 'templates' / template_name,
            app_path / 'templates' / app_name / template_name.split('/')[-1],
            self.project_path / '_templates' / 'global' / template_name.split('/')[-1],
        ]
        
        template_path = None
        for path in template_paths:
            if path.exists():
                template_path = path
                break
        
        if not template_path:
            return None
        
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
            
            # Extract text using BeautifulSoup if available, else regex
            if HAS_BS4:
                soup = BeautifulSoup(html_content, 'html.parser')
                # Remove script and style elements
                for script in soup(["script", "style"]):
                    script.decompose()
                text = soup.get_text()
                # Clean up whitespace
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                text = ' '.join(chunk for chunk in chunks if chunk)
                return text
            else:
                # Fallback: simple regex-based extraction
                # Remove script and style tags
                text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                # Remove HTML tags
                text = re.sub(r'<[^>]+>', '', text)
                # Clean up whitespace
                text = re.sub(r'\s+', ' ', text)
                return text.strip()
        except Exception as e:
            warnings.warn(f"Failed to extract text from template {template_path}: {e}")
            return None
    
    def extract_treatment_variants(self, app_name: str) -> Dict[str, List[str]]:
        """
        Извлекает все возможные варианты treatment из кода приложения.
        
        Ищет в Subsession.creating_session() присваивания participant.vars[...]
        и списки, из которых выбираются значения.
        
        Args:
            app_name: имя приложения
            
        Returns:
            Dict с ключами - именами treatment переменных, значениями - списки вариантов
            Пример: {'donation_text': ['вариант 1', 'вариант 2'], ...}
        """
        treatment_variants = {}
        app_path = self.project_path / app_name
        
        if not app_path.exists():
            return treatment_variants
        
        # Проверяем все .py файлы в приложении
        for py_file in app_path.glob('*.py'):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    source_code = f.read()
                
                # Парсим AST
                tree = ast.parse(source_code, filename=str(py_file))
                
                # Ищем creating_session - может быть методом класса Subsession или отдельной функцией
                creating_session_func = None
                
                # Сначала ищем в классе Subsession
                for node in tree.body:
                    if isinstance(node, ast.ClassDef) and node.name == 'Subsession':
                        # Ищем метод creating_session
                        for item in node.body:
                            if isinstance(item, ast.FunctionDef) and item.name == 'creating_session':
                                creating_session_func = item
                                break
                        if creating_session_func:
                            break
                
                # Если не нашли в классе, ищем как отдельную функцию
                if not creating_session_func:
                    for node in tree.body:
                        if isinstance(node, ast.FunctionDef) and node.name == 'creating_session':
                            # Проверяем, что это функция creating_session (может быть с параметром subsession)
                            creating_session_func = node
                            break
                
                # Если нашли функцию, извлекаем treatment
                if creating_session_func:
                    treatment_variants.update(
                        self._extract_treatment_from_method(creating_session_func, source_code)
                    )
            except Exception as e:
                warnings.warn(f"Failed to extract treatment variants from {py_file}: {e}")
                continue
        
        return treatment_variants
    
    def _extract_treatment_from_method(self, method_node: ast.FunctionDef, source_code: str) -> Dict[str, List[str]]:
        """Извлекает варианты treatment из метода creating_session."""
        treatment_variants = {}
        
        # Ищем переменные, которые присваиваются в participant.vars[...]
        # и списки, из которых они выбираются
        
        # Сначала собираем все списки (переменные со списками значений)
        list_variables = {}  # имя_переменной -> список_значений
        
        for item in method_node.body:
            # Ищем присваивания вида: var_name = [...]
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        var_name = target.id
                        # Проверяем, является ли значение списком
                        if isinstance(item.value, ast.List):
                            values = []
                            for elt in item.value.elts:
                                value = self._extract_value_from_ast(elt)
                                if value is not None:
                                    # Если это строка, убираем HTML теги для чистоты
                                    if isinstance(value, str):
                                        import re
                                        value = re.sub(r'<[^>]+>', '', value)  # Убираем HTML теги
                                    values.append(str(value))
                            if values:
                                list_variables[var_name] = values
        
        # Отслеживаем промежуточные переменные (например, selected_frame = random.choice(donation_frames))
        intermediate_vars = {}  # имя_переменной -> список_значений
        
        # Отслеживаем промежуточные переменные (например, selected_frame = random.choice(donation_frames))
        intermediate_vars = {}  # имя_переменной -> список_значений
        
        # Рекурсивная функция для поиска присваиваний в любых вложенных структурах
        def find_participant_vars_assignments(node):
            """Рекурсивно ищет присваивания participant.vars[...] = ..."""
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        var_name = target.id
                        # Проверяем, является ли это промежуточной переменной, которая получает значение из random.choice()
                        if isinstance(node.value, ast.Call):
                            if (isinstance(node.value.func, ast.Attribute) and
                                node.value.func.attr == 'choice' and
                                isinstance(node.value.func.value, ast.Name) and
                                node.value.func.value.id == 'random'):
                                # Получаем аргумент random.choice()
                                if node.value.args:
                                    arg = node.value.args[0]
                                    if isinstance(arg, ast.Name) and arg.id in list_variables:
                                        # Сохраняем промежуточную переменную
                                        intermediate_vars[var_name] = list_variables[arg.id]
                    
                    # Ищем паттерн: participant.vars['key'] = value или player.participant.vars['key'] = value
                    if isinstance(target, ast.Subscript):
                        if isinstance(target.value, ast.Attribute):
                            # Проверяем разные варианты структуры:
                            # 1. participant.vars['key'] - target.value.value это Name
                            # 2. player.participant.vars['key'] - target.value.value это Attribute
                            is_participant_vars = False
                            if target.value.attr == 'vars':
                                # Вариант 1: participant.vars
                                if (isinstance(target.value.value, ast.Name) and 
                                    target.value.value.id in ['player', 'participant']):
                                    is_participant_vars = True
                                # Вариант 2: player.participant.vars
                                elif (isinstance(target.value.value, ast.Attribute) and
                                      target.value.value.attr == 'participant' and
                                      isinstance(target.value.value.value, ast.Name) and
                                      target.value.value.value.id == 'player'):
                                    is_participant_vars = True
                            
                            if is_participant_vars:
                                # Извлекаем ключ
                                if isinstance(target.slice, ast.Constant):
                                    var_key = target.slice.value
                                elif isinstance(target.slice, ast.Str):  # Python < 3.8
                                    var_key = target.slice.s
                                elif isinstance(target.slice, ast.Index):  # Python < 3.8
                                    if isinstance(target.slice.value, (ast.Constant, ast.Str)):
                                        var_key = target.slice.value.value if hasattr(target.slice.value, 'value') else target.slice.value.s
                                    else:
                                        continue
                                else:
                                    continue
                                
                                # Извлекаем значение
                                if isinstance(node.value, ast.Call):
                                    # random.choice(list_var)
                                    if (isinstance(node.value.func, ast.Attribute) and
                                        node.value.func.attr == 'choice' and
                                        isinstance(node.value.func.value, ast.Name) and
                                        node.value.func.value.id == 'random'):
                                        # Получаем аргумент random.choice()
                                        if node.value.args:
                                            arg = node.value.args[0]
                                            if isinstance(arg, ast.Name) and arg.id in list_variables:
                                                # Нашли! Сохраняем варианты
                                                treatment_variants[var_key] = list_variables[arg.id]
                                elif isinstance(node.value, ast.Name):
                                    # Прямое присваивание переменной (может быть промежуточной)
                                    if node.value.id in list_variables:
                                        treatment_variants[var_key] = list_variables[node.value.id]
                                    elif node.value.id in intermediate_vars:
                                        # Используем промежуточную переменную
                                        treatment_variants[var_key] = intermediate_vars[node.value.id]
            
            # Рекурсивно обходим дочерние узлы
            for child in ast.iter_child_nodes(node):
                find_participant_vars_assignments(child)
        
        # Ищем присваивания во всем теле метода (включая вложенные циклы)
        for item in method_node.body:
            find_participant_vars_assignments(item)
        
        return treatment_variants
    
    def extract_template_vars(self, app_name: str) -> Dict[str, List[str]]:
        """
        Извлекает переменные, используемые в vars_for_template() для шаблонов.
        
        Args:
            app_name: имя приложения
            
        Returns:
            Dict с ключами - именами переменных, значениями - списки возможных значений
        """
        template_vars = {}
        app_path = self.project_path / app_name
        
        if not app_path.exists():
            return template_vars
        
        # Проверяем все .py файлы
        for py_file in app_path.glob('*.py'):
            try:
                with open(py_file, 'r', encoding='utf-8') as f:
                    source_code = f.read()
                
                tree = ast.parse(source_code, filename=str(py_file))
                
                # Ищем классы Page с методом vars_for_template
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        # Проверяем, является ли это Page классом
                        is_page = any(
                            (isinstance(base, ast.Name) and base.id == 'Page') or
                            (isinstance(base, ast.Attribute) and base.attr == 'Page')
                            for base in node.bases
                        )
                        
                        if is_page:
                            for item in node.body:
                                if isinstance(item, ast.FunctionDef) and item.name == 'vars_for_template':
                                    # Ищем возвращаемые значения
                                    for stmt in item.body:
                                        if isinstance(stmt, ast.Return):
                                            if isinstance(stmt.value, ast.Dict):
                                                # Извлекаем ключи и значения из словаря
                                                for key, value in zip(stmt.value.keys, stmt.value.values):
                                                    if key:
                                                        key_name = self._extract_value_from_ast(key)
                                                        if key_name:
                                                            # Пытаемся извлечь значение
                                                            if isinstance(value, ast.Call):
                                                                # participant.vars.get('key')
                                                                if (isinstance(value.func, ast.Attribute) and
                                                                    value.func.attr == 'get' and
                                                                    isinstance(value.func.value, ast.Attribute) and
                                                                    value.func.value.attr == 'vars'):
                                                                    # Получаем ключ из аргументов
                                                                    if value.args:
                                                                        var_key = self._extract_value_from_ast(value.args[0])
                                                                        if var_key:
                                                                            # Ищем этот ключ в treatment_variants
                                                                            treatment_variants = self.extract_treatment_variants(app_name)
                                                                            if var_key in treatment_variants:
                                                                                template_vars[key_name] = treatment_variants[var_key]
            except Exception as e:
                warnings.warn(f"Failed to extract template vars from {py_file}: {e}")
                continue
        
        return template_vars


# ============================================================================
# QuestionnaireBuilder
# ============================================================================

class QuestionnaireBuilder:
    """Builder for determining visible pages and building questionnaire structure."""
    
    def __init__(self, inspector: OTreeInspector, mock_factory: MockObjectFactory):
        self.inspector = inspector
        self.mock_factory = mock_factory
    
    def determine_visible_pages(self, treatment: Treatment, inspector: OTreeInspector, app_models: Optional[Dict[str, Dict[str, List[FieldMetadata]]]] = None) -> List[PageInfo]:
        """Determine which pages are visible for a treatment."""
        visible_pages = []
        
        # Get all pages from apps in sequence
        all_pages = {}
        page_sequences = {}  # Словарь для хранения page_sequence для каждого app
        
        for app_name in treatment.app_sequence:
            # app_models имеет структуру Dict[app_name, Dict[model_name, List[FieldMetadata]]]
            # inspect_pages ожидает ту же структуру, но только для одного app
            # Создаем словарь только для этого app
            app_specific_models = None
            if app_models and app_name in app_models:
                app_specific_models = {app_name: app_models[app_name]}
            pages = inspector.inspect_pages(app_name, app_specific_models)
            for page in pages:
                # Use app_name.class_name as key
                key = f"{app_name}.{page.class_name}"
                all_pages[key] = page
        
        # Получаем page_sequence для каждого app
        for app_name in treatment.app_sequence:
            try:
                app_path = inspector.project_path / app_name
                pages_path = app_path / 'pages.py'
                if pages_path.exists():
                    spec = importlib.util.spec_from_file_location(f"{app_name}.pages", pages_path)
                    pages_module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(pages_module)
                    
                    if hasattr(pages_module, 'page_sequence'):
                        page_sequences[app_name] = pages_module.page_sequence
            except Exception as e:
                warnings.warn(f"Failed to get page_sequence for {app_name}: {e}")
        
            if app_name in page_sequences:
                app_path = inspector.project_path / app_name
                pages_path = app_path / 'pages.py'
                for page_class in page_sequences[app_name]:
                    page_key = f"{app_name}.{page_class.__name__}"
                    if page_key in all_pages:
                        page_info = all_pages[page_key]
                        if self._is_page_visible(page_info, page_class, app_name, treatment.config):
                            visible_pages.append(page_info)
            else:
                # If no page_sequence, check all pages for this app
                app_pages = [page_info for page_key, page_info in all_pages.items() 
                            if page_info.app_name == app_name]
                
                # If pages were found from templates (no pages.py), add them all
                app_path = inspector.project_path / app_name
                pages_path = app_path / 'pages.py'
                
                if not pages_path.exists():
                    # No pages.py - add all pages found from templates
                    for page_info in app_pages:
                        if page_info not in visible_pages:
                            visible_pages.append(page_info)
                else:
                    # Try to check visibility using pages.py
                    for page_info in app_pages:
                        try:
                            spec = importlib.util.spec_from_file_location(f"{app_name}.pages", pages_path)
                            pages_module = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(pages_module)
                            page_class = getattr(pages_module, page_info.class_name, None)
                            
                            if page_class and self._is_page_visible(page_info, page_class, app_name, treatment.config):
                                if page_info not in visible_pages:
                                    visible_pages.append(page_info)
                        except Exception as e:
                            warnings.warn(f"Failed to check visibility for {page_info.class_name}: {e}")
                            # If we can't check, assume visible
                            if page_info not in visible_pages:
                                visible_pages.append(page_info)
        
        return visible_pages
    
    def _is_page_visible(self, page_info: PageInfo, page_class, app_name: str, session_config: Dict[str, Any]) -> bool:
        """Check if a page is visible by calling is_displayed() if it exists."""
        # If no is_displayed method, page is always visible
        if not page_info.is_displayed_conditional:
            return True
        
        try:
            # Create mock page instance
            page_instance = self.mock_factory.create_mock_page_instance(
                page_class, app_name, round_number=1, session_config=session_config
            )
            
            if page_instance is None:
                # If we can't create instance, assume visible (fallback)
                return True
            
            # Call is_displayed()
            result = page_instance.is_displayed()
            return bool(result)
        except Exception as e:
            warnings.warn(f"Error calling is_displayed() for {page_info.class_name}: {e}")
            # Fallback: assume visible
            return True
    
    def build_questionnaire(self, treatment: Treatment, app_models: Optional[Dict[str, Dict[str, List[FieldMetadata]]]] = None) -> Treatment:
        """Enrich treatment with visible pages."""
        visible_pages = self.determine_visible_pages(treatment, self.inspector, app_models)
        treatment.visible_pages = visible_pages
        return treatment


# ============================================================================
# Public API
# ============================================================================

def parse_otreezip(otreezip_path: str) -> Experiment:
    """
    Main parsing function.
    
    Args:
        otreezip_path: path to .otreezip file
        
    Returns:
        Experiment: structured description of the experiment
    """
    inspector = OTreeInspector(Path())
    mock_factory = MockObjectFactory(Path())
    builder = QuestionnaireBuilder(inspector, mock_factory)
    
    # Extract .otreezip
    project_path = inspector.extract_otreezip(otreezip_path)
    inspector.project_path = project_path
    mock_factory.project_path = project_path
    
    # Import project
    inspector.import_project(project_path)
    
    # Get SESSION_CONFIGS from settings
    if not inspector.settings_module:
        raise ValueError("Failed to import settings module")
    
    session_configs = getattr(inspector.settings_module, 'SESSION_CONFIGS', [])
    if not session_configs:
        raise ValueError("No SESSION_CONFIGS found in settings")
    
    # Get project name
    project_name = getattr(inspector.settings_module, 'PROJECT_NAME', project_path.name)
    
    # Process each treatment
    treatments = []
    app_models = {}
    
    for config in session_configs:
        name = config.get('name', 'unnamed')
        display_name = config.get('display_name', name)
        app_sequence = config.get('app_sequence', [])
        
        # Inspect models for each app
        for app_name in app_sequence:
            if app_name not in app_models:
                app_models[app_name] = inspector.inspect_models(app_name)
        
        # Create treatment
        treatment = Treatment(
            name=name,
            display_name=display_name,
            app_sequence=app_sequence,
            config=config
        )
        
        # Build questionnaire (determine visible pages)
        treatment = builder.build_questionnaire(treatment, app_models)
        
        treatments.append(treatment)
    
    # Create experiment
    experiment = Experiment(
        project_name=project_name,
        treatments=treatments,
        app_models=app_models
    )
    
    # Сохраняем inspector и project_path для последующего использования
    experiment._inspector = inspector
    experiment._project_path = project_path
    
    return experiment


# ============================================================================
# Helper Functions for Extracting Questionnaire Text
# ============================================================================

def extract_questionnaire_texts(experiment: Experiment, treatment_name: Optional[str] = None) -> List[str]:
    """
    Извлекает тексты всех вопросов анкеты.
    
    Args:
        experiment: объект Experiment из parse_otreezip()
        treatment_name: имя treatment (если None, берется первый)
    
    Returns:
        Список текстов вопросов
    """
    if treatment_name:
        treatment = next((t for t in experiment.treatments if t.name == treatment_name), None)
        if not treatment:
            raise ValueError(f"Treatment '{treatment_name}' not found")
    else:
        if not experiment.treatments:
            raise ValueError("No treatments found in experiment")
        treatment = experiment.treatments[0]
    
    questions_texts = []
    for page in treatment.visible_pages:
        for question in page.questions:
            if question.question_text:
                questions_texts.append(question.question_text)
    
    return questions_texts


def format_questionnaire_for_llm(experiment: Experiment, treatment_name: Optional[str] = None) -> str:
    """
    Форматирует эксперимент в текстовый формат для отправки в LLM.
    
    Args:
        experiment: объект Experiment из parse_otreezip()
        treatment_name: имя treatment (если None, берется первый)
    
    Returns:
        Форматированная строка с анкетой
    """
    if treatment_name:
        treatment = next((t for t in experiment.treatments if t.name == treatment_name), None)
        if not treatment:
            raise ValueError(f"Treatment '{treatment_name}' not found")
    else:
        if not experiment.treatments:
            raise ValueError("No treatments found in experiment")
        treatment = experiment.treatments[0]
    
    # Формируем текст
    parts = []
    parts.append(f"# Эксперимент: {experiment.project_name}")
    parts.append(f"## Treatment: {treatment.display_name}")
    parts.append("")
    parts.append("## Структура анкеты:")
    parts.append("")
    
    # Добавляем страницы по порядку
    for i, page in enumerate(treatment.visible_pages, 1):
        parts.append(f"### Страница {i}: {page.class_name} (App: {page.app_name})")
        
        if page.form_fields:
            parts.append(f"Поля формы: {', '.join(page.form_fields)}")
        
        # Добавляем вопросы
        if page.questions:
            parts.append("")
            parts.append("**Вопросы:**")
            for j, question in enumerate(page.questions, 1):
                parts.append(f"{j}. {question.question_text}")
                if question.answer_options:
                    parts.append(f"   Варианты ответов: {', '.join(question.answer_options)}")
                if question.help_text:
                    parts.append(f"   Подсказка: {question.help_text}")
        elif page.template_text:
            parts.append("")
            parts.append("**Текст страницы:**")
            parts.append(page.template_text)
        else:
            parts.append("(Текст шаблона не найден)")
        
        parts.append("")
        parts.append("---")
        parts.append("")
    
    return "\n".join(parts)


def get_all_questions_with_options(experiment: Experiment, treatment_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Извлекает все вопросы с вариантами ответов в структурированном виде.
    
    Args:
        experiment: объект Experiment из parse_otreezip()
        treatment_name: имя treatment (если None, берется первый)
    
    Returns:
        Список словарей с информацией о вопросах:
        [
            {
                'page': 'Demographics',
                'question_text': 'Укажите свой возраст',
                'field_name': 'age',
                'answer_options': [],
                'field_type': 'FloatField',
                'help_text': None
            },
            ...
        ]
    """
    if treatment_name:
        treatment = next((t for t in experiment.treatments if t.name == treatment_name), None)
        if not treatment:
            raise ValueError(f"Treatment '{treatment_name}' not found")
    else:
        if not experiment.treatments:
            raise ValueError("No treatments found in experiment")
        treatment = experiment.treatments[0]
    
    questions_list = []
    for page in treatment.visible_pages:
        for question in page.questions:
            questions_list.append({
                'page': page.class_name,
                'app': page.app_name,
                'question_text': question.question_text or question.field_name,
                'field_name': question.field_name,
                'answer_options': question.answer_options,
                'field_type': question.field_type,
                'help_text': question.help_text,
                'is_required': question.is_required
            })
    
    return questions_list


def build_questionnaire_for_api(experiment: Experiment, treatment_name: Optional[str] = None, include_all_treatment_variants: bool = True, format_for_llm: bool = True) -> Union[List[Dict[str, Any]], List[List[Dict[str, Any]]], List[List[str]]]:
    """
    Создает полную структурированную анкету для последовательной отправки в LLM API.
    
    Args:
        experiment: объект Experiment из parse_otreezip()
        treatment_name: имя treatment из SESSION_CONFIGS (если None, берется первый)
        include_all_treatment_variants: если True, создает отдельные анкеты
                                       для каждого возможного treatment значения
        format_for_llm: если True, форматирует каждый элемент как читаемый текст для LLM
    
    Returns:
        Если include_all_treatment_variants=False:
            Если format_for_llm=True: список строк (текстовые элементы для LLM)
            Если format_for_llm=False: список словарей с детальной информацией
        
        Если include_all_treatment_variants=True:
            Список анкет, где каждая анкета - это список элементов для одного treatment варианта.
            Если format_for_llm=True: каждая анкета - список строк (текстовые элементы)
            Если format_for_llm=False: каждая анкета - список словарей с детальной информацией
            
            Формат при format_for_llm=True:
            [
                [  # Анкета для treatment варианта 1
                    "ИНСТРУКЦИЯ: [текст инструкции]",
                    "ВОПРОС 1: [текст вопроса]\nВарианты ответов: [варианты]",
                    "ВОПРОС 2: [текст вопроса]\nВарианты ответов: [варианты]",
                    ...
                ],
                [  # Анкета для treatment варианта 2
                    "ИНСТРУКЦИЯ: [текст инструкции]",
                    "ВОПРОС 1: [текст вопроса]\nВарианты ответов: [варианты]",
                    ...
                ]
            ]
    """
    if treatment_name:
        treatment = next((t for t in experiment.treatments if t.name == treatment_name), None)
        if not treatment:
            raise ValueError(f"Treatment '{treatment_name}' not found")
    else:
        if not experiment.treatments:
            raise ValueError("No treatments found in experiment")
        treatment = experiment.treatments[0]
    
    # Получаем inspector из experiment для извлечения текста шаблонов
    inspector = getattr(experiment, '_inspector', None)
    
    # Извлекаем все варианты treatment для всех приложений
    all_treatment_variants = {}
    if include_all_treatment_variants and inspector:
        for app_name in treatment.app_sequence:
            treatment_variants = inspector.extract_treatment_variants(app_name)
            if treatment_variants:
                all_treatment_variants[app_name] = treatment_variants
        
        # Также извлекаем переменные из vars_for_template
        for app_name in treatment.app_sequence:
            vars_dict = inspector.extract_template_vars(app_name)
            if vars_dict:
                if app_name not in all_treatment_variants:
                    all_treatment_variants[app_name] = {}
                all_treatment_variants[app_name].update(vars_dict)
    
    # Если есть варианты treatment, создаем отдельные анкеты для каждого
    if all_treatment_variants:
        # Генерируем все комбинации treatment
        treatment_combinations = _generate_treatment_combinations(all_treatment_variants)
        
        # Если комбинаций нет, используем пустой словарь (базовый вариант)
        if not treatment_combinations:
            treatment_combinations = [{}]
        
        questionnaires = []  # Список анкет, каждая для своего treatment
        
        # Создаем отдельную анкету для каждой комбинации treatment
        for combo_idx, treatment_combo in enumerate(treatment_combinations, 1):
            items = _build_questionnaire_for_treatment_combo(
                treatment, inspector, treatment_combo, combo_idx, len(treatment_combinations)
            )
            
            # Добавляем информацию о treatment в первый элемент анкеты
            if items:
                # Формируем словарь с информацией о treatment для этого варианта
                treatment_info = {}
                for key, value in treatment_combo.items():
                    if ':' in key:
                        app_name_part, var_name = key.split(':', 1)
                        treatment_info[var_name] = value
                
                # Добавляем treatment_info в первый элемент
                if treatment_info:
                    items[0]['treatment_info'] = treatment_info
                    items[0]['treatment_combo_number'] = combo_idx
                    items[0]['total_treatment_combos'] = len(treatment_combinations)
            
            # Форматируем для LLM, если нужно
            if format_for_llm:
                formatted_questionnaire = _format_questionnaire_for_llm(items)
                questionnaires.append(formatted_questionnaire)
            else:
                questionnaires.append(items)
        
        return questionnaires
    else:
        # Нет вариантов treatment - создаем обычную анкету (один список)
        items = _build_questionnaire_for_treatment_combo(treatment, inspector, {}, 1, 1)
        if format_for_llm:
            return _format_questionnaire_for_llm(items)
        else:
            return items


def _generate_treatment_combinations(treatment_variants: Dict[str, Dict[str, List[str]]]) -> List[Dict[str, str]]:
    """
    Генерирует все возможные комбинации treatment.
    
    Args:
        treatment_variants: Dict[app_name, Dict[var_name, List[values]]]
        
    Returns:
        Список словарей с комбинациями: [{'app_name:var_name': 'value', ...}, ...]
    """
    if not treatment_variants:
        return []
    
    # Собираем все пары (app_name, var_name) -> values
    all_vars = []
    for app_name, vars_dict in treatment_variants.items():
        for var_name, values in vars_dict.items():
            key = f"{app_name}:{var_name}"
            all_vars.append((key, values))
    
    if not all_vars:
        return []
    
    # Генерируем декартово произведение
    from itertools import product
    
    combinations = []
    keys = [key for key, _ in all_vars]
    value_lists = [values for _, values in all_vars]
    
    for combo in product(*value_lists):
        combination = dict(zip(keys, combo))
        combinations.append(combination)
    
    return combinations


def _format_questionnaire_for_llm(items: List[Dict[str, Any]]) -> List[str]:
    """
    Форматирует анкету в читаемый текст для отправки в LLM.
    
    Создает отдельные текстовые элементы:
    - Инструкция (если есть)
    - Каждый вопрос с вариантами ответов (отдельным элементом)
    
    Args:
        items: список словарей с информацией о вопросах
        
    Returns:
        Список строк, готовых для отправки в LLM
    """
    formatted_items = []
    
    # Собираем все инструкции со всех страниц
    instructions_parts = []
    seen_instructions = set()
    
    # Собираем информацию о treatment из первого элемента
    treatment_info_text = ""
    if items and 'treatment_info' in items[0]:
        treatment_info = items[0]['treatment_info']
        if treatment_info:
            treatment_parts = []
            for key, value in treatment_info.items():
                treatment_parts.append(f"{key}: {value}")
            treatment_info_text = "\n".join(treatment_parts)
    
    # Проходим по всем элементам и собираем инструкции и вопросы
    current_page = None
    page_instructions = []
    page_info_blocks = {}  # Словарь для хранения информационных блоков по страницам
    
    for item in items:
        # Если это новая страница, добавляем инструкции предыдущей страницы
        if current_page and current_page != item['page_name']:
            if page_instructions:
                instruction_text = "\n".join(page_instructions)
                if instruction_text and instruction_text not in seen_instructions:
                    seen_instructions.add(instruction_text)
                    instructions_parts.append(instruction_text)
                page_instructions = []
        
        current_page = item['page_name']
        
        # Собираем инструкции страницы
        # Важно: сохраняем инструкции для каждой страницы отдельно, чтобы добавить их перед вопросами этой страницы
        if item.get('page_instructions'):
            instruction = item['page_instructions'].strip()
            if instruction and instruction not in seen_instructions:
                # Очищаем от лишних пробелов, но сохраняем структуру (переносы строк)
                instruction = re.sub(r'[ \t]+', ' ', instruction)  # Только множественные пробелы/табы
                instruction = re.sub(r'\n\s*\n+', '\n\n', instruction)  # Множественные переносы -> двойной перенос
                if instruction not in seen_instructions:
                    seen_instructions.add(instruction)
                    page_instructions.append(instruction)
                    # Сохраняем информационный блок для этой страницы
                    if current_page not in page_info_blocks:
                        page_info_blocks[current_page] = []
                    page_info_blocks[current_page].append(instruction)
        
        # Добавляем вопросы
        if item.get('question_text') or item.get('field_name'):
            question_text = item.get('question_text') or item.get('field_name', '')
            if question_text:
                # Если это первый вопрос на странице и есть информационный блок для этой страницы,
                # добавляем его перед вопросом
                page_name = item.get('page_name')
                if page_name in page_info_blocks and page_info_blocks[page_name]:
                    # Добавляем информационный блок как отдельный элемент (только один раз)
                    info_text = "\n\n".join(page_info_blocks[page_name])
                    # Используем уникальный ключ для проверки, был ли уже добавлен этот блок
                    info_key = f"info_{page_name}"
                    if info_text and info_key not in seen_instructions:
                        formatted_items.append(f"ИНФОРМАЦИЯ (страница: {page_name}):\n{info_text}")
                        seen_instructions.add(info_key)
                    # Очищаем, чтобы не добавлять повторно
                    page_info_blocks[page_name] = []
                
                # Формируем текст вопроса
                question_parts = [f"ВОПРОС {item.get('question_number', '?')}: {question_text}"]
                
                # Добавляем варианты ответов, если есть
                if item.get('answer_options'):
                    options_list = "\n".join([f"  - {opt}" for opt in item['answer_options']])
                    question_parts.append(f"Варианты ответов:\n{options_list}")
                
                # Добавляем подсказку, если есть
                if item.get('help_text'):
                    question_parts.append(f"Подсказка: {item['help_text']}")
                
                formatted_items.append("\n".join(question_parts))
    
    # Добавляем инструкции последней страницы
    if page_instructions:
        instruction_text = "\n".join(page_instructions)
        if instruction_text and instruction_text not in seen_instructions:
            instructions_parts.append(instruction_text)
    
    # Формируем финальный список: сначала инструкции, потом вопросы
    result = []
    
    # Добавляем информацию о treatment, если есть
    if treatment_info_text:
        result.append(f"ИНФОРМАЦИЯ О TREATMENT:\n{treatment_info_text}")
    
    # Добавляем инструкции
    if instructions_parts:
        all_instructions = "\n\n".join(instructions_parts)
        result.append(f"ИНСТРУКЦИЯ:\n{all_instructions}")
    
    # Добавляем вопросы
    result.extend(formatted_items)
    
    return result


def _build_questionnaire_for_treatment_combo(
    treatment: Treatment,
    inspector: Optional['OTreeInspector'],
    treatment_combo: Dict[str, str],
    combo_number: int,
    total_combos: int
) -> List[Dict[str, Any]]:
    """
    Создает анкету для конкретной комбинации treatment.
    
    Args:
        treatment: Treatment объект
        inspector: OTreeInspector для извлечения текста
        treatment_combo: словарь с комбинацией treatment {'app_name:var_name': 'value'}
        combo_number: номер комбинации
        total_combos: всего комбинаций
    """
    questionnaire_items = []
    question_counter = 1
    
    for page_num, page in enumerate(treatment.visible_pages, 1):
        # Извлекаем текст страницы и инструкции
        # Сначала пытаемся использовать уже извлеченный текст
        page_text = page.template_text or ""
        
        # Если текст не был извлечен ранее, пытаемся извлечь из шаблона
        if not page_text and page.template_name and inspector:
            page_text = inspector.extract_template_text(page.app_name, page.template_name) or ""
        
        # Если все еще нет текста, пытаемся прочитать HTML напрямую
        if not page_text and page.template_name and inspector:
            try:
                app_path = inspector.project_path / page.app_name
                template_file = page.template_name.split('/')[-1]
                template_paths = [
                    app_path / template_file,  # Прямо в директории app
                    app_path / 'templates' / template_file,  # В templates
                    app_path / 'templates' / page.app_name / template_file,  # В templates/app_name
                    app_path / 'templates' / page.template_name,  # Полный путь из template_name
                    inspector.project_path / '_templates' / 'global' / template_file,  # Глобальный шаблон
                ]
                
                for template_path in template_paths:
                    if template_path.exists():
                        with open(template_path, 'r', encoding='utf-8') as f:
                            page_text = f.read()
                        break
            except Exception as e:
                warnings.warn(f"Failed to read template {page.template_name}: {e}")
        
        # Подставляем значения treatment в текст страницы
        if page_text and treatment_combo:
            # Ищем переменные treatment для этого приложения
            for key, value in treatment_combo.items():
                if ':' in key:
                    app_name_part, var_name = key.split(':', 1)
                    if app_name_part == page.app_name:
                        # Заменяем {{ var_name }} или {{ var_name|safe }} на значение
                        import re
                        # Ищем паттерны типа {{ donation_vignette|safe }} или {{ donation_vignette }}
                        pattern = rf'\{{\{{\s*{re.escape(var_name)}(?:\s*\|\s*safe)?\s*\}}\}}'
                        page_text = re.sub(pattern, value, page_text, flags=re.IGNORECASE)
        
        # Пытаемся извлечь инструкции из текста страницы
        # Инструкции обычно идут до {{ formfields }}
        page_instructions = ""
        if page_text:
            # Удаляем Jinja2 синтаксис для получения чистого текста
            import re
            
            # Сначала извлекаем текст из HTML, если это HTML
            if HAS_BS4 and '<' in page_text:
                try:
                    soup = BeautifulSoup(page_text, 'html.parser')
                    # Удаляем script и style
                    for script in soup(["script", "style"]):
                        script.decompose()
                    # Получаем текст
                    html_text = soup.get_text()
                except:
                    html_text = page_text
            else:
                html_text = page_text
            
            # Убираем Jinja2 синтаксис
            # Сначала убираем блоки extends
            clean_text = re.sub(r'\{\{\s*extends\s+[^\}]+\}\}', '', html_text, flags=re.IGNORECASE)
            # Убираем блоки {% block %} и {% endblock %}
            clean_text = re.sub(r'\{\%\s*block\s+\w+\s*\%\}', '', clean_text)
            clean_text = re.sub(r'\{\%\s*endblock\s*\%\}', '', clean_text)
            
            # Инструкции - это весь содержательный текст до первого упоминания formfields
            # Это включает заголовки, описания, введения и другой важный контент
            if 'formfields' in page_text.lower() or page.form_fields:
                # Берем текст до formfields как инструкции
                parts = re.split(r'\{\{\s*formfields?\s*\}\}', clean_text, flags=re.IGNORECASE)
                if len(parts) > 0:
                    instructions_text = parts[0]
                    # Очищаем от оставшихся Jinja2 конструкций
                    instructions_text = re.sub(r'\{\{.*?\}\}', '', instructions_text)
                    instructions_text = re.sub(r'\{\%.*?\%\}', '', instructions_text, flags=re.DOTALL)
                    # Убираем HTML теги, но сохраняем структуру текста
                    # Сначала заменяем некоторые теги на переносы строк для сохранения структуры
                    instructions_text = re.sub(r'<h[1-6][^>]*>', '\n', instructions_text, flags=re.IGNORECASE)
                    instructions_text = re.sub(r'</h[1-6]>', '\n', instructions_text, flags=re.IGNORECASE)
                    instructions_text = re.sub(r'<p[^>]*>', '\n', instructions_text, flags=re.IGNORECASE)
                    instructions_text = re.sub(r'</p>', '\n', instructions_text, flags=re.IGNORECASE)
                    instructions_text = re.sub(r'<br\s*/?>', '\n', instructions_text, flags=re.IGNORECASE)
                    # Теперь убираем остальные HTML теги
                    instructions_text = re.sub(r'<[^>]+>', '', instructions_text)
                    # Убираем лишние пробелы, но сохраняем переносы строк для читаемости
                    instructions_text = re.sub(r'[ \t]+', ' ', instructions_text)  # Множественные пробелы -> один
                    instructions_text = re.sub(r'\n\s*\n+', '\n\n', instructions_text)  # Множественные переносы -> двойной
                    page_instructions = instructions_text.strip()
            else:
                # Если нет formfields, очищаем весь текст
                clean_text = re.sub(r'\{\{.*?\}\}', '', clean_text)
                clean_text = re.sub(r'\{\%.*?\%\}', '', clean_text, flags=re.DOTALL)
                clean_text = re.sub(r'<[^>]+>', '', clean_text)
                clean_text = re.sub(r'\s+', ' ', clean_text)
                page_instructions = clean_text.strip()
        
        # Если нет инструкций, но есть template_text, используем его
        if not page_instructions and page_text:
            page_instructions = page_text
        
        # Обрабатываем каждый вопрос на странице
        if page.questions:
            for question in page.questions:
                # Если варианты ответов не были извлечены из модели, пытаемся извлечь из HTML
                answer_options = list(question.answer_options) if question.answer_options else []
                
                if not answer_options and page_text and question.field_name:
                    # Пытаемся извлечь варианты ответов из HTML шаблона
                    try:
                        if HAS_BS4 and '<' in page_text:
                            soup = BeautifulSoup(page_text, 'html.parser')
                            
                            # Ищем input или select с name или id, соответствующим field_name
                            field_inputs = soup.find_all(['input', 'select'], {
                                'name': question.field_name
                            })
                            
                            if not field_inputs:
                                # Пробуем найти по id
                                field_inputs = soup.find_all(['input', 'select'], {
                                    'id': f'id_{question.field_name}'
                                })
                            
                            for field_input in field_inputs:
                                if field_input.name == 'select':
                                    # Для select извлекаем все option
                                    options = field_input.find_all('option')
                                    for option in options:
                                        opt_text = option.get_text(strip=True)
                                        opt_value = option.get('value', opt_text)
                                        if opt_text and opt_text not in ['', '---------']:
                                            if opt_text not in answer_options:
                                                answer_options.append(opt_text)
                                elif field_input.get('type') in ['radio', 'checkbox']:
                                    # Для radio/checkbox ищем все элементы с тем же name
                                    field_name = field_input.get('name')
                                    if field_name:
                                        all_inputs = soup.find_all('input', {
                                            'name': field_name,
                                            'type': field_input.get('type')
                                        })
                                        for inp in all_inputs:
                                            # Пытаемся найти label
                                            inp_id = inp.get('id')
                                            if inp_id:
                                                label = soup.find('label', {'for': inp_id})
                                                if label:
                                                    label_text = label.get_text(strip=True)
                                                    if label_text and label_text not in answer_options:
                                                        answer_options.append(label_text)
                                            # Если label не найден, используем value
                                            if not inp_id or not soup.find('label', {'for': inp_id}):
                                                value = inp.get('value', '')
                                                if value and value not in answer_options:
                                                    answer_options.append(value)
                    except Exception as e:
                        warnings.warn(f"Failed to extract answer options from HTML for {question.field_name}: {e}")
                
                # Формируем полный контекст для LLM
                context_parts = []
                
                # Добавляем инструкции страницы (если есть и еще не добавлены)
                # Это включает описание компании и другой содержательный текст
                if page_instructions:
                    context_parts.append(f"Инструкция: {page_instructions}")
                
                # Добавляем текст вопроса
                question_text = question.question_text or question.field_name
                if question_text != question.field_name:
                    context_parts.append(f"Вопрос: {question_text}")
                else:
                    # Если question_text совпадает с field_name, значит текст не был извлечен
                    # Используем field_name, но это не идеально
                    context_parts.append(f"Вопрос: {question_text}")
                
                # Добавляем варианты ответов (если есть)
                if answer_options:
                    options_text = ", ".join(answer_options)
                    context_parts.append(f"Варианты ответов: {options_text}")
                
                # Добавляем подсказку (если есть)
                if question.help_text:
                    context_parts.append(f"Подсказка: {question.help_text}")
                
                full_context = "\n".join(context_parts)
                
                # Создаем элемент анкеты
                item = {
                    'page_name': page.class_name,
                    'page_number': page_num,
                    'page_instructions': page_instructions,
                    'page_text': page_text,
                    'question_number': question_counter,
                    'question_text': question_text,
                    'answer_options': answer_options,  # Используем извлеченные варианты
                    'field_name': question.field_name,
                    'field_type': question.field_type,
                    'is_required': question.is_required,
                    'help_text': question.help_text,
                    'full_context': full_context,
                    'app_name': page.app_name
                }
                
                # Информация о treatment будет добавлена в первый элемент анкеты
                # Здесь не добавляем, чтобы не дублировать
                
                questionnaire_items.append(item)
                question_counter += 1
        else:
            # Если на странице нет вопросов, но есть текст - создаем элемент для контекста
            if page_text or page_instructions:
                item = {
                    'page_name': page.class_name,
                    'page_number': page_num,
                    'page_instructions': page_instructions,
                    'page_text': page_text,
                    'question_number': None,  # Нет вопроса
                    'question_text': None,
                    'answer_options': [],
                    'field_name': None,
                    'field_type': None,
                    'is_required': False,
                    'help_text': None,
                    'full_context': page_instructions or page_text,
                    'app_name': page.app_name,
                    'is_info_page': True  # Информационная страница без вопросов
                }
                
                # Информация о treatment будет добавлена в первый элемент анкеты
                # Здесь не добавляем, чтобы не дублировать
                
                questionnaire_items.append(item)
    
    return questionnaire_items
