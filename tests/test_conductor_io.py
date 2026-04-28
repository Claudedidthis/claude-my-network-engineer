"""Stage 0 conformance: the existing _CliRenderer must satisfy ConductorIO,
and the runtime-checkable Protocol must accept any duck-typed implementation
that provides the right shape.

This test guards the structural prep for Stage 2 (WebSocket adapter). If a
future change breaks the contract — e.g. drops the `mode` attribute or
renames a callback — this test fails before any web adapter would.
"""
from __future__ import annotations

from typing import Any

from network_engineer.agents.conductor import _CliRenderer
from network_engineer.tools.conductor_io import ConductorIO


def test_cli_renderer_satisfies_conductor_io_protocol() -> None:
    renderer = _CliRenderer()
    # runtime_checkable Protocol — isinstance() does structural check.
    assert isinstance(renderer, ConductorIO)
    assert renderer.mode == "cli"


def test_arbitrary_duck_typed_class_satisfies_protocol() -> None:
    """Any object with the right callables + a `mode` attribute is a
    ConductorIO. This is what lets tests / embedders pass a minimal stub."""

    class _Stub:
        mode = "web"

        def on_say(self, text: str) -> None:
            pass

        def on_user_input(self, prompt: str) -> str:
            return ""

        def on_status(self, event: dict[str, Any]) -> None:
            pass

    assert isinstance(_Stub(), ConductorIO)


def test_missing_member_fails_protocol_check() -> None:
    """Negative case: missing required members should NOT be recognized
    as a ConductorIO. Both attributes and methods are checked."""

    class _MissingMode:
        # No `mode` attribute set.
        def on_say(self, text: str) -> None:
            pass

        def on_user_input(self, prompt: str) -> str:
            return ""

        def on_status(self, event: dict[str, Any]) -> None:
            pass

    assert not isinstance(_MissingMode(), ConductorIO), (
        "Protocol must reject implementations missing the `mode` attribute"
    )

    class _MissingMethod:
        mode = "cli"

        def on_say(self, text: str) -> None:
            pass

        def on_user_input(self, prompt: str) -> str:
            return ""
        # Missing on_status entirely.

    assert not isinstance(_MissingMethod(), ConductorIO), (
        "Protocol must reject implementations missing on_status"
    )
