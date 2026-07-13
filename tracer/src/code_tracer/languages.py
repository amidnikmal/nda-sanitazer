"""Language registry: extension mapping and tree-sitter queries.

Definitions query captures ``@def`` (the whole definition node) and ``@name``
(its identifier). Calls query captures ``@callee`` (the called name node).
Kind (function/method/class) is derived from the def node's grammar type.
"""

from __future__ import annotations

from dataclasses import dataclass

# file extension -> tree-sitter-language-pack language name
EXT_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".php": "php",
    ".java": "java",
}

# def node grammar-type -> logical kind
KIND_MAP = {
    "function_definition": "function",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "method_definition": "method",
    "method_declaration": "method",
    "constructor_declaration": "method",
    "class_definition": "class",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "class",
    "enum_declaration": "class",
    "trait_declaration": "class",
    "type_spec": "type",
    "type_declaration": "type",
    "arrow_var": "function",
}

_PY_DEFS = """
(function_definition name: (identifier) @name) @def
(class_definition name: (identifier) @name) @def
"""
_PY_CALLS = """
(call function: (identifier) @callee)
(call function: (attribute attribute: (identifier) @callee))
"""

_JS_DEFS = """
(function_declaration name: (identifier) @name) @def
(generator_function_declaration name: (identifier) @name) @def
(method_definition name: (property_identifier) @name) @def
(class_declaration name: (identifier) @name) @def
(variable_declarator name: (identifier) @name value: (arrow_function)) @def
(variable_declarator name: (identifier) @name value: (function_expression)) @def
"""
_JS_CALLS = """
(call_expression function: (identifier) @callee)
(call_expression function: (member_expression property: (property_identifier) @callee))
(new_expression constructor: (identifier) @callee)
"""

# TS reuses most JS patterns but classes/interfaces use (type_identifier).
_TS_DEFS = """
(function_declaration name: (identifier) @name) @def
(generator_function_declaration name: (identifier) @name) @def
(method_definition name: (property_identifier) @name) @def
(class_declaration name: (type_identifier) @name) @def
(abstract_class_declaration name: (type_identifier) @name) @def
(variable_declarator name: (identifier) @name value: (arrow_function)) @def
(variable_declarator name: (identifier) @name value: (function_expression)) @def
(function_signature name: (identifier) @name) @def
(abstract_method_signature name: (property_identifier) @name) @def
(interface_declaration name: (type_identifier) @name) @def
"""
_TS_CALLS = _JS_CALLS

_GO_DEFS = """
(function_declaration name: (identifier) @name) @def
(method_declaration name: (field_identifier) @name) @def
(type_declaration (type_spec name: (type_identifier) @name)) @def
"""
_GO_CALLS = """
(call_expression function: (identifier) @callee)
(call_expression function: (selector_expression field: (field_identifier) @callee))
"""

_PHP_DEFS = """
(function_definition name: (name) @name) @def
(method_declaration name: (name) @name) @def
(class_declaration name: (name) @name) @def
(interface_declaration name: (name) @name) @def
(trait_declaration name: (name) @name) @def
"""
_PHP_CALLS = """
(function_call_expression function: (name) @callee)
(member_call_expression name: (name) @callee)
(scoped_call_expression name: (name) @callee)
(object_creation_expression (name) @callee)
"""

_JAVA_DEFS = """
(method_declaration name: (identifier) @name) @def
(constructor_declaration name: (identifier) @name) @def
(class_declaration name: (identifier) @name) @def
(interface_declaration name: (identifier) @name) @def
(enum_declaration name: (identifier) @name) @def
"""
_JAVA_CALLS = """
(method_invocation name: (identifier) @callee)
(object_creation_expression type: (type_identifier) @callee)
"""


@dataclass
class LangSpec:
    name: str
    defs_query: str
    calls_query: str


_SPECS: dict[str, LangSpec] = {
    "python": LangSpec("python", _PY_DEFS, _PY_CALLS),
    "javascript": LangSpec("javascript", _JS_DEFS, _JS_CALLS),
    "typescript": LangSpec("typescript", _TS_DEFS, _TS_CALLS),
    "tsx": LangSpec("tsx", _TS_DEFS, _TS_CALLS),
    "go": LangSpec("go", _GO_DEFS, _GO_CALLS),
    "php": LangSpec("php", _PHP_DEFS, _PHP_CALLS),
    "java": LangSpec("java", _JAVA_DEFS, _JAVA_CALLS),
}

SUPPORTED_LANGS = set(_SPECS)


def lang_for_path(path: str) -> str | None:
    import os

    _, ext = os.path.splitext(path)
    return EXT_LANG.get(ext.lower())


def spec_for_lang(lang: str) -> LangSpec | None:
    return _SPECS.get(lang)
