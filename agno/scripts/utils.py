import re
from typing import List, Optional, Tuple

# ABAP keywords that end statements with a bare 'WORD.' and must never be treated as macro calls
_ABAP_KEYWORDS_EXCLUDE = {
    'ENDFORM', 'ENDIF', 'ENDLOOP', 'ENDCASE', 'ENDMETHOD', 'ENDFUNCTION', 'ENDCLASS', 'ENDMODULE',
    'ENDWHILE', 'ENDAT', 'ENDSELECT', 'ENDEXEC', 'ENDCATCH', 'ENDDO', 'ENDPROVIDE', 'ENDINTERFACE',
    'CONTINUE', 'EXIT', 'CHECK', 'CLEAR', 'CALL', 'CASE', 'CATCH', 'CLASS', 'CONSTANTS', 'CREATE',
    'CONCATENATE', 'CONDENSE', 'CONVERT', 'COLLECT', 'COMMIT', 'COMPUTE', 'DELETE', 'DESCRIBE',
    'DIVIDE', 'ELSE', 'ELSEIF', 'EXPORT', 'FIELD', 'FORM', 'FREE', 'IMPORT', 'INCLUDE',
    'INITIALIZATION', 'INSERT', 'LOOP', 'MESSAGE', 'METHOD', 'MODIFY', 'MODULE', 'MOVE',
    'MULTIPLY', 'PERFORM', 'RAISE', 'READ', 'REFRESH', 'REPLACE', 'RETURN', 'ROLLBACK', 'SEARCH',
    'SELECT', 'SET', 'SHIFT', 'SORT', 'SPLIT', 'SUBTRACT', 'TABLES', 'TRANSLATE', 'TRY', 'TYPES',
    'UNPACK', 'UPDATE', 'WHILE', 'WRITE', 'DATA', 'PARAMETERS', 'FUNCTION', 'PROGRAM', 'REPORT',
    'INTERFACE', 'PUBLIC', 'PRIVATE', 'PROTECTED', 'VALUE', 'OPTIONAL', 'DEFAULT', 'CHANGING',
    'EXPORTING', 'IMPORTING', 'RETURNING', 'RAISING', 'EXCEPTIONS',
}


def extract_datasource_from_prompt(text: str) -> Optional[str]:
    """Find a SAP datasource/extractor name mentioned in free-text, e.g. 'datasource 0FI_GL_4'."""
    m = re.search(r"datasources?\s*[:\-]?\s*([0-9A-Za-z_]{3,30})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(2LIS_[0-9A-Z_]+|0[A-Z0-9_]{3,}|Z[A-Z0-9_]{3,})\b", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def extract_objvers_from_prompt(text: str) -> Optional[str]:
    m = re.search(r"objvers\s*[:\-]?\s*([A-Za-z])\b", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _rows_from_query_result(res):
    """Normalize runQuery's {status, result:{columns, values}} shape into a list of row dicts."""
    if res is None:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        result = res.get('result')
        if isinstance(result, dict) and isinstance(result.get('values'), list):
            return result['values']
        if isinstance(res.get('values'), list):
            return res['values']
        if isinstance(res.get('rows'), list):
            return res['rows']
        return [res]
    return []


_SIMPLE_SELECT_RE = re.compile(
    r"^SELECT\s+(?P<cols>\*|[\w,\s]+)\s+FROM\s+(?P<table>\w+)\s+WHERE\s+(?P<where>.+)$",
    re.IGNORECASE,
)
_SIMPLE_COND_RE = re.compile(r"(?P<field>\w+)\s*=\s*'(?P<value>[^']*)'")


def _sql_to_rest_query(sql: str):
    """Translate this script's own simple 'SELECT cols FROM T WHERE F=\'V\' [AND ...]' calls into
    the (table, output_fields, filters) shape the SAP REST table-read fallback API expects.
    Returns None if the SQL doesn't match this script's own generated pattern.
    """
    m = _SIMPLE_SELECT_RE.match(sql.strip())
    if not m:
        return None
    table = m.group('table').strip().upper()
    if not _valid_ident(table):
        return None
    cols_raw = m.group('cols').strip()
    output_fields = None if cols_raw == '*' else [c.strip().upper() for c in cols_raw.split(',') if c.strip()]
    filters = [
        {'fields': [cm.group('field').strip().upper()], 'operator': 'EQ', 'value': cm.group('value')}
        for cm in _SIMPLE_COND_RE.finditer(m.group('where'))
    ]
    return table, output_fields, filters


def clean_json_response(text: str) -> str:
    """Strip markdown code fences/wrappers the LLM may add around a JSON payload."""
    if not isinstance(text, str):
        return text
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"^<.*?>", "", s)
    return s.strip()


_IDENT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_]*$')


def _valid_ident(name) -> bool:
    """True when name is safe to interpolate as a bare SQL identifier/literal (defense in depth).

    SAP datasources/tables commonly start with a digit (e.g. 0FI_GL_4, 2LIS_02_ITM), so only
    quote/whitespace/injection-breaking characters are rejected here, not a leading-letter rule.
    """
    return isinstance(name, str) and len(name) <= 30 and bool(_IDENT_RE.match(name))


def _strip_abap_comments(source_text: str) -> str:
    """Blank out ABAP comment lines/spans (leading '*' or inline '"') so table/keyword regexes
    never mistake prose in comments (e.g. a German 'Join mit BKPF' remark) for real code.

    Replaces comment characters with spaces rather than removing them, so line numbers and
    character offsets stay identical to the original source for snippet/line-number extraction.
    """
    out_lines = []
    for line in source_text.split('\n'):
        if line.lstrip().startswith('*'):
            out_lines.append(' ' * len(line))
            continue
        in_string = False
        comment_at = None
        for i, ch in enumerate(line):
            if ch == "'":
                in_string = not in_string
            elif ch == '"' and not in_string:
                comment_at = i
                break
        if comment_at is not None:
            line = line[:comment_at] + ' ' * (len(line) - comment_at)
        out_lines.append(line)
    return '\n'.join(out_lines)


def _extract_comment_hints(source_text: str):
    """Find ALL-CAPS identifiers mentioned only inside ABAP comments (e.g. 'Join mit BKPF').

    Comments often name the real table a macro/join affects even with no executable SQL nearby.
    These are returned as (name, comment_text) hints for manual review only — never as confirmed
    table evidence, since a comment can be stale, wrong, or written in a different language.
    """
    hints = []
    for line in source_text.split('\n'):
        stripped = line.strip()
        comment_text = None
        if stripped.startswith('*'):
            comment_text = stripped.lstrip('*').strip()
        else:
            in_string = False
            comment_at = None
            for i, ch in enumerate(line):
                if ch == "'":
                    in_string = not in_string
                elif ch == '"' and not in_string:
                    comment_at = i
                    break
            if comment_at is not None:
                comment_text = line[comment_at + 1:].strip()
        if not comment_text:
            continue
        for tok in re.finditer(r'\b([A-Z][A-Z0-9]{2,9})\b', comment_text):
            name = tok.group(1)
            if name in _ABAP_KEYWORDS_EXCLUDE:
                continue
            hints.append((name, comment_text))
    return hints


def detect_source_symbols(source_text: str):
    """Scan ABAP source text for INCLUDE statements, PERFORM targets, and macro invocations.

    Returns dict with keys 'includes', 'forms', 'macros'. Macro invocations are only
    reported when the name is either DEFINE'd locally or a well-known BW extractor macro,
    to avoid treating arbitrary 'WORD.' statements (keywords, table names, etc.) as macros.
    """
    source_text = _strip_abap_comments(source_text)
    includes = []
    forms = []
    macros = []
    for m in re.finditer(r"INCLUDE\s+(\w+)", source_text, flags=re.IGNORECASE):
        name = m.group(1).strip()
        if name not in includes:
            includes.append(name)
    for m in re.finditer(r"PERFORM\s+(\w+)", source_text, flags=re.IGNORECASE):
        name = m.group(1).strip()
        if name not in forms:
            forms.append(name)
    defined_macros = set()
    for m in re.finditer(r"DEFINE\s+(\w+)", source_text, flags=re.IGNORECASE):
        defined_macros.add(m.group(1).strip().upper())
    known_extractor_macros = {'GET_DELTA_DATA', 'GENERAL_DELTA_INITIALIZATION', 'DECLARE_GENERAL', 'APPEND_R_TABLE'}
    # A macro invocation is a bare 'NAME.' statement occupying its own line
    for m in re.finditer(r"^\s*([A-Z][A-Z0-9_]{3,})\s*\.\s*$", source_text, flags=re.MULTILINE):
        name = m.group(1).strip()
        if name in _ABAP_KEYWORDS_EXCLUDE:
            continue
        if name in defined_macros or name in known_extractor_macros:
            if name not in macros:
                macros.append(name)
    return {'includes': includes, 'forms': forms, 'macros': macros}
