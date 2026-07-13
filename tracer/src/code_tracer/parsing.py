"""Static extraction of symbols and calls from source via tree-sitter."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from tree_sitter_language_pack import get_language, get_parser

from .languages import KIND_MAP, LangSpec, spec_for_lang

_LANG_CACHE: dict[str, object] = {}
_PARSER_CACHE: dict[str, object] = {}
_QUERY_CACHE: dict[str, object] = {}


def _get(lang: str):
    if lang not in _PARSER_CACHE:
        _LANG_CACHE[lang] = get_language(lang)
        _PARSER_CACHE[lang] = get_parser(lang)
    return _LANG_CACHE[lang], _PARSER_CACHE[lang]


def _query(lang, key: str, source: str):
    ck = f"{id(lang)}:{key}"
    if ck not in _QUERY_CACHE:
        _QUERY_CACHE[ck] = lang.query(source)
    return _QUERY_CACHE[ck]


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str
    start_line: int  # 1-based
    end_line: int
    signature: str
    docstring: str
    comments: str
    identifiers: str
    body_text: str
    body_hash: str
    calls: list["Call"] = field(default_factory=list)


@dataclass
class Call:
    name: str
    line: int  # 1-based


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _node_text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _leading_comment(def_node, src: bytes) -> str:
    """Comment block directly above a definition (same or previous lines)."""
    prev = def_node.prev_sibling
    comments: list[str] = []
    while prev is not None and prev.type in {
        "comment",
        "line_comment",
        "block_comment",
        "expression_statement",  # python string-only stmt sometimes
    }:
        txt = _node_text(prev, src).strip()
        if prev.type == "comment" or prev.type.endswith("comment"):
            comments.insert(0, txt)
            prev = prev.prev_sibling
        else:
            break
    return "\n".join(comments)


def _python_docstring(def_node, src: bytes) -> str:
    body = def_node.child_by_field_name("body")
    if body is None or body.named_child_count == 0:
        return ""
    first = body.named_child(0)
    if first.type == "expression_statement" and first.named_child_count:
        s = first.named_child(0)
        if s.type == "string":
            return _node_text(s, src).strip("\"'` \n")
    return ""


def _signature(def_node, src: bytes, kind: str) -> str:
    """First line of the definition, trimmed, minus body."""
    name_node = def_node.child_by_field_name("name")
    params = def_node.child_by_field_name("parameters") or def_node.child_by_field_name(
        "parameter_list"
    )
    if name_node is not None:
        base = _node_text(name_node, src)
        if params is not None:
            base += _node_text(params, src)
        return base.replace("\n", " ").strip()
    line = _node_text(def_node, src).splitlines()[0]
    return line.strip().rstrip("{").strip()


_IDENT_TYPES = {
    "identifier",
    "property_identifier",
    "field_identifier",
    "type_identifier",
    "name",
}


def _collect_identifiers(node, src: bytes, out: list[str], limit: int = 400) -> None:
    stack = [node]
    while stack and len(out) < limit:
        n = stack.pop()
        if n.type in _IDENT_TYPES:
            out.append(_node_text(n, src))
        for c in n.children:
            stack.append(c)


def _enclosing_class(def_node, src: bytes) -> Optional[str]:
    parent = def_node.parent
    while parent is not None:
        if parent.type in {
            "class_definition",
            "class_declaration",
            "abstract_class_declaration",
        }:
            nm = parent.child_by_field_name("name")
            if nm is not None:
                return _node_text(nm, src)
        parent = parent.parent
    return None


def parse_source(lang: str, code: str) -> list[Symbol]:
    spec: LangSpec | None = spec_for_lang(lang)
    if spec is None:
        return []
    src = code.encode("utf-8", "replace")
    ts_lang, parser = _get(lang)
    tree = parser.parse(src)
    root = tree.root_node

    defs_q = _query(ts_lang, spec.name + ":defs", spec.defs_query)
    calls_q = _query(ts_lang, spec.name + ":calls", spec.calls_query)

    symbols: list[Symbol] = []
    # Each match groups @def with its @name.
    def_nodes: list[tuple[object, object]] = []
    for _pat, caps in defs_q.matches(root):
        dn = caps.get("def")
        nn = caps.get("name")
        if not dn or not nn:
            continue
        def_nodes.append((dn[0], nn[0]))

    # sort by start byte for stable qualname nesting
    def_nodes.sort(key=lambda t: t[0].start_byte)

    for def_node, name_node in def_nodes:
        name = _node_text(name_node, src)
        kind = KIND_MAP.get(def_node.type, "function")
        cls = _enclosing_class(def_node, src)
        qualname = f"{cls}.{name}" if cls and kind != "class" else name

        docstring = ""
        if lang == "python":
            docstring = _python_docstring(def_node, src)
        comments = _leading_comment(def_node, src)
        signature = _signature(def_node, src, kind)

        idents: list[str] = []
        _collect_identifiers(def_node, src, idents)
        body_text = _node_text(def_node, src)

        sym = Symbol(
            name=name,
            qualname=qualname,
            kind=kind,
            start_line=def_node.start_point[0] + 1,
            end_line=def_node.end_point[0] + 1,
            signature=signature,
            docstring=docstring,
            comments=comments,
            identifiers=" ".join(dict.fromkeys(idents)),
            body_text=body_text,
            body_hash=_sha(body_text),
        )

        # calls whose position falls inside this def's body but NOT inside a
        # nested def (so calls attach to their nearest enclosing symbol).
        symbols.append(sym)

    # Assign calls to the innermost enclosing symbol.
    call_hits: list[Call] = []
    for _pat, caps in calls_q.matches(root):
        cn = caps.get("callee")
        if not cn:
            continue
        node = cn[0]
        call_hits.append((_node_text(node, src), node.start_byte, node.start_point[0] + 1))

    # Build (start,end,index) sorted by size for innermost match.
    ranges = [
        (s.start_line, s.end_line, i, def_nodes[i][0].start_byte, def_nodes[i][0].end_byte)
        for i, s in enumerate(symbols)
    ]
    for cname, cbyte, cline in call_hits:
        best = None
        best_span = None
        for (sl, el, i, sb, eb) in ranges:
            if sb <= cbyte <= eb:
                span = eb - sb
                if best_span is None or span < best_span:
                    best_span = span
                    best = i
        if best is not None:
            symbols[best].calls.append(Call(name=cname, line=cline))

    return symbols
