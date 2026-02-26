"""Tests for ConnectionState enum."""

from soundbridge.state import ConnectionState


class TestConnectionState:

    def test_has_three_states(self):
        states = list(ConnectionState)
        assert len(states) == 3

    def test_states_are_distinct(self):
        assert ConnectionState.DISCONNECTED != ConnectionState.SEARCHING
        assert ConnectionState.SEARCHING != ConnectionState.CONNECTED
        assert ConnectionState.CONNECTED != ConnectionState.DISCONNECTED

    def test_expected_members(self):
        assert hasattr(ConnectionState, "DISCONNECTED")
        assert hasattr(ConnectionState, "SEARCHING")
        assert hasattr(ConnectionState, "CONNECTED")
