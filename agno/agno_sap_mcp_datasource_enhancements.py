import asyncio
import html
import json
import re
from os import environ
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from agno.tools.mcp import MCPTools
from scripts.enhancement_analysis import (
    EnhancementReport,
    analyze_datasource_enhancement,
    report_as_json,
)


load_dotenv()


class MCPRepositorySourceReader:
    """Resolve ABAP object names to ADT URLs and read their source."""

    def __init__(self, mcp_tools: MCPTools, datasource: str):
        self.mcp_tools = mcp_tools
        self.datasource = datasource
        self.search_tool: Optional[Any] = None
        self.source_tool: Optional[Any] = None
        self.grep_tool: Optional[Any] = None
        self.destination = environ.get("SAP_MCP_DESTINATION")

    async def initialize(self) -> None:
        session = self.mcp_tools.session
        if session is None:
            raise RuntimeError("SAP MCP session is not connected.")
        listed = await session.list_tools()
        tools = list(listed if isinstance(listed, list) else listed.tools)
        tools_by_name = {tool.name: tool for tool in tools}
        self.search_tool = tools_by_name.get("searchObject")
        self.source_tool = tools_by_name.get("getObjectSource")
        self.grep_tool = tools_by_name.get("grepPackage")
        if self.search_tool is None or self.source_tool is None or self.grep_tool is None:
            available = ", ".join(sorted(tool.name for tool in tools))
            raise RuntimeError(
                "SAP MCP must expose searchObject, getObjectSource, and grepPackage. "
                f"Available tools: {available}"
            )

    @staticmethod
    def _source_from_value(value: Any) -> Optional[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                if stripped.startswith("<?xml") or stripped.startswith("<"):
                    source_match = re.search(
                        r"<(?:abapsource:)?source[^>]*>([\s\S]*?)</(?:abapsource:)?source>",
                        stripped,
                        re.IGNORECASE,
                    )
                    return html.unescape(source_match.group(1)) if source_match else None
                return value
            return MCPRepositorySourceReader._source_from_value(parsed)
        if isinstance(value, dict):
            for key in (
                "source",
                "sourceCode",
                "source_code",
                "code",
                "abapSource",
                "content",
                "result",
                "data",
            ):
                if key in value:
                    source = MCPRepositorySourceReader._source_from_value(value[key])
                    if source:
                        return source
        if isinstance(value, list):
            text_parts: List[str] = []
            for item in value:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    source = MCPRepositorySourceReader._source_from_value(text)
                    if source:
                        text_parts.append(source)
            if text_parts:
                return "\n".join(text_parts)
        return None

    @staticmethod
    def _object_info_from_value(value: Any, object_name: str) -> Optional[Dict[str, str]]:
        if isinstance(value, str):
            try:
                return MCPRepositorySourceReader._object_info_from_value(json.loads(value), object_name)
            except json.JSONDecodeError:
                return None
        if isinstance(value, list):
            for item in value:
                text = getattr(item, "text", None)
                candidate = MCPRepositorySourceReader._object_info_from_value(
                    text if isinstance(text, str) else item,
                    object_name,
                )
                if candidate:
                    return candidate
        if isinstance(value, dict):
            name = str(
                value.get("adtcore:name") or value.get("name") or value.get("objectName") or ""
            ).strip().upper()
            url = value.get("adtcore:uri") or value.get("uri") or value.get("objectUrl")
            if name == object_name and isinstance(url, str):
                return {
                    "url": url,
                    "package": str(
                        value.get("adtcore:packageName") or value.get("packageName") or ""
                    ).strip(),
                }
            for child in value.values():
                candidate = MCPRepositorySourceReader._object_info_from_value(child, object_name)
                if candidate:
                    return candidate
        return None

    @staticmethod
    def _grep_source(value: Any, object_name: str) -> Optional[str]:
        if isinstance(value, str):
            try:
                return MCPRepositorySourceReader._grep_source(json.loads(value), object_name)
            except json.JSONDecodeError:
                return None
        if isinstance(value, list):
            for item in value:
                text = getattr(item, "text", None)
                source = MCPRepositorySourceReader._grep_source(
                    text if isinstance(text, str) else item,
                    object_name,
                )
                if source:
                    return source
        if isinstance(value, dict):
            matches = value.get("matches")
            if isinstance(matches, list):
                blocks = []
                for match in matches:
                    if not isinstance(match, dict) or str(match.get("name", "")).upper() != object_name:
                        continue
                    context = [str(line) for line in match.get("context", [])]
                    text = str(match.get("text") or "")
                    blocks.append("\n".join(context + [text]))
                return "\n".join(blocks) if blocks else None
        return None

    async def _call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Any]:
        session = self.mcp_tools.session
        if session is None:
            return None
        try:
            return await session.call_tool(tool_name, arguments, raise_on_error=False)
        except TypeError:
            try:
                return await session.call_tool(tool_name, arguments)
            except Exception:
                return None
        except Exception:
            return None

    async def read(self, object_name: str, object_type: str) -> Optional[str]:
        if self.search_tool is None or self.source_tool is None or self.grep_tool is None:
            await self.initialize()
        if self.search_tool is None or self.source_tool is None or self.grep_tool is None:
            return None

        search_arguments: Dict[str, Any] = {"query": object_name, "max": 20}
        object_type_filter = {
            "INCL": "PROG/I",
            "PROG": "PROG/P",
            "CLAS": "CLAS/OC",
        }.get(object_type)
        if object_type_filter:
            search_arguments["objType"] = object_type_filter
        if self.destination:
            search_arguments["destination"] = self.destination
        search_result = await self._call_tool(self.search_tool.name, search_arguments)
        if search_result is None or getattr(search_result, "is_error", False):
            return None
        object_info = self._object_info_from_value(
            getattr(search_result, "structured_content", None),
            object_name,
        ) or self._object_info_from_value(getattr(search_result, "content", None), object_name)
        if not object_info:
            return None
        object_url = object_info["url"]

        source_arguments: Dict[str, Any] = {"objectSourceUrl": object_url}
        if not object_url.rstrip("/").endswith("/source/main"):
            source_arguments["objectSourceUrl"] = f"{object_url.rstrip('/')}/source/main"
        if self.destination:
            source_arguments["destination"] = self.destination
        source_result = await self._call_tool(self.source_tool.name, source_arguments)
        if source_result is not None and not getattr(source_result, "is_error", False):
            structured = getattr(source_result, "structured_content", None)
            source = self._source_from_value(structured) or self._source_from_value(
                getattr(source_result, "content", None)
            )
            if source:
                return source

        package_name = object_info.get("package")
        if not package_name or object_type != "INCL":
            return None
        grep_arguments: Dict[str, Any] = {
            "packageName": package_name,
            "pattern": self.datasource,
            "regex": False,
            "caseSensitive": False,
            "recursive": False,
            "objectTypes": ["PROG/I"],
            "contextLines": 100,
            "maxObjects": 1000,
            "maxMatches": 100,
        }
        if self.destination:
            grep_arguments["destination"] = self.destination
        grep_result = await self._call_tool(self.grep_tool.name, grep_arguments)
        if grep_result is None or getattr(grep_result, "is_error", False):
            return None
        return self._grep_source(getattr(grep_result, "structured_content", None), object_name) or self._grep_source(
            getattr(grep_result, "content", None),
            object_name,
        )


def write_report(report: EnhancementReport, output_dir: str = "sap") -> Path:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path = output_path / f"{report.datasource}_enhancement.json"
    report_path.write_text(report_as_json(report), encoding="utf-8")
    return report_path


async def main() -> None:
    datasource = environ.get("SAP_DATASOURCE", "0ACCT_TYPE_TEXT").strip().upper()
    objvers = environ.get("SAP_OBJVERS", "A").strip().upper()
    max_depth = int(environ.get("SAP_ENHANCEMENT_MAX_DEPTH", "8"))
    mcp_tools = MCPTools(
        url=environ["SAP_MCP_URL"],
        transport="streamable-http",
        headers={"Authorization": f"Bearer {environ['SAP_MCP_TOKEN']}"},
        timeout_seconds=120,
    )

    await mcp_tools.connect()
    try:
        source_reader = MCPRepositorySourceReader(mcp_tools, datasource)
        await source_reader.initialize()
        report = await analyze_datasource_enhancement(
            datasource=datasource,
            objvers=objvers,
            max_depth=max_depth,
            source_reader=source_reader.read,
        )
        report_path = write_report(report)
        print(report_as_json(report))
        print(f"FINAL OUTPUT (JSON report): {report_path}")
    finally:
        await mcp_tools.close()


if __name__ == "__main__":
    asyncio.run(main())