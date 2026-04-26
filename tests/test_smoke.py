"""Smoke tests — verify package layout and basic imports.

This is the minimum bar for Phase 0: the package imports cleanly and the CLI is callable.
Real behavior tests land in later phases.
"""

from __future__ import annotations


def test_package_importable() -> None:
    import network_engineer

    assert hasattr(network_engineer, "__version__")
    assert isinstance(network_engineer.__version__, str)


def test_subpackages_importable() -> None:
    from network_engineer import agents, server, tools  # noqa: F401


def test_cli_main_callable() -> None:
    from network_engineer import cli

    assert callable(cli.main)
