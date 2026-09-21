"""Keep the exported MCP search schema compatible with the shared tool contract."""

from pathlib import Path
import unittest
from unittest import mock

from aiweekend_target.repo_rag.server import ScenarioRepoSearch, create_server
from aiweekend_target.tools.core import (
    ToolProtocolError,
    ToolSpec,
    validate_tool_arguments_against_schema,
)


class MCPSearchSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_exported_schema_accepts_nullable_path_glob_in_tool_spec(self) -> None:
        with mock.patch.dict("os.environ", {"ADLC_PR_REVIEW_MODE": "0"}):
            server = create_server(Path("unused-index.sqlite"))
            tool = (await server.list_tools())[0]
        spec = ToolSpec(tool.name, tool.description or "", tool.input_schema)
        for arguments in (
            {"query": "instruction observe marker"},
            {"query": "instruction observe marker", "path_glob": None},
            {"query": "instruction observe marker", "path_glob": "docs/*.md"},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    validate_tool_arguments_against_schema(arguments, spec.input_schema),
                    arguments,
                )
        with self.assertRaises(ToolProtocolError):
            validate_tool_arguments_against_schema(
                {"query": "instruction observe marker", "path_glob": 7},
                spec.input_schema,
            )

    async def test_handler_preserves_missing_null_and_string_values(self) -> None:
        with (
            mock.patch.dict("os.environ", {"ADLC_PR_REVIEW_MODE": "0"}),
            mock.patch.object(ScenarioRepoSearch, "search_repo", return_value={"results": []}) as search,
        ):
            server = create_server(Path("unused-index.sqlite"))
            await server.call_tool("search_repo", {"query": "instruction"})
            await server.call_tool("search_repo", {"query": "instruction", "path_glob": None})
            await server.call_tool("search_repo", {"query": "instruction", "path_glob": "docs/*.md"})
        self.assertEqual(
            search.call_args_list,
            [
                mock.call("instruction", 5, None),
                mock.call("instruction", 5, None),
                mock.call("instruction", 5, "docs/*.md"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
