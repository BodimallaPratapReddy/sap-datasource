import unittest
from unittest.mock import AsyncMock, patch

from scripts.enhancement_analysis import (
    analyze_datasource_enhancement,
    enhancement_include_for_type,
)


class EnhancementAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def test_routes_datasource_types_to_customer_exit_includes(self):
        self.assertEqual(enhancement_include_for_type("TRAN"), "ZXRSAU01")
        self.assertEqual(enhancement_include_for_type("attr"), "ZXRSAU02")
        self.assertEqual(enhancement_include_for_type("TEXT"), "ZXRSAU03")
        self.assertIsNone(enhancement_include_for_type("HIER"))

    @patch("scripts.enhancement_analysis.run_query_rest", new_callable=AsyncMock)
    @patch("scripts.enhancement_analysis.fetch_roosource", new_callable=AsyncMock)
    async def test_scans_only_requested_datasource_branch_and_follows_local_form(
        self,
        fetch_roosource,
        run_query_rest,
    ):
        fetch_roosource.return_value = [{"TYPE": "TRAN"}]
        include_source = """
CASE i_datasource.
  WHEN 'ZSALES_DS'.
    PERFORM enhance_sales.
  WHEN 'ZOTHER_DS'.
    SELECT * FROM mara INTO TABLE lt_mara.
ENDCASE.

FORM enhance_sales.
  SELECT SINGLE zzvalue FROM zsales_ext INTO ls_output-zzsales
    WHERE vbeln = ls_output-vbeln.
  ls_output-zzstatus = zsales_ext-zzstatus.
ENDFORM.
"""
        source_reader = AsyncMock(side_effect=lambda name, object_type: include_source)

        async def ddic_rows(sql):
            if "FROM DD02L" in sql:
                return [{"TABNAME": "ZSALES_EXT", "AS4LOCAL": "A", "TABCLASS": "TRANSP"}]
            if "FROM DD02T" in sql:
                return [{"TABNAME": "ZSALES_EXT", "DDLANGUAGE": "E", "DDTEXT": "Sales extension"}]
            if "FROM DD03L" in sql:
                return [{"TABNAME": "ZSALES_EXT", "FIELDNAME": "VBELN", "KEYFLAG": "X"}]
            return []

        run_query_rest.side_effect = ddic_rows
        report = await analyze_datasource_enhancement("ZSALES_DS", source_reader)

        self.assertTrue(report.datasource_branch_found)
        self.assertEqual(report.enhancement_include, "ZXRSAU01")
        self.assertEqual([table.name for table in report.tables], ["ZSALES_EXT"])
        self.assertNotIn("MARA", [table.name for table in report.tables])
        self.assertEqual(report.tables[0].primary_key, ["VBELN"])
        self.assertIn("ZZSTATUS", [field.name for field in report.enhanced_fields])
        self.assertIn("FORM:ENHANCE_SALES", report.source_objects[1])

    @patch("scripts.enhancement_analysis.run_query_rest", new_callable=AsyncMock)
    @patch("scripts.enhancement_analysis.fetch_roosource", new_callable=AsyncMock)
    async def test_follows_nested_include_from_datasource_branch(
        self,
        fetch_roosource,
        run_query_rest,
    ):
        fetch_roosource.return_value = [{"type": "ATTR"}]
        sources = {
            ("ZXRSAU02", "INCL"): "CASE i_datasource. WHEN 'ZMAT_ATTR'. INCLUDE zmat_attr_impl. ENDCASE.",
            ("ZMAT_ATTR_IMPL", "INCL"): "SELECT SINGLE * FROM zmat_enrich INTO ls_data.",
        }
        source_reader = AsyncMock(side_effect=lambda name, object_type: sources.get((name, object_type)))

        async def ddic_rows(sql):
            if "FROM DD02L" in sql:
                return [{"TABNAME": "ZMAT_ENRICH", "AS4LOCAL": "A", "TABCLASS": "TRANSP"}]
            if "FROM DD02T" in sql:
                return [{"TABNAME": "ZMAT_ENRICH", "DDLANGUAGE": "E", "DDTEXT": "Material enrichment"}]
            if "FROM DD03L" in sql:
                return []
            return []

        run_query_rest.side_effect = ddic_rows
        report = await analyze_datasource_enhancement("ZMAT_ATTR", source_reader)

        self.assertEqual(report.enhancement_include, "ZXRSAU02")
        self.assertEqual([table.name for table in report.tables], ["ZMAT_ENRICH"])
        self.assertEqual(report.source_objects, ["INCL:ZXRSAU02", "INCL:ZMAT_ATTR_IMPL"])


if __name__ == "__main__":
    unittest.main()