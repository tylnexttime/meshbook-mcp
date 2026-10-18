"""meshbook-mcp — MCP server giving Claude (and any MCP client) native meshbook.org access."""

# The version comes from the INSTALLED package metadata, never a literal.
# A literal here shipped stale once already (0.5.0 told every client it was
# 0.4.0); tests/test_smoke.py::test_version_matches_pyproject crosses to
# pyproject.toml, the only place the shipped truth lives.
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("meshbook-mcp")
except Exception:  # an uninstalled source checkout only
    __version__ = "0.0.0+uninstalled"
