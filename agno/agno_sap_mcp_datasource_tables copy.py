import asyncio
import base64
import logging
from datetime import datetime
from os import environ
from pathlib import Path
from typing import List, Optional
import sys

import json
import re
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from agno.agent import Agent
from agno.models.openai.like import OpenAILike

# local helpers
from scripts.utils import (
    extract_datasource_from_prompt,
    extract_objvers_from_prompt,
    _rows_from_query_result,
    _sql_to_rest_query,
    clean_json_response,
    _valid_ident,
    _strip_abap_comments,
    _extract_comment_hints,
    detect_source_symbols,
)
from scripts.rest_api import fetch_table_data_rest, fetch_function_source_rest
from scripts.prompts import AGENT_INSTRUCTIONS, LLM_TABLE_PROMPT, RETURN_RULES_PROMPT
from scripts.analysis import run_query_rest, run_query_rest_all, fetch_roosource, resolve_abap_source, resolve_infoset_query, fetch_and_follow_symbols

load_dotenv()

# Ensure running this file directly can resolve local `agno` package imports by
# preferring the workspace root on `sys.path` when it isn't already present.
try:
    workspace_root = Path(__file__).resolve().parent
    if str(workspace_root) not in sys.path:
        sys.path.insert(0, str(workspace_root.parent))
except Exception:
    pass


class Evidence(BaseModel):
    roosource: Optional[str] = None
    dd02l: Optional[str] = None
    dd26s: Optional[str] = None
    dd02t: Optional[str] = None
    dd03l: Optional[str] = None
    tmcexcfs: Optional[str] = None
    aqgqcat: Optional[str] = None
    aqlqcat: Optional[str] = None
    iqqcatdb: Optional[str] = None
    iqqcatdbx: Optional[str] = None
    iqicatdb: Optional[str] = None
    iqicatdbx: Optional[str] = None
    infoset_api: Optional[str] = None


class SourceEvidence(BaseModel):
    object: str
    object_type: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    snippet: Optional[str] = None
    discovery_path: List[str] = Field(default_factory=list)


class TableEntry(BaseModel):
    name: str
    role: str
    description: Optional[str] = None
    primary_key: List[str] = Field(default_factory=list)
    change_field_candidates: List[str] = Field(default_factory=list)
    evidence: Evidence
    source_evidence: List[SourceEvidence] = Field(default_factory=list)
    confidence: str
    source_classification: Optional[str] = None
    ddic_object_type: Optional[str] = None
    join_relationship: Optional[str] = None


class QueryInfo(BaseModel):
    name: str = ''
    user_group: str = ''
    query_area: str = ''
    infoset: str = ''
    description: str = ''


class InfoSetInfo(BaseModel):
    source_type: str = 'OTHER'
    description: str = ''
    logical_databases: List[str] = Field(default_factory=list)
    selection_fields: List[dict] = Field(default_factory=list)
    output_fields: List[dict] = Field(default_factory=list)
    join_relationships: List[dict] = Field(default_factory=list)
    additional_fields: List[dict] = Field(default_factory=list)
    additional_abap_logic: List[dict] = Field(default_factory=list)


class DatasourceReport(BaseModel):
    datasource: str
    objvers: str
    exmethod: str
    extractor: str
    tables: List[TableEntry] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    candidates_for_manual_review: List[dict] = Field(default_factory=list)
    errors: List[dict] = Field(default_factory=list)
    extractor_change_fields: List[str] = Field(default_factory=list)
    fabric_replication: List[dict] = Field(default_factory=list)
    query: Optional[QueryInfo] = None
    infoset: Optional[InfoSetInfo] = None
    final_unique_tables: List[str] = Field(default_factory=list)


def get_english_description(dd02t_rows: list) -> str:
    """Return the English DD02T text, falling back to the first available text."""
    for row in dd02t_rows:
        language = str(row.get('DDLANGUAGE') or row.get('ddlanguage') or '').strip().upper()
        if language == 'E':
            return str(row.get('DDTEXT') or row.get('ddtext') or '')
    for row in dd02t_rows:
        description = row.get('DDTEXT') or row.get('ddtext')
        if description:
            return str(description)
    return ''


def write_json_report(report: DatasourceReport, output_dir: str = "sap") -> str:
    """Write the validated report to <output_dir>/<datasource>.json."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', report.datasource)
    out_path = out_dir / f"{safe_name}.json"
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return str(out_path)


async def main():
    # Edit this line to ask about a different datasource/extractor.
    user_prompt = "What Tables or views are used by the datasource 0ACCT_TYPE_TEXT"

    datasource = extract_datasource_from_prompt(user_prompt) or environ.get("SAP_DATASOURCE", "0ACCT_TYPE_TEXT")
    objvers = extract_objvers_from_prompt(user_prompt) or environ.get("SAP_OBJVERS", "A")

    # Activity log: tracks FM resolution and every LLM call/response for this run.
    log_dir = Path("sap")
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_log_name = re.sub(r'[^A-Za-z0-9_.-]', '_', datasource)
    log_path = log_dir / f"{safe_log_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger(f"sap_analysis.{id(log_path)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _log_handler = logging.FileHandler(log_path, encoding="utf-8")
    _log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_log_handler)
    logger.info(f"=== Analysis started for datasource={datasource} objvers={objvers} ===")

    

    

    try:
        agent = Agent(
            model=OpenAILike(
                id=environ["MANTLE_MODEL_ID"],
                api_key=environ["MANTLE_API_KEY"],
                base_url=environ["MANTLE_BASE_URL"],
            ),
            tools=[fetch_table_data_rest, fetch_function_source_rest],
            markdown=False,
            debug_mode=True,
            # output_schema=DatasourceReport,
            instructions=AGENT_INSTRUCTIONS,
                        )

        async def llm_extract_tables_from_source(object_name: str, source_text: str):
            """Ask the LLM to read one FM's full logic and comments (not just regex patterns)
            and propose additional tables it depends on, each with a stated evidence type.

            Every proposal is grounded (name must literally appear in source_text) and later
            validated against DD02T by the caller before being trusted as a confirmed table.

            Returns a list of hints (possibly empty if the LLM found nothing), or None if the LLM
            backend itself was unreachable/errored (distinct from a legitimate 'no tables' answer).
            """
            # Prompt text moved to agno.scripts.prompts.LLM_TABLE_PROMPT
            logger.info(f"Calling LLM to analyze {object_name} ({len(source_text)} chars of source)")
            try:
                abap_agent = Agent(
                    model=OpenAILike(
                        id=environ["MANTLE_MODEL_ID"],
                        api_key=environ["MANTLE_API_KEY"],
                        base_url=environ["MANTLE_BASE_URL"],
                    ),
                    markdown=False,
                    debug_mode=True,
                    instructions="You are Expert ABAP Engineer. Identify all SAP tables used in the given ABAP source code."
                )
                response = await abap_agent.arun(LLM_TABLE_PROMPT.format(object_name=object_name, source_text=source_text), stream=False)
            except Exception as e:
                logger.warning(f"LLM call failed for {object_name}: {e}")
                return None
            content = response.content if hasattr(response, 'content') else response
            logger.info(f"LLM response for {object_name}: {str(content)[:2000]}")
            if isinstance(content, str) and content.strip().rstrip('.').lower() in ('connection error', 'error', 'timeout', 'internal server error'):
                logger.warning(f"LLM appears unreachable for {object_name} (response: {content!r})")
                return None
            txt = clean_json_response(str(content))
            m = re.search(r"(\[[\s\S]*\])", txt)
            if not m:
                return []
            try:
                arr = json.loads(m.group(1))
            except (json.JSONDecodeError, TypeError):
                return []
            if not isinstance(arr, list):
                return []
            grounded = []
            upper_source = source_text.upper()
            for item in arr:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name', '')).strip().upper()
                if not name or not _valid_ident(name) or name not in upper_source:
                    continue  # reject anything not literally present in the source (grounding check)
                grounded.append({
                    'name': name,
                    'role': item.get('role') if item.get('role') in ('Core', 'Support') else 'Support',
                    'evidence_type': item.get('evidence_type') if item.get('evidence_type') in ('direct_sql', 'comment', 'logic_inference') else 'logic_inference',
                    'reason': str(item.get('reason', ''))[:300],
                    'confidence': item.get('confidence') if item.get('confidence') in ('High', 'Medium', 'Low') else 'Low',
                })
            logger.info(f"LLM proposed {len(arr)} table(s) for {object_name}; {len(grounded)} grounded in the source text: {[g['name'] for g in grounded]}")
            return grounded

                # Use a typed run that returns a DatasourceReport when possible
        prompt = user_prompt + RETURN_RULES_PROMPT

        # First retrieve ROOSOURCE directly (deterministic) to drive analysis
        roosource_rows = await fetch_roosource(datasource, objvers)
        # If ROOSOURCE available, prefer deterministic analysis path
        fm_name = None
        exmethod = None
        if roosource_rows:
            # roosource_rows may be a list of rows or a dict; try to extract extractor and exmethod
            row = roosource_rows[0] if isinstance(roosource_rows, list) and len(roosource_rows) > 0 else roosource_rows
            fm_name = row.get('EXTRACTOR') or row.get('extractor')
            exmethod = row.get('EXMETHOD') or row.get('exmethod')
            logger.info(f"ROOSOURCE resolved: EXTRACTOR={fm_name} EXMETHOD={exmethod}")

        response = None
        parsed = None
        table_discovery_context = {
            'from_regex': [],
            'functions': {},
        }

        if str(exmethod).upper() == 'V' and fm_name:
            dd26s_rows = await run_query_rest(
                f"SELECT * FROM DD26S WHERE VIEWNAME = '{fm_name}'"
            )
            dd26s_rows = _rows_from_query_result(dd26s_rows)
            table_names = sorted({
                str(row.get('TABNAME') or row.get('tabname') or row.get('TABSUB') or row.get('tabsub') or '').strip().upper()
                for row in dd26s_rows
                if row.get('TABNAME') or row.get('tabname') or row.get('TABSUB') or row.get('tabsub')
            })

            if not table_names:
                table_names = [str(fm_name).strip().upper()]

            report_obj = {
                'datasource': datasource,
                'objvers': objvers,
                'exmethod': exmethod,
                'extractor': fm_name,
                'tables': [],
                'candidates_for_manual_review': [],
                'recommendations': [],
                'errors': [],
            }
            roosource_evidence = json.dumps(roosource_rows[0], default=str) if roosource_rows else None

            for table_name in table_names:
                dd02t_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD02T WHERE TABNAME = '{table_name}'")
                )
                dd03l_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD03L WHERE TABNAME = '{table_name}'")
                )
                description = get_english_description(dd02t_rows)
                primary_key = [
                    row.get('FIELDNAME') or row.get('fieldname')
                    for row in dd03l_rows
                    if (row.get('KEYFLAG') or row.get('keyflag')) == 'X'
                    and (row.get('FIELDNAME') or row.get('fieldname'))
                ]
                report_obj['tables'].append(TableEntry(
                    name=table_name,
                    role='Core',
                    description=description,
                    primary_key=primary_key,
                    evidence=Evidence(
                        roosource=roosource_evidence,
                        dd26s=json.dumps(dd26s_rows, default=str) if dd26s_rows else None,
                        dd02t=json.dumps(dd02t_rows, default=str) if dd02t_rows else None,
                        dd03l=json.dumps(dd03l_rows, default=str) if dd03l_rows else None,
                    ),
                    confidence='High' if dd26s_rows else 'Medium',
                ).model_dump())

            if not dd26s_rows:
                report_obj['recommendations'].append(
                    f"DD26S returned no base-table mapping for view {fm_name}; treated the extractor as a direct table/view."
                )
            parsed = DatasourceReport.model_validate(report_obj)

        elif str(exmethod).strip().upper() == 'D':
            report_obj = {
                'datasource': datasource,
                'objvers': objvers,
                'exmethod': exmethod,
                'extractor': fm_name or datasource,
                'tables': [],
                'candidates_for_manual_review': [],
                'recommendations': [],
                'errors': [],
            }
            roosource_evidence = json.dumps(roosource_rows[0], default=str)

            for table_name in ('DD07L', 'DD07T'):
                dd02l_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD02L WHERE TABNAME = '{table_name}'")
                )
                matching_dd02l_rows = [
                    row for row in dd02l_rows
                    if str(row.get('TABNAME') or row.get('tabname') or '').strip().upper() == table_name
                    and str(row.get('AS4LOCAL') or row.get('as4local') or '').strip().upper() == 'A'
                ]
                if not matching_dd02l_rows:
                    report_obj['errors'].append({
                        'stage': 'DOMAIN_FIXED_VALUES',
                        'object': table_name,
                        'type': 'DDIC_NOT_FOUND',
                        'message': f'{table_name} was not confirmed as an active DDIC object.',
                        'recoverable': True,
                    })
                    continue

                dd02t_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD02T WHERE TABNAME = '{table_name}'")
                )
                matching_dd02t_rows = [
                    row for row in dd02t_rows
                    if str(row.get('TABNAME') or row.get('tabname') or '').strip().upper() == table_name
                ]
                dd03l_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD03L WHERE TABNAME = '{table_name}'")
                )
                matching_dd03l_rows = [
                    row for row in dd03l_rows
                    if str(row.get('TABNAME') or row.get('tabname') or '').strip().upper() == table_name
                ]
                primary_key = [
                    row.get('FIELDNAME') or row.get('fieldname')
                    for row in matching_dd03l_rows
                    if str(row.get('KEYFLAG') or row.get('keyflag') or '').strip().upper() == 'X'
                    and (row.get('FIELDNAME') or row.get('fieldname'))
                ]
                object_type = str(
                    matching_dd02l_rows[0].get('TABCLASS')
                    or matching_dd02l_rows[0].get('tabclass')
                    or ''
                ).strip().upper()
                report_obj['tables'].append(TableEntry(
                    name=table_name,
                    role='Core',
                    description=get_english_description(matching_dd02t_rows),
                    primary_key=primary_key,
                    evidence=Evidence(
                        roosource=roosource_evidence,
                        dd02l=json.dumps(matching_dd02l_rows, default=str),
                        dd02t=json.dumps(matching_dd02t_rows, default=str) if matching_dd02t_rows else None,
                        dd03l=json.dumps(matching_dd03l_rows, default=str) if matching_dd03l_rows else None,
                    ),
                    confidence='High',
                    source_classification='Domain Fixed Values',
                    ddic_object_type=object_type,
                ).model_dump())

            report_obj['final_unique_tables'] = sorted(
                table['name'] for table in report_obj['tables']
            )
            parsed = DatasourceReport.model_validate(report_obj)

        elif str(exmethod).strip().upper() == 'Q':
            query_name = fm_name or datasource
            infoset_result = await resolve_infoset_query(datasource, query_name)
            catalog_evidence = infoset_result['evidence']
            report_obj = {
                'datasource': datasource,
                'objvers': objvers,
                'exmethod': exmethod,
                'extractor': query_name,
                'tables': [],
                'candidates_for_manual_review': [],
                'recommendations': [],
                'errors': [],
                'query': {
                    'name': infoset_result['query_name'],
                    'user_group': infoset_result['user_group'],
                    'query_area': infoset_result['query_area'] or '',
                    'infoset': ', '.join(infoset_result['infosets']),
                    'description': infoset_result['query_description'],
                },
                'infoset': {
                    'source_type': infoset_result['source_type'],
                    'description': infoset_result['infoset_description'],
                    'logical_databases': infoset_result['logical_databases'],
                    'selection_fields': infoset_result['selection_fields'],
                    'output_fields': infoset_result['output_fields'],
                    'join_relationships': infoset_result['join_relationships'],
                    'additional_fields': infoset_result['additional_fields'],
                    'additional_abap_logic': infoset_result['additional_code'],
                },
            }
            roosource_evidence = json.dumps(roosource_rows[0], default=str)

            for table_candidate in infoset_result['table_candidates']:
                table_name = table_candidate['name']
                dd02l_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD02L WHERE TABNAME = '{table_name}'")
                )
                matching_dd02l_rows = [
                    row for row in dd02l_rows
                    if str(row.get('TABNAME') or row.get('tabname') or '').strip().upper() == table_name
                    and str(row.get('AS4LOCAL') or row.get('as4local') or '').strip().upper() == 'A'
                ]
                object_type = str(
                    (matching_dd02l_rows[0].get('TABCLASS') or matching_dd02l_rows[0].get('tabclass') or '')
                    if matching_dd02l_rows else ''
                ).strip().upper()
                if object_type not in {'TRANSP', 'CLUSTER', 'POOL'}:
                    report_obj['candidates_for_manual_review'].append({
                        'name': table_name,
                        'reason': (
                            f"InfoSet catalog source has DDIC type {object_type or 'unresolved'}, "
                            'not a confirmed physical database table.'
                        ),
                    })
                    continue

                dd02t_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD02T WHERE TABNAME = '{table_name}'")
                )
                matching_dd02t_rows = [
                    row for row in dd02t_rows
                    if str(row.get('TABNAME') or row.get('tabname') or '').strip().upper() == table_name
                ]
                if not matching_dd02t_rows:
                    report_obj['candidates_for_manual_review'].append({
                        'name': table_name,
                        'reason': 'Referenced by the InfoSet catalog but not confirmed in DD02T.',
                    })
                    continue

                dd03l_rows = _rows_from_query_result(
                    await run_query_rest(f"SELECT * FROM DD03L WHERE TABNAME = '{table_name}'")
                )
                matching_dd03l_rows = [
                    row for row in dd03l_rows
                    if str(row.get('TABNAME') or row.get('tabname') or '').strip().upper() == table_name
                ]
                primary_key = [
                    row.get('FIELDNAME') or row.get('fieldname')
                    for row in matching_dd03l_rows
                    if str(row.get('KEYFLAG') or row.get('keyflag') or '').strip().upper() == 'X'
                    and (row.get('FIELDNAME') or row.get('fieldname'))
                ]
                report_obj['tables'].append(TableEntry(
                    name=table_name,
                    role='Core',
                    description=get_english_description(matching_dd02t_rows),
                    primary_key=primary_key,
                    evidence=Evidence(
                        roosource=roosource_evidence,
                        dd02l=json.dumps(matching_dd02l_rows, default=str),
                        dd02t=json.dumps(matching_dd02t_rows, default=str),
                        dd03l=json.dumps(matching_dd03l_rows, default=str) if matching_dd03l_rows else None,
                        aqgqcat=json.dumps(catalog_evidence['AQGQCAT'], default=str) or None,
                        aqlqcat=json.dumps(catalog_evidence['AQLQCAT'], default=str) or None,
                        iqqcatdb=json.dumps(catalog_evidence['IQQCATDB'], default=str) or None,
                        iqqcatdbx=json.dumps(catalog_evidence['IQQCATDBX'], default=str) or None,
                        iqicatdb=json.dumps(catalog_evidence['IQICATDB'], default=str) or None,
                        iqicatdbx=json.dumps(catalog_evidence['IQICATDBX'], default=str) or None,
                        infoset_api=json.dumps(catalog_evidence['Z_INFOSET'], default=str) or None,
                    ),
                    confidence='High',
                    source_classification=table_candidate['classification'],
                    ddic_object_type=object_type,
                ).model_dump())

            infosets = infoset_result['infosets']
            if not infosets:
                report_obj['errors'].append({
                    'stage': 'INFOSET_RESOLUTION',
                    'object': query_name,
                    'type': 'NOT_FOUND',
                    'message': 'No linked InfoSet was found in SAP Query catalogs or returned by the z_infoset API.',
                    'recoverable': True,
                })
            elif not report_obj['tables']:
                report_obj['recommendations'].append(
                    f"Inspect InfoSet {', '.join(infosets)} in SQ02; its catalog entry did not expose a DD02T-confirmed base table."
                )
            if infoset_result['source_type'] == 'JOIN':
                report_obj['recommendations'].append(
                    'Retrieve the authoritative SQ02 InfoSet definition to resolve join relationships and join fields.'
                )
            if infoset_result['logical_databases']:
                report_obj['recommendations'].append(
                    'Inspect the logical database definition and database program to resolve actual table accesses; '
                    'logical database names were not classified as tables.'
                )
            report_obj['recommendations'].append(
                'Selection/output fields and additional InfoSet ABAP require the authoritative SQ02 definition; '
                'catalog metadata alone is not sufficient and no table was inferred from fields.'
            )
            report_obj['recommendations'].append(
                f"Query area: {infoset_result['query_area'] or 'unresolved'}; InfoSet(s): {', '.join(infosets) or 'unresolved'}."
            )
            report_obj['final_unique_tables'] = sorted({
                table['name'] for table in report_obj['tables']
            })
            parsed = DatasourceReport.model_validate(report_obj)

        elif (
            exmethod
            and str(exmethod).upper().startswith('F')
            and fm_name
            and (
                str(datasource).strip().upper().startswith('2LIS')
                or str(fm_name).strip().upper().startswith('2LIS')
            )
        ):
            roosource_row = roosource_rows[0]
            extract_structure = str(
                roosource_row.get('EXSTRUCT')
                or roosource_row.get('exstruct')
                or ''
            ).strip().upper()
            report_obj = {
                'datasource': datasource,
                'objvers': objvers,
                'exmethod': exmethod,
                'extractor': fm_name,
                'tables': [],
                'candidates_for_manual_review': [],
                'recommendations': [],
                'errors': [],
            }

            if not extract_structure:
                report_obj['errors'].append({
                    'stage': 'LO_EXTRACT_STRUCTURE',
                    'object': datasource,
                    'type': 'MISSING_EXSTRUCT',
                    'message': 'ROOSOURCE did not provide an extract structure for this LO DataSource.',
                    'recoverable': True,
                })
                report_obj['recommendations'].append(
                    f"Maintain or verify ROOSOURCE.EXSTRUCT for LO DataSource {datasource}."
                )
            else:
                tmcexcfs_rows = _rows_from_query_result(
                    await run_query_rest_all(
                        f"SELECT * FROM TMCEXCFS WHERE ESTRUC = '{extract_structure}'"
                    )
                )
                matching_tmcexcfs_rows = [
                    tmcexcfs_row
                    for tmcexcfs_row in tmcexcfs_rows
                    if str(
                        tmcexcfs_row.get('ESTRUC')
                        or tmcexcfs_row.get('estruc')
                        or ''
                    ).strip().upper() == extract_structure
                ]
                candidate_mappings = {}
                for tmcexcfs_row in matching_tmcexcfs_rows:
                    communication_structure = str(
                        tmcexcfs_row.get('CSTRUC')
                        or tmcexcfs_row.get('cstruc')
                        or ''
                    ).strip().upper()
                    if not communication_structure:
                        continue
                    table_name = (
                        communication_structure[2:]
                        if communication_structure.startswith('MC')
                        else communication_structure
                    )
                    if _valid_ident(table_name):
                        candidate_mappings.setdefault(table_name, []).append(tmcexcfs_row)

                if not candidate_mappings:
                    report_obj['errors'].append({
                        'stage': 'LO_TABLE_MAPPING',
                        'object': extract_structure,
                        'type': 'NOT_FOUND',
                        'message': 'TMCEXCFS returned no communication structures for the LO extract structure.',
                        'recoverable': True,
                    })
                    report_obj['recommendations'].append(
                        f"Verify TMCEXCFS entries where ESTRUC = '{extract_structure}'."
                    )

                roosource_evidence = json.dumps(roosource_row, default=str)
                for table_name, mapping_rows in sorted(candidate_mappings.items()):
                    dd02t_rows = _rows_from_query_result(
                        await run_query_rest(
                            f"SELECT * FROM DD02T WHERE TABNAME = '{table_name}'"
                        )
                    )
                    matching_dd02t_rows = [
                        dd02t_row
                        for dd02t_row in dd02t_rows
                        if str(
                            dd02t_row.get('TABNAME')
                            or dd02t_row.get('tabname')
                            or ''
                        ).strip().upper() == table_name
                    ]
                    if not matching_dd02t_rows:
                        report_obj['candidates_for_manual_review'].append({
                            'name': table_name,
                            'reason': (
                                'Derived from TMCEXCFS.CSTRUC but not confirmed in DD02T; '
                                'verify the LO communication-structure mapping manually.'
                            ),
                        })
                        continue

                    dd03l_rows = _rows_from_query_result(
                        await run_query_rest(
                            f"SELECT * FROM DD03L WHERE TABNAME = '{table_name}'"
                        )
                    )
                    matching_dd03l_rows = [
                        dd03l_row
                        for dd03l_row in dd03l_rows
                        if str(
                            dd03l_row.get('TABNAME')
                            or dd03l_row.get('tabname')
                            or ''
                        ).strip().upper() == table_name
                    ]
                    primary_key = [
                        dd03l_row.get('FIELDNAME') or dd03l_row.get('fieldname')
                        for dd03l_row in matching_dd03l_rows
                        if str(
                            dd03l_row.get('KEYFLAG')
                            or dd03l_row.get('keyflag')
                            or ''
                        ).strip().upper() == 'X'
                        and (dd03l_row.get('FIELDNAME') or dd03l_row.get('fieldname'))
                    ]
                    report_obj['tables'].append(TableEntry(
                        name=table_name,
                        role='Core',
                        description=get_english_description(matching_dd02t_rows),
                        primary_key=primary_key,
                        evidence=Evidence(
                            roosource=roosource_evidence,
                            tmcexcfs=json.dumps(mapping_rows, default=str),
                            dd02t=json.dumps(matching_dd02t_rows, default=str),
                            dd03l=json.dumps(matching_dd03l_rows, default=str) if matching_dd03l_rows else None,
                        ),
                        confidence='High',
                    ).model_dump())

            parsed = DatasourceReport.model_validate(report_obj)

        elif (
            exmethod
            and str(exmethod).upper().startswith('F')
            and fm_name
            and not str(datasource).strip().upper().startswith('2LIS')
            and not str(fm_name).strip().upper().startswith('2LIS')
        ):
            # Deterministic FM analysis using resolver
            fm_result = await resolve_abap_source(fm_name, 'FUNC')
            report_obj = {
                'datasource': datasource,
                'objvers': objvers,
                'exmethod': exmethod,
                'extractor': fm_name,
                'tables': [],
                'candidates_for_manual_review': [],
                'recommendations': [],
                'errors': fm_result.get('errors', []),
            }

            # If source found, deterministically follow includes/forms/macros to find actual SELECTs
            if fm_result.get('source'):
                # Attach source evidence
                report_obj['source_retrieval'] = {'uri': fm_result.get('uri'), 'snippet_preview': fm_result.get('source')[:200]}

                # Recursively follow symbols to find SELECT/JOIN occurrences
                agg = await fetch_and_follow_symbols(fm_result, max_depth=8)

                table_discovery_context['from_regex'] = sorted({
                    evidence['table']
                    for evidence in agg.get('evidence_snippets', [])
                    if evidence.get('table')
                })
                for object_uri in agg.get('sources', {}):
                    function_name = object_uri.rsplit(':', 1)[-1]
                    table_discovery_context['functions'][function_name] = {
                        'tables': [],
                        'all_hints': [],
                        'regex_hints': [],
                        'comment_hints': [],
                        'llm_hints': [],
                    }
                for evidence in agg.get('evidence_snippets', []):
                    function_name = str(evidence.get('object', 'UNKNOWN')).rsplit(':', 1)[-1]
                    function_hints = table_discovery_context['functions'].setdefault(
                        function_name,
                        {'tables': [], 'all_hints': [], 'regex_hints': [], 'comment_hints': [], 'llm_hints': []},
                    )
                    function_hints['regex_hints'].append(evidence)
                    function_hints['all_hints'].append({'source': 'regex', **evidence})
                    if evidence.get('table') not in function_hints['tables']:
                        function_hints['tables'].append(evidence['table'])
                for hint in agg.get('comment_hints', []):
                    function_name = str(hint.get('object', 'UNKNOWN')).rsplit(':', 1)[-1]
                    function_hints = table_discovery_context['functions'].setdefault(
                        function_name,
                        {'tables': [], 'all_hints': [], 'regex_hints': [], 'comment_hints': [], 'llm_hints': []},
                    )
                    function_hints['comment_hints'].append(hint)
                    function_hints['all_hints'].append({'source': 'comment', **hint})
                    if hint.get('name') not in function_hints['tables']:
                        function_hints['tables'].append(hint['name'])

                # Surface anything the traversal couldn't resolve (e.g. macros like GET_DELTA_DATA
                # that live inside a function-group include rather than being a repository object
                # in their own right) so they aren't silently dropped from the report.
                for u in agg.get('unresolved_symbols', []):
                    report_obj['candidates_for_manual_review'].append({
                        'name': u.get('name'),
                        'reason': f"Could not automatically resolve/follow this symbol ({u.get('reason')}); inspect it manually for additional table dependencies.",
                    })

                # Collect extractor-level change fields (recorded separately until table ownership proven)
                change_field_candidates = set()
                field_pattern = re.compile(r"'([A-Z0-9_]{3,10})'", flags=re.IGNORECASE)
                for s in [fm_result.get('source')] + list(agg.get('sources', {}).values()):
                    if not s:
                        continue
                    for m in field_pattern.finditer(s):
                        name = m.group(1).upper()
                        if name in ['CPUDT', 'CPUTM', 'AEDAT', 'AETIM', 'AUDAT', 'ERDAT', 'ERZET']:
                            change_field_candidates.add(name)

                report_obj['extractor_change_fields'] = list(change_field_candidates)

                # Build table entries from evidence, ranking role/confidence by evidence kind:
                # direct SELECT/JOIN > APPEND_R_TABLE macro registration > write-only (UPDATE/INSERT/DELETE)
                ROLE_RANK = {'Support': 1, 'Core': 2}
                CONF_RANK = {'Low': 1, 'Medium': 2, 'High': 3}

                def _evidence_role_confidence(ev):
                    kind = ev.get('kind', 'select')
                    dp = ev.get('discovery_path') or []
                    if kind in ('select', 'join'):
                        role = 'Core' if len(dp) == 1 and dp[0] == fm_result.get('uri') else 'Support'
                        return role, 'High'
                    if kind == 'append_r_table':
                        return 'Core', 'Medium'
                    return 'Support', 'Medium'  # update/insert/delete

                tables_by_name = {}
                for ev in agg.get('evidence_snippets', []):
                    table = ev.get('table')
                    if not table:
                        continue
                    role, confidence = _evidence_role_confidence(ev)
                    if table not in tables_by_name:
                        tables_by_name[table] = {'name': table, 'source_evidence': [], 'role': role, 'confidence': confidence, 'source': 'static'}
                    else:
                        entry = tables_by_name[table]
                        if ROLE_RANK[role] > ROLE_RANK[entry['role']]:
                            entry['role'] = role
                        if CONF_RANK[confidence] > CONF_RANK[entry['confidence']]:
                            entry['confidence'] = confidence
                    se = SourceEvidence(object=ev.get('object'), object_type=ev.get('object_type'), line_start=ev.get('line_start'), line_end=ev.get('line_end'), snippet=ev.get('snippet'), discovery_path=ev.get('discovery_path', []))
                    tables_by_name[table]['source_evidence'].append(se)

                # Field-list-builder variables like E_OLTP_FIELDS_BKPF name a table without a
                # direct SELECT/JOIN; flag them for manual review instead of guessing they're used.
                fieldlist_tables = set()
                for s in list(agg.get('sources', {}).values()):
                    if not s:
                        continue
                    for m in re.finditer(r"\bFIELDS_([A-Z][A-Z0-9_]{2,})\b", _strip_abap_comments(s), flags=re.IGNORECASE):
                        fieldlist_tables.add(m.group(1).upper())
                for t in sorted(fieldlist_tables - set(tables_by_name.keys())):
                    report_obj['candidates_for_manual_review'].append({
                        'name': t,
                        'reason': f"Referenced via a field-list builder variable (e.g. '...FIELDS_{t}') but no direct SELECT/JOIN was found for it; the actual SQL may live in an unresolved macro/include.",
                    })

                # Comment-only mentions (e.g. 'Join mit BKPF') are weak but often correct signal;
                # surface them too, skipping anything already confirmed or already flagged above.
                already_flagged = {c['name'] for c in report_obj['candidates_for_manual_review']}
                seen_hints = set()
                for hint in agg.get('comment_hints', []):
                    name = hint['name']
                    if name in tables_by_name or name in already_flagged or name in seen_hints:
                        continue
                    seen_hints.add(name)
                    report_obj['candidates_for_manual_review'].append({
                        'name': name,
                        'reason': f"Mentioned only in a source comment (\"{hint['comment']}\") in {hint['object']}; not confirmed by executable code.",
                    })

                # Helper to fetch DD02L/DD02T/DD03L
                async def fetch_dd02l(table_name: str):
                    """Returns matching rows, [] if DD02L genuinely has no such row, or None if the
                    lookup itself failed/errored (inconclusive; must NOT be read as non-existence)."""
                    if not _valid_ident(table_name):
                        return []
                    sql = f"SELECT TABNAME, AS4LOCAL, AS4VERS FROM DD02L WHERE TABNAME = '{table_name}' AND AS4LOCAL = 'A'"
                    res = await run_query_rest(sql)
                    if res is None:
                        return None
                    return _rows_from_query_result(res)

                async def fetch_dd02t(table_name: str):
                    if not _valid_ident(table_name):
                        return []
                    sql = f"SELECT * FROM DD02T WHERE TABNAME = '{table_name}'"
                    res = await run_query_rest(sql)
                    return _rows_from_query_result(res)

                async def fetch_dd03l(table_name: str):
                    if not _valid_ident(table_name):
                        return []
                    sql = f"SELECT * FROM DD03L WHERE TABNAME = '{table_name}'"
                    res = await run_query_rest(sql)
                    return _rows_from_query_result(res)

                # LLM understanding pass: let the model read each fetched FM's full logic and
                # comments (not just regex patterns) to catch tables the static scan may miss.
                # Every proposal is grounded (must appear literally in the source text, checked in
                # llm_extract_tables_from_source); DD02L existence is enforced separately below.
                all_sources = {fm_result.get('uri'): fm_result.get('source')}
                all_sources.update(agg.get('sources', {}))
                llm_unavailable_streak = 0
                llm_skipped = False
                LLM_FAILURE_CIRCUIT_THRESHOLD = 3
                for obj_uri, obj_src in all_sources.items():
                    if not obj_src or llm_skipped:
                        continue
                    try:
                        llm_hints = await llm_extract_tables_from_source(obj_uri, obj_src)
                    except Exception:
                        llm_hints = None
                    if llm_hints is None:
                        llm_unavailable_streak += 1
                        if llm_unavailable_streak >= LLM_FAILURE_CIRCUIT_THRESHOLD:
                            logger.warning(f"LLM unreachable for {llm_unavailable_streak} consecutive objects; skipping the LLM understanding pass for the rest of this run.")
                            report_obj['recommendations'].append(
                                "LLM understanding pass skipped for the remainder of this run: the LLM backend (MANTLE_BASE_URL) appears unreachable. Static/regex-based table discovery is unaffected; re-run once connectivity is restored to also capture LLM-inferred tables from comments/logic."
                            )
                            llm_skipped = True
                        continue
                    llm_unavailable_streak = 0
                    function_name = str(obj_uri).rsplit(':', 1)[-1]
                    function_hints = table_discovery_context['functions'].setdefault(
                        function_name,
                        {'tables': [], 'all_hints': [], 'regex_hints': [], 'comment_hints': [], 'llm_hints': []},
                    )
                    function_hints['llm_hints'].extend(llm_hints)
                    function_hints['all_hints'].extend({'source': 'llm', **hint} for hint in llm_hints)
                    for hint in llm_hints:
                        if hint.get('name') not in function_hints['tables']:
                            function_hints['tables'].append(hint['name'])
                    for hint in llm_hints:
                        name = hint['name']
                        if name in tables_by_name:
                            continue  # already confirmed by static analysis; keep that evidence
                        role = hint['role'] if hint['evidence_type'] == 'direct_sql' else 'Support'
                        confidence = 'Medium' if hint['confidence'] != 'Low' else 'Low'
                        se = SourceEvidence(object=obj_uri, object_type='UNKNOWN', snippet=hint['reason'], discovery_path=[obj_uri])
                        tables_by_name[name] = {'name': name, 'source_evidence': [se], 'role': role, 'confidence': confidence, 'source': 'llm'}

                # Final gate: every table (static or LLM-sourced) must exist in DD02L, the
                # authoritative SAP table catalog. A genuine empty result (call succeeded, 0 rows)
                # evicts the table; a failed/errored lookup is inconclusive and must never evict
                # (one bad DD02L call can otherwise cascade into every other table looking "missing").
                for tname in list(tables_by_name.keys()):
                    dd02l_rows = await fetch_dd02l(tname)
                    if dd02l_rows:
                        continue
                    already_flagged = {c['name'] for c in report_obj['candidates_for_manual_review']}
                    if dd02l_rows is None:
                        note = f"DD02L lookup for '{tname}' failed (tool/session error); kept unconfirmed, verify manually."
                        logger.warning(note)
                        if tname not in already_flagged:
                            report_obj['recommendations'].append(note)
                        continue
                    logger.warning(f"DD02L did not confirm '{tname}'; evicting from confirmed tables.")
                    entry = tables_by_name.pop(tname)
                    if tname not in already_flagged:
                        report_obj['candidates_for_manual_review'].append({
                            'name': tname,
                            'reason': f"Not found in DD02L (evidence source: {entry.get('source', 'unknown')}); likely a hallucination, typo, or non-existent table.",
                        })

                for tname, info in tables_by_name.items():
                    dd02t = await fetch_dd02t(tname)
                    dd03l = await fetch_dd03l(tname)
                    desc = ''
                    primary_keys = []
                    if dd02t:
                        desc = get_english_description(dd02t)
                    for r in dd03l:
                        if r.get('KEYFLAG') == 'X' or r.get('keyflag') == 'X':
                            primary_keys.append(r.get('FIELDNAME') or r.get('fieldname'))

                    # Attach change fields to table only if they exist in DD03L
                    table_change_fields = []
                    if dd03l:
                        fields = {(r.get('FIELDNAME') or r.get('fieldname')): r for r in dd03l}
                        for cf in report_obj.get('extractor_change_fields', []):
                            if cf in fields:
                                table_change_fields.append(cf)

                    te = TableEntry(name=tname, role=info['role'], description=desc, primary_key=primary_keys, change_field_candidates=table_change_fields, evidence=Evidence(roosource=fm_result.get('uri')), source_evidence=info['source_evidence'], confidence=info['confidence'])
                    report_obj['tables'].append(json.loads(te.model_dump_json()))

                # Build fabric_replication recommendations: include all Core tables; include Support with High confidence
                fabric = []
                for t in report_obj['tables']:
                    if t.get('role') == 'Core':
                        fabric.append({'name': t['name'], 'role': t['role'], 'reason': 'Supplies extractor row-level data', 'confidence': t.get('confidence', ''), 'discovery_path': [se.get('discovery_path') for se in t.get('source_evidence', [])]})
                    elif t.get('role') == 'Support' and t.get('confidence') == 'High':
                        fabric.append({'name': t['name'], 'role': t['role'], 'reason': 'High-confidence support/context table required for enrichment or filtering', 'confidence': t.get('confidence', ''), 'discovery_path': [se.get('discovery_path') for se in t.get('source_evidence', [])]})

                report_obj['fabric_replication'] = fabric

                # If still no confirmed tables after static analysis + LLM understanding pass,
                # record it as a recommendation rather than attempting another differently-shaped
                # LLM call whose output wouldn't match the TableEntry schema (evidence must be a
                # dict, not a string) and could crash validation like the raw whole-object reply did.
                if not report_obj['tables']:
                    report_obj['recommendations'].append(
                        f"Static analysis and LLM understanding pass found no confirmed tables for {fm_name}; manual review required."
                    )

                logger.info(f"Confirmed tables ({len(report_obj['tables'])}): {[t['name'] for t in report_obj['tables']]}")
                logger.info(f"Candidates for manual review ({len(report_obj['candidates_for_manual_review'])}): {[c['name'] for c in report_obj['candidates_for_manual_review']]}")
                logger.info("Table discovery context for main agent: %s", json.dumps(table_discovery_context))
            else:
                # No source retrieved — add recommendation
                logger.warning(f"Could not retrieve source for {fm_name}; ending analysis for this FM.")
                report_obj['recommendations'].append(f"Could not retrieve source for {fm_name}; manual inspection required.")

            # Validate final report_obj against DatasourceReport. The .json file is the program's
            # guaranteed final output, so a validation error must never suppress it: fall back to
            # re-validating each table individually and dropping only the ones that fail.
            try:
                parsed = DatasourceReport.model_validate(report_obj)
            except ValidationError as e:
                logger.error(f"Validation error building deterministic report: {e}")
                safe_tables = []
                for t in report_obj.get('tables', []):
                    try:
                        safe_tables.append(TableEntry.model_validate(t))
                    except ValidationError as te:
                        logger.error(f"Dropping invalid table entry {t.get('name')}: {te}")
                report_obj['tables'] = [json.loads(t.model_dump_json()) for t in safe_tables]
                report_obj.setdefault('recommendations', []).append(
                    f"Report validation failed and some data may be incomplete: {e}"
                )
                parsed = DatasourceReport.model_construct(**{**report_obj, 'tables': safe_tables})

        else:
            # Fallback: use LLM with strict prompt. Never let a network/model failure here crash
            # the whole run silently -- the .json file is the guaranteed final output.
            try:
                response = await agent.arun(prompt, stream=False)
            except Exception as e:
                logger.error(f"Agent fallback run failed: {e!r}")
                parsed = DatasourceReport.model_construct(
                    datasource=datasource, objvers=objvers, exmethod=exmethod or '', extractor=fm_name or '',
                    recommendations=[f"LLM fallback analysis failed: {e}; manual review required."],
                )

        # If the deterministic branch already produced a validated report, print it as-is.
        # The validated JSON file is the program's final output.
        if parsed is not None:
            print(parsed.model_dump_json(indent=2))
            json_path = write_json_report(parsed)
            print(f"FINAL OUTPUT (JSON report): {json_path}")
            logger.info(f"JSON report written to {json_path}")
        elif response is None:
            print("No response returned by agent.")
        else:
            content = response.content if hasattr(response, 'content') else response

            if isinstance(content, DatasourceReport):
                parsed = content
            elif isinstance(content, dict):
                try:
                    parsed = DatasourceReport.model_validate(content)
                except ValidationError as e:
                    print("Validation error:\n", e)
            else:
                # Try to extract JSON substring
                txt = clean_json_response(str(content))
                # find first { ... } block
                m = re.search(r"(\{[\s\S]*\})", txt)
                if m:
                    json_text = m.group(1)
                    try:
                        obj = json.loads(json_text)
                        parsed = DatasourceReport.model_validate(obj)
                    except (json.JSONDecodeError, ValidationError) as e:
                        print("Failed to parse/validate JSON response:\n", e)
                else:
                    print("No JSON object found in agent response.")

            if parsed is None:
                print("Expected DatasourceReport, got:\n", content)
            else:
                print(parsed.model_dump_json(indent=2))
                json_path = write_json_report(parsed)
                print(f"FINAL OUTPUT (JSON report): {json_path}")
                logger.info(f"JSON report written to {json_path}")

    finally:
        logger.info("=== Analysis finished ===")
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)


if __name__ == "__main__":
    asyncio.run(main())