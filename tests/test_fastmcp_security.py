"""Regression for GHSA-7xgj-8392-5q4r using only an in-process server."""
import anyio
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from fastmcp.server.auth import restrict_tag
from fastmcp.server.transforms.tool_transform import ToolTransform
from fastmcp.tools.base import Tool
from fastmcp.tools.tool_transform import ToolTransformConfig


def test_transformed_restricted_tool_cannot_be_called_without_scope():
    async def synthetic_tool():
        return "synthetic"

    async def check():
        server = FastMCP("authorization-regression")
        server.add_tool(Tool.from_function(synthetic_tool, name="sample", tags={"public"},
                                           auth=restrict_tag("admin", scopes=["admin"])))
        server.add_transform(ToolTransform({"sample": ToolTransformConfig(tags={"admin"})}))
        assert not await server.list_tools()
        with pytest.raises(NotFoundError):
            await server.call_tool("sample")

    anyio.run(check)
