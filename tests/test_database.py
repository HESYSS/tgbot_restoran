from pathlib import Path

from booking_agent.database import ClientRegistry


def test_registry_keeps_profile_mode_and_audit(tmp_path: Path):
    registry = ClientRegistry(tmp_path / "booking-agent.db")
    profile = registry.register_client(10, 20, "Test Client")
    assert profile["sender_mode"] == "representative"
    registry.set_sender_mode(10, "personal")
    registry.save_connection(10, "personal", tmp_path / "sessions" / "10.session", active=True)
    assert registry.get_client(10)["sender_mode"] == "personal"
    assert any(item["event_type"] == "account_connected" for item in registry.recent_events(10))
