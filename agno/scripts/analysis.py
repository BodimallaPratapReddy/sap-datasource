import logging
import re
from typing import Any, Dict, List
from os import environ

from .utils import _sql_to_rest_query, _rows_from_query_result, _valid_ident, _strip_abap_comments, _extract_comment_hints, detect_source_symbols
from .rest_api import fetch_table_data_rest, fetch_function_source_rest, fetch_infoset_rest


async def run_query_rest(sql: str):
    parsed = _sql_to_rest_query(sql)
    if not parsed:
        return None
    table, output_fields, filters = parsed
    return await fetch_table_data_rest(table, filters=filters, output_fields=output_fields)


async def run_query_rest_all(sql: str, page_size: int = 100):
    parsed = _sql_to_rest_query(sql)
    if not parsed:
        return None
    table, output_fields, filters = parsed
    rows = []
    skip = 0
    previous_page = None
    while True:
        page = await fetch_table_data_rest(
            table,
            filters=filters,
            output_fields=output_fields,
            top=page_size,
            skip=skip,
        )
        if page is None:
            return None
        if page == previous_page:
            logging.getLogger('sap_analysis').warning(
                "Pagination stopped for %s because the REST endpoint repeated a page at skip=%s.",
                table,
                skip,
            )
            return rows
        rows.extend(page)
        if len(page) < page_size:
            return rows
        previous_page = page
        skip += page_size


async def resolve_infoset_query(datasource: str, extractor: str) -> Dict[str, Any]:
    """Resolve an EXMETHOD Q DataSource through SAP Query and InfoSet catalogs."""
    identifiers = {
        str(value).strip().upper()
        for value in (datasource, extractor)
        if value and _valid_ident(str(value).strip())
    }
    evidence = {
        'AQGQCAT': [],
        'AQLQCAT': [],
        'IQQCATDB': [],
        'IQQCATDBX': [],
        'IQICATDB': [],
        'IQICATDBX': [],
    }

    for catalog in ('AQGQCAT', 'AQLQCAT'):
        for identifier in identifiers:
            rows = _rows_from_query_result(
                await run_query_rest(f"SELECT * FROM {catalog} WHERE QNUM = '{identifier}'")
            )
            evidence[catalog].extend(
                row for row in rows
                if str(row.get('QNUM') or row.get('qnum') or '').strip().upper() == identifier
            )

    for identifier in identifiers:
        for field_name in ('QNAME', 'OLDQNAME'):
            rows = _rows_from_query_result(
                await run_query_rest(
                    f"SELECT * FROM IQQCATDB WHERE {field_name} = '{identifier}'"
                )
            )
            evidence['IQQCATDB'].extend(
                row for row in rows
                if str(row.get(field_name) or row.get(field_name.lower()) or '').strip().upper() == identifier
            )

    infosets = {
        str(row.get(field_name) or row.get(field_name.lower()) or '').strip().upper()
        for catalog in ('AQGQCAT', 'AQLQCAT')
        for row in evidence[catalog]
        for field_name in ('CLAS',)
        if row.get(field_name) or row.get(field_name.lower())
    }
    infosets.update(
        str(row.get(field_name) or row.get(field_name.lower()) or '').strip().upper()
        for row in evidence['IQQCATDB']
        for field_name in ('ISNAME', 'OLDISNAME')
        if row.get(field_name) or row.get(field_name.lower())
    )

    for infoset in infosets:
        rows = _rows_from_query_result(
            await run_query_rest(f"SELECT * FROM IQICATDB WHERE ISNAME = '{infoset}'")
        )
        evidence['IQICATDB'].extend(
            row for row in rows
            if str(row.get('ISNAME') or row.get('isname') or '').strip().upper() == infoset
        )

    infoset_api_results = []
    infoset_api_candidates = set(infosets)
    extractor_name = str(extractor or '').strip().upper()
    if _valid_ident(extractor_name):
        infoset_api_candidates.add(extractor_name)
    for infoset in sorted(infoset_api_candidates):
        result = await fetch_infoset_rest(infoset)
        if not result or not isinstance(result.get('tables'), list):
            continue
        infoset_api_results.append(result)
        resolved_infoset = str(result.get('infoset') or infoset).strip().upper()
        if _valid_ident(resolved_infoset):
            infosets.add(resolved_infoset)
    evidence['Z_INFOSET'] = infoset_api_results

    query_names = identifiers | {
        str(row.get('QNAME') or row.get('qname') or '').strip().upper()
        for row in evidence['IQQCATDB']
        if row.get('QNAME') or row.get('qname')
    }
    for query_name in query_names:
        rows = _rows_from_query_result(
            await run_query_rest(f"SELECT * FROM IQQCATDBX WHERE QNAME = '{query_name}'")
        )
        evidence['IQQCATDBX'].extend(
            row for row in rows
            if str(row.get('QNAME') or row.get('qname') or '').strip().upper() == query_name
        )
    for infoset in infosets:
        rows = _rows_from_query_result(
            await run_query_rest(f"SELECT * FROM IQICATDBX WHERE ISNAME = '{infoset}'")
        )
        evidence['IQICATDBX'].extend(
            row for row in rows
            if str(row.get('ISNAME') or row.get('isname') or '').strip().upper() == infoset
        )

    query_rows = evidence['AQGQCAT'] or evidence['AQLQCAT'] or evidence['IQQCATDB']
    query_row = query_rows[0] if query_rows else {}
    infoset_rows = evidence['IQICATDB']
    user_group = str(
        query_row.get('NUM') or query_row.get('num')
        or query_row.get('OLDUNAME') or query_row.get('olduname') or ''
    ).strip()
    logical_databases = sorted({
        str(row.get('LDB') or row.get('ldb') or '').strip().upper()
        for row in infoset_rows
        if row.get('LDB') or row.get('ldb')
    })
    base_tables = sorted({
        str(row.get('TAB') or row.get('tab') or '').strip().upper()
        for row in infoset_rows
        if row.get('TAB') or row.get('tab')
    })
    infoset_types = {
        str(row.get('ITYPE') or row.get('itype') or '').strip().upper()
        for row in infoset_rows
        if row.get('ITYPE') or row.get('itype')
    }
    source_type = 'OTHER'
    api_table_rows = [
        row
        for result in infoset_api_results
        for row in result.get('tables', [])
        if isinstance(row, dict)
    ]
    api_source_types = {
        str(row.get('sourceType') or row.get('sourcetype') or '').strip().upper()
        for row in api_table_rows
    }
    api_candidates = {}
    for row in api_table_rows:
        table_name = str(row.get('tableName') or row.get('tablename') or '').strip().upper()
        if not _valid_ident(table_name):
            continue
        api_candidates.setdefault(table_name, {
            'name': table_name,
            'classification': str(row.get('usage') or row.get('USAGE') or 'InfoSet Table'),
            'source_type': str(row.get('sourceType') or row.get('sourcetype') or ''),
        })
    if 'LDB' in infoset_types or logical_databases:
        source_type = 'LOGICAL_DATABASE'
    elif 'J' in api_source_types or ('JOIN' in infoset_types and len(base_tables) > 1):
        source_type = 'JOIN'
    elif 'T' in api_source_types:
        source_type = 'TABLE'
    elif 'JOIN' in infoset_types and len(base_tables) == 1:
        source_type = 'TABLE'

    def _description(rows):
        english_row = next((
            row for row in rows
            if str(row.get('LANGU') or row.get('langu') or row.get('SPRAS') or row.get('spras') or '').upper() == 'E'
        ), rows[0] if rows else {})
        return str(
            english_row.get('TEXT') or english_row.get('text')
            or english_row.get('QTEXT') or english_row.get('qtext')
            or english_row.get('ITEXT') or english_row.get('itext') or ''
        )

    return {
        'query_name': str(
            query_row.get('QNUM') or query_row.get('qnum')
            or query_row.get('QNAME') or query_row.get('qname') or extractor
        ).strip(),
        'query_area': 'GLOBAL' if evidence['AQGQCAT'] else ('LOCAL' if evidence['AQLQCAT'] else None),
        'user_group': user_group,
        'query_description': _description(evidence['IQQCATDBX']),
        'infosets': sorted(infosets),
        'infoset_description': _description(evidence['IQICATDBX']),
        'source_type': source_type,
        'logical_databases': logical_databases,
        'table_candidates': list(api_candidates.values()) or [
            {
                'name': table,
                'classification': (
                    'InfoSet Base Table' if source_type == 'TABLE'
                    else 'InfoSet Join' if source_type == 'JOIN'
                    else 'InfoSet Catalog Candidate'
                ),
                'source_type': '',
            }
            for table in base_tables
            if _valid_ident(table)
        ],
        'join_relationships': [],
        'selection_fields': [],
        'output_fields': [],
        'additional_fields': [],
        'additional_code': [],
        'evidence': evidence,
    }


async def fetch_roosource(oltpsource: str, objvers: str = 'A', logger: logging.Logger = None):
    if logger is None:
        logger = logging.getLogger('sap_analysis')
    if not _valid_ident(oltpsource) or not _valid_ident(objvers):
        return []
    filters = [
        {'fields': ['OLTPSOURCE'], 'operator': 'EQ', 'value': oltpsource},
        {'fields': ['OBJVERS'], 'operator': 'EQ', 'value': objvers},
    ]
    rows = await fetch_table_data_rest('ROOSOURCE', filters=filters, top=200) or []
    matched = [
        r for r in rows
        if str(r.get('OLTPSOURCE', r.get('oltpsource', ''))).strip().upper() == oltpsource.upper()
        and str(r.get('OBJVERS', r.get('objvers', ''))).strip().upper() == objvers.upper()
    ]
    if len(matched) != len(rows):
        dropped = [r.get('OLTPSOURCE') or r.get('oltpsource') for r in rows if r not in matched]
        logger.warning(f"ROOSOURCE query for OLTPSOURCE={oltpsource}/OBJVERS={objvers} returned {len(rows)} row(s) but only {len(matched)} actually matched (REST filters may not be honored); dropped: {dropped}")
    if not matched:
        logger.warning(f"No ROOSOURCE row found matching OLTPSOURCE={oltpsource} OBJVERS={objvers} among {len(rows)} row(s) returned by the REST API.")
    return matched


async def resolve_abap_source(object_name: str, object_type: str = 'FUNC', logger: logging.Logger = None) -> Dict[str, Any]:
    if logger is None:
        logger = logging.getLogger('sap_analysis')
    logger.info(f"Resolving ABAP source for object={object_name} type={object_type}")
    result = {'source': None, 'uri': None, 'evidence': [], 'errors': [], 'includes': [], 'forms': [], 'macros': []}

    if object_type != 'FUNC':
        result['errors'].append({'stage': 'FUNCTION_SOURCE_RETRIEVAL', 'object': object_name, 'type': 'UNSUPPORTED_OBJECT_TYPE', 'message': f"Only FUNC objects are resolvable (got {object_type})", 'recoverable': False})
        return result

    src = await fetch_function_source_rest(object_name)
    if src:
        result['source'] = src
        result['uri'] = f"rest:functionsrc:{object_name}"
        result['evidence'].append({'action': 'functionSourceRest', 'function': object_name})
        result.update(detect_source_symbols(src))
        logger.info(f"Fetched source for {object_name} via SAP REST function-source API ({len(src)} chars)")
        return result

    logger.warning(f"Could not resolve source for {object_name}")
    result['errors'].append({'stage': 'FUNCTION_SOURCE_RETRIEVAL', 'object': object_name, 'type': 'NOT_RETRIEVED', 'message': 'SAP REST function-source API returned no source', 'recoverable': True})
    return result


async def fetch_and_follow_symbols(initial_result: Dict[str, Any], max_depth: int = 8, logger: logging.Logger = None) -> Dict[str, Any]:
    if logger is None:
        logger = logging.getLogger('sap_analysis')
    aggregated = {'sources': {}, 'evidence_snippets': [], 'unresolved_symbols': [], 'comment_hints': []}
    visited = set()

    current_level = []
    if initial_result.get('uri') and initial_result.get('source'):
        current_level.append({'uri': initial_result['uri'], 'source': initial_result['source'], 'path': [initial_result.get('uri')]})

    depth = 0
    while current_level and depth < max_depth:
        next_level = []
        for item in current_level:
            uri = item.get('uri')
            if uri in visited:
                continue
            visited.add(uri)
            src = item.get('source')
            aggregated['sources'][uri] = src
            path = item.get('path', [])
            logger.info(f"Visiting FM/object {uri} (depth={depth}, path={' -> '.join(path)})")
            scan_src = _strip_abap_comments(src)

            for name, comment_text in _extract_comment_hints(src):
                aggregated['comment_hints'].append({'name': name, 'comment': comment_text, 'object': uri, 'discovery_path': path})

            def _add_evidence(table, kind, match_start, match_end):
                start = max(0, match_start - 80)
                end = min(len(src), match_end + 80)
                line_start = src.count('\n', 0, match_start) + 1
                line_end = src.count('\n', 0, match_end) + 1
                aggregated['evidence_snippets'].append({
                    'object': uri, 'object_type': 'UNKNOWN', 'table': table, 'kind': kind,
                    'line_start': line_start, 'line_end': line_end, 'snippet': src[start:end],
                    'discovery_path': path,
                })

            for sel in re.finditer(r"SELECT\s+[\s\S]{1,200}?FROM\s+([A-Z0-9_]+)", scan_src, flags=re.IGNORECASE):
                _add_evidence(sel.group(1).upper(), 'select', sel.start(), sel.end())
            for sel in re.finditer(r"(?:INNER\s+|LEFT\s+OUTER\s+|LEFT\s+)?JOIN\s+([A-Z0-9_]+)", scan_src, flags=re.IGNORECASE):
                _add_evidence(sel.group(1).upper(), 'join', sel.start(), sel.end())
            for sel in re.finditer(r"UPDATE\s+([A-Z0-9_]+)", scan_src, flags=re.IGNORECASE):
                _add_evidence(sel.group(1).upper(), 'update', sel.start(), sel.end())
            for sel in re.finditer(r"INSERT\s+INTO\s+([A-Z0-9_]+)", scan_src, flags=re.IGNORECASE):
                _add_evidence(sel.group(1).upper(), 'insert', sel.start(), sel.end())
            for sel in re.finditer(r"DELETE\s+FROM\s+([A-Z0-9_]+)", scan_src, flags=re.IGNORECASE):
                _add_evidence(sel.group(1).upper(), 'delete', sel.start(), sel.end())
            for sel in re.finditer(r"APPEND_R_TABLE\s*:\s*TABLE_([A-Z0-9_]+)\s*\.", scan_src, flags=re.IGNORECASE):
                _add_evidence(sel.group(1).upper(), 'append_r_table', sel.start(), sel.end())

            symbols = detect_source_symbols(src)

            for m in re.finditer(r"CALL\s+FUNCTION\s+'([A-Z0-9_]+)'", scan_src, flags=re.IGNORECASE):
                cf = m.group(1)
                try:
                    cf_res = await resolve_abap_source(cf, 'FUNC', logger=logger)
                except Exception:
                    cf_res = None
                if cf_res and cf_res.get('source') and cf_res.get('uri') and cf_res.get('uri') not in visited:
                    next_level.append({'uri': cf_res.get('uri'), 'source': cf_res.get('source'), 'path': path + [cf_res.get('uri')]})
                    logger.info(f"CALL FUNCTION '{cf}' found and queued for analysis ({cf_res.get('uri')})")
                else:
                    aggregated['unresolved_symbols'].append({'name': cf, 'reason': 'CALLED_FM_NOT_RESOLVED'})
                    logger.info(f"CALL FUNCTION '{cf}' could not be resolved/fetched")

            for m in re.finditer(r"CALL\s+METHOD\s+([A-Z0-9_:\->=]+)", scan_src, flags=re.IGNORECASE):
                aggregated['unresolved_symbols'].append({'name': m.group(1), 'reason': 'METHOD_CALL_NOT_FOLLOWED'})

            for inc in symbols.get('includes', []) + symbols.get('forms', []) + symbols.get('macros', []):
                aggregated['unresolved_symbols'].append({'name': inc, 'reason': 'NOT_FOUND_IN_REPO'})

        current_level = next_level
        depth += 1

    return aggregated
