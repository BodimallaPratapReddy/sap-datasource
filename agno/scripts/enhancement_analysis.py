import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from .analysis import fetch_roosource, run_query_rest
from .utils import _rows_from_query_result, _strip_abap_comments, _valid_ident


SourceReader = Callable[[str, str], Awaitable[Optional[str]]]


ENHANCEMENT_INCLUDE_BY_TYPE = {
    "TRAN": "ZXRSAU01",
    "ATTR": "ZXRSAU02",
    "TEXT": "ZXRSAU03",
}


class EnhancementSourceEvidence(BaseModel):
    object: str
    object_type: str
    line_start: int
    line_end: int
    snippet: str
    discovery_path: List[str] = Field(default_factory=list)
    usage: str


class EnhancementTable(BaseModel):
    name: str
    role: str
    description: str = ""
    ddic_object_type: str = ""
    primary_key: List[str] = Field(default_factory=list)
    evidence: List[EnhancementSourceEvidence] = Field(default_factory=list)
    confidence: str


class EnhancedField(BaseModel):
    name: str
    source_table: Optional[str] = None
    source_field: Optional[str] = None
    evidence: EnhancementSourceEvidence
    confidence: str


class EnhancementReport(BaseModel):
    datasource: str
    objvers: str
    datasource_type: str = ""
    enhancement_include: str = ""
    datasource_branch_found: bool = False
    enhanced_fields: List[EnhancedField] = Field(default_factory=list)
    tables: List[EnhancementTable] = Field(default_factory=list)
    source_objects: List[str] = Field(default_factory=list)
    unresolved_symbols: List[dict] = Field(default_factory=list)
    manual_review: List[dict] = Field(default_factory=list)
    recursion_truncated: bool = False
    errors: List[dict] = Field(default_factory=list)


@dataclass
class SourceObject:
    name: str
    object_type: str
    source: str
    path: List[str]
    restrict_to_datasource: bool = False


def enhancement_include_for_type(datasource_type: str) -> Optional[str]:
    return ENHANCEMENT_INCLUDE_BY_TYPE.get(str(datasource_type or "").strip().upper())


def _line_span(source: str, start: int, end: int) -> Tuple[int, int]:
    return source.count("\n", 0, start) + 1, source.count("\n", 0, end) + 1


def _snippet(source: str, start: int, end: int, context_lines: int = 2) -> str:
    lines = source.splitlines()
    line_start, line_end = _line_span(source, start, end)
    first = max(0, line_start - context_lines - 1)
    last = min(len(lines), line_end + context_lines)
    return "\n".join(lines[first:last])


def _mask_outside_ranges(source: str, ranges: List[Tuple[int, int]]) -> str:
    chars = ["\n" if char == "\n" else " " for char in source]
    for start, end in ranges:
        chars[start:end] = source[start:end]
    return "".join(chars)


def _datasource_branch_ranges(source: str, datasource: str) -> List[Tuple[int, int]]:
    """Return executable ranges controlled by a literal datasource comparison.

    CASE blocks are handled precisely. IF/CHECK-style exits are conservatively bounded
    to their statement/block so unrelated datasource branches in the shared include are
    not promoted as evidence.
    """
    scan = _strip_abap_comments(source)
    escaped = re.escape(datasource)
    ranges: List[Tuple[int, int]] = []

    case_re = re.compile(r"\bCASE\b[\s\S]*?\bENDCASE\s*\.", re.IGNORECASE)
    when_re = re.compile(r"\bWHEN\s+([^.]*)\.", re.IGNORECASE)
    for case_match in case_re.finditer(scan):
        when_matches = list(when_re.finditer(case_match.group(0)))
        for index, when_match in enumerate(when_matches):
            if not re.search(rf"['`]\s*{escaped}\s*['`]", when_match.group(1), re.IGNORECASE):
                continue
            start = case_match.start() + when_match.end()
            end = (
                case_match.start() + when_matches[index + 1].start()
                if index + 1 < len(when_matches)
                else case_match.end()
            )
            ranges.append((start, end))

    if_re = re.compile(
        rf"\b(?:IF|ELSEIF)\b[^.]*['`]\s*{escaped}\s*['`][^.]*\.[\s\S]*?\b(?:ELSEIF|ELSE|ENDIF)\b",
        re.IGNORECASE,
    )
    ranges.extend((match.start(), match.end()) for match in if_re.finditer(scan))

    statement_re = re.compile(
        rf"\b(?:CHECK|IF|ELSEIF)\b[^.]*['`]\s*{escaped}\s*['`][^.]*\.",
        re.IGNORECASE,
    )
    ranges.extend((match.start(), match.end()) for match in statement_re.finditer(scan))

    standalone_when_re = re.compile(
        rf"\bWHEN\s+['`]\s*{escaped}\s*['`]\s*\.(?:[\s\S]*?)(?=\bWHEN\b|\Z)",
        re.IGNORECASE,
    )
    ranges.extend((match.start(), match.end()) for match in standalone_when_re.finditer(scan))

    merged: List[Tuple[int, int]] = []
    for start, end in sorted(set(ranges)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _source_references(source: str) -> List[Tuple[str, str, int, int]]:
    scan = _strip_abap_comments(source)
    references: List[Tuple[str, str, int, int]] = []
    patterns = (
        ("INCL", r"\bINCLUDE\s+([/A-Z0-9_]+)\s*\."),
        ("FUNC", r"\bCALL\s+FUNCTION\s+['`]([/A-Z0-9_]+)['`]"),
        ("PROG", r"\bPERFORM\s+\w+\s+IN\s+PROGRAM\s+([/A-Z0-9_]+)"),
        ("PROG", r"\bPERFORM\s+\w+\s*\(\s*([/A-Z0-9_]+)\s*\)"),
        ("CLAS", r"\b([/A-Z][A-Z0-9_/]+)\s*=>\s*\w+\s*\("),
    )
    for object_type, pattern in patterns:
        for match in re.finditer(pattern, scan, re.IGNORECASE):
            name = match.group(1) if match.lastindex else None
            if name:
                references.append((name.upper(), object_type, match.start(), match.end()))
    return references


def _local_perform_references(source: str) -> List[Tuple[str, int, int]]:
    scan = _strip_abap_comments(source)
    references = []
    for match in re.finditer(r"\bPERFORM\s+([A-Z0-9_]+)[^.]*\.", scan, re.IGNORECASE):
        if re.search(r"\bIN\s+PROGRAM\b", match.group(0), re.IGNORECASE):
            continue
        references.append((match.group(1).upper(), match.start(), match.end()))
    return references


def _extract_form_source(source: str, form_name: str) -> Optional[str]:
    match = re.search(
        rf"\bFORM\s+{re.escape(form_name)}\b[\s\S]*?\bENDFORM\s*\.",
        source,
        re.IGNORECASE,
    )
    return match.group(0) if match else None


def _table_occurrences(source: str) -> List[Tuple[str, str, int, int]]:
    scan = _strip_abap_comments(source)
    occurrences: List[Tuple[str, str, int, int]] = []
    patterns = (
        ("select", r"\bSELECT\b[\s\S]{0,500}?\bFROM\s+([/A-Z0-9_]+)"),
        ("join", r"\b(?:INNER\s+|LEFT(?:\s+OUTER)?\s+|RIGHT(?:\s+OUTER)?\s+)?JOIN\s+([/A-Z0-9_]+)"),
        ("update", r"\bUPDATE\s+([/A-Z0-9_]+)"),
        ("modify", r"\bMODIFY\s+([/A-Z0-9_]+)"),
        ("insert", r"\bINSERT\s+(?:INTO\s+)?([/A-Z0-9_]+)"),
        ("delete", r"\bDELETE\s+FROM\s+([/A-Z0-9_]+)"),
    )
    for usage, pattern in patterns:
        for match in re.finditer(pattern, scan, re.IGNORECASE):
            occurrences.append((match.group(1).upper(), usage, match.start(), match.end()))
    return occurrences


def _dynamic_sql_occurrences(source: str) -> List[Tuple[str, int, int]]:
    scan = _strip_abap_comments(source)
    patterns = (
        ("dynamic_from", r"\bFROM\s*\([^)]*\)"),
        ("native_sql", r"\bEXEC\s+SQL\b"),
    )
    return [
        (kind, match.start(), match.end())
        for kind, pattern in patterns
        for match in re.finditer(pattern, scan, re.IGNORECASE)
    ]


def _enhanced_field_occurrences(source: str) -> List[Tuple[str, Optional[str], Optional[str], int, int]]:
    scan = _strip_abap_comments(source)
    assignment = re.compile(
        r"(?:<[^>]+>|[A-Z][A-Z0-9_]*)-(ZZ?[A-Z0-9_]+)\s*=\s*"
        r"(?:(?:<[^>]+>|[A-Z][A-Z0-9_]*)-)?([A-Z][A-Z0-9_]*)?",
        re.IGNORECASE,
    )
    results = []
    for match in assignment.finditer(scan):
        source_field = match.group(2).upper() if match.group(2) else None
        rhs = scan[match.start():match.end()]
        source_table_match = re.search(r"=\s*(?:<)?([A-Z][A-Z0-9_]*)(?:>)?-", rhs, re.IGNORECASE)
        source_table = source_table_match.group(1).upper() if source_table_match else None
        results.append((match.group(1).upper(), source_table, source_field, match.start(), match.end()))
    return results


async def _ddic_metadata(table_name: str) -> Optional[dict]:
    dd02l = _rows_from_query_result(
        await run_query_rest(f"SELECT * FROM DD02L WHERE TABNAME = '{table_name}'")
    )
    active = [
        row for row in dd02l
        if str(row.get("TABNAME") or row.get("tabname") or "").strip().upper() == table_name
        and str(row.get("AS4LOCAL") or row.get("as4local") or "").strip().upper() == "A"
    ]
    if not active:
        return None
    object_type = str(active[0].get("TABCLASS") or active[0].get("tabclass") or "").strip().upper()
    if object_type not in {"TRANSP", "CLUSTER", "POOL"}:
        return None

    dd02t = _rows_from_query_result(
        await run_query_rest(f"SELECT * FROM DD02T WHERE TABNAME = '{table_name}'")
    )
    english = next((
        row for row in dd02t
        if str(row.get("DDLANGUAGE") or row.get("ddlanguage") or "").strip().upper() == "E"
    ), dd02t[0] if dd02t else {})
    dd03l = _rows_from_query_result(
        await run_query_rest(f"SELECT * FROM DD03L WHERE TABNAME = '{table_name}'")
    )
    return {
        "description": str(english.get("DDTEXT") or english.get("ddtext") or ""),
        "object_type": object_type,
        "primary_key": [
            str(row.get("FIELDNAME") or row.get("fieldname"))
            for row in dd03l
            if str(row.get("KEYFLAG") or row.get("keyflag") or "").strip().upper() == "X"
        ],
    }


async def analyze_datasource_enhancement(
    datasource: str,
    source_reader: SourceReader,
    objvers: str = "A",
    max_depth: int = 8,
) -> EnhancementReport:
    datasource = str(datasource or "").strip().upper()
    objvers = str(objvers or "A").strip().upper()
    report = EnhancementReport(datasource=datasource, objvers=objvers)
    if not _valid_ident(datasource) or not _valid_ident(objvers):
        report.errors.append({"stage": "INPUT", "type": "INVALID_IDENTIFIER"})
        return report

    roosource = await fetch_roosource(datasource, objvers)
    if not roosource:
        report.errors.append({
            "stage": "ROOSOURCE_LOOKUP",
            "object": datasource,
            "type": "NOT_FOUND",
        })
        return report

    row = roosource[0]
    datasource_type = str(row.get("TYPE") or row.get("type") or "").strip().upper()
    include_name = enhancement_include_for_type(datasource_type)
    report.datasource_type = datasource_type
    if not include_name:
        report.errors.append({
            "stage": "ENHANCEMENT_ROUTING",
            "object": datasource,
            "type": "UNSUPPORTED_DATASOURCE_TYPE",
            "value": datasource_type,
        })
        return report
    report.enhancement_include = include_name

    initial_source = await source_reader(include_name, "INCL")
    if not initial_source:
        report.errors.append({
            "stage": "SOURCE_RETRIEVAL",
            "object": include_name,
            "object_type": "INCL",
            "type": "NOT_RETRIEVED",
        })
        return report

    ranges = _datasource_branch_ranges(initial_source, datasource)
    report.datasource_branch_found = bool(ranges)
    if not ranges:
        report.manual_review.append({
            "object": include_name,
            "reason": "No literal datasource-specific CASE/IF/CHECK branch was found.",
        })
        return report

    queue = [SourceObject(
        name=include_name,
        object_type="INCL",
        source=initial_source,
        path=[include_name],
        restrict_to_datasource=True,
    )]
    visited: Set[Tuple[str, str]] = set()
    table_evidence: Dict[str, List[EnhancementSourceEvidence]] = {}
    enhanced_fields: List[EnhancedField] = []

    while queue:
        current = queue.pop(0)
        key = (current.name, current.object_type)
        if key in visited:
            continue
        depth = len(current.path) - 1
        if depth >= max_depth:
            report.recursion_truncated = True
            continue
        visited.add(key)
        report.source_objects.append(f"{current.object_type}:{current.name}")

        analysis_source = current.source
        if current.restrict_to_datasource:
            analysis_source = _mask_outside_ranges(
                current.source,
                _datasource_branch_ranges(current.source, datasource),
            )

        for name, usage, start, end in _table_occurrences(analysis_source):
            if not _valid_ident(name):
                continue
            line_start, line_end = _line_span(current.source, start, end)
            evidence = EnhancementSourceEvidence(
                object=current.name,
                object_type=current.object_type,
                line_start=line_start,
                line_end=line_end,
                snippet=_snippet(current.source, start, end),
                discovery_path=current.path,
                usage=usage,
            )
            table_evidence.setdefault(name, []).append(evidence)

        for kind, start, end in _dynamic_sql_occurrences(analysis_source):
            line_start, line_end = _line_span(current.source, start, end)
            report.manual_review.append({
                "object": current.name,
                "kind": kind,
                "line_start": line_start,
                "line_end": line_end,
                "snippet": _snippet(current.source, start, end),
            })

        for field_name, source_table, source_field, start, end in _enhanced_field_occurrences(analysis_source):
            line_start, line_end = _line_span(current.source, start, end)
            enhanced_fields.append(EnhancedField(
                name=field_name,
                source_table=source_table,
                source_field=source_field,
                evidence=EnhancementSourceEvidence(
                    object=current.name,
                    object_type=current.object_type,
                    line_start=line_start,
                    line_end=line_end,
                    snippet=_snippet(current.source, start, end),
                    discovery_path=current.path,
                    usage="field_assignment",
                ),
                confidence="Medium" if source_table and source_field else "Low",
            ))

        for form_name, start, end in _local_perform_references(analysis_source):
            form_source = _extract_form_source(current.source, form_name)
            if form_source:
                queue.append(SourceObject(
                    name=f"{current.name}#FORM:{form_name}",
                    object_type="FORM",
                    source=form_source,
                    path=current.path + [f"FORM:{form_name}"],
                ))
            else:
                line_start, _ = _line_span(current.source, start, end)
                report.unresolved_symbols.append({
                    "name": form_name,
                    "object_type": "FORM",
                    "referenced_by": current.name,
                    "line": line_start,
                    "discovery_path": current.path,
                })

        for name, object_type, start, end in _source_references(analysis_source):
            ref_key = (name, object_type)
            if ref_key in visited:
                continue
            child_source = await source_reader(name, object_type)
            if child_source:
                queue.append(SourceObject(
                    name=name,
                    object_type=object_type,
                    source=child_source,
                    path=current.path + [name],
                ))
            else:
                line_start, _ = _line_span(current.source, start, end)
                report.unresolved_symbols.append({
                    "name": name,
                    "object_type": object_type,
                    "referenced_by": current.name,
                    "line": line_start,
                    "discovery_path": current.path,
                })

    unique_fields: Dict[Tuple[str, Optional[str], Optional[str], str, str], EnhancedField] = {}
    for field in enhanced_fields:
        field_key = (
            field.name,
            field.source_table,
            field.source_field,
            field.evidence.object,
            field.evidence.snippet,
        )
        unique_fields.setdefault(field_key, field)
    report.enhanced_fields = list(unique_fields.values())

    unique_unresolved = {}
    for symbol in report.unresolved_symbols:
        symbol_key = (
            symbol.get("name"),
            symbol.get("object_type"),
            symbol.get("referenced_by"),
        )
        unique_unresolved.setdefault(symbol_key, symbol)
    report.unresolved_symbols = list(unique_unresolved.values())

    for table_name, evidence in sorted(table_evidence.items()):
        unique_evidence = {}
        for item in evidence:
            evidence_key = (item.object, item.usage, item.snippet)
            unique_evidence.setdefault(evidence_key, item)
        evidence = list(unique_evidence.values())
        metadata = await _ddic_metadata(table_name)
        if metadata is None:
            report.manual_review.append({
                "object": table_name,
                "reason": "Source reference was not confirmed as an active physical table in DD02L.",
                "evidence": [item.model_dump() for item in evidence],
            })
            continue
        read_usages = {item.usage for item in evidence} & {"select", "join"}
        report.tables.append(EnhancementTable(
            name=table_name,
            role="Core" if read_usages else "Support",
            description=metadata["description"],
            ddic_object_type=metadata["object_type"],
            primary_key=metadata["primary_key"],
            evidence=evidence,
            confidence="High",
        ))

    return report


def report_as_json(report: EnhancementReport) -> str:
    return json.dumps(report.model_dump(), indent=2, default=str)