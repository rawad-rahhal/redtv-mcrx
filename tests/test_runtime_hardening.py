"""Regression tests for production-path defects found during v4.16 hardening."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api_gateway.app import create_app
from api_gateway.routers.control import _resolve_playlist_input_path
from automation_ai.main import _resolve_playlist_output_path
from shared_contracts.models import Playlist, PlaylistItem, SlotType
from shared_contracts.preflight import preflight_playlist
from playout_core.output_adapters import PreviewWindowAdapter


def test_real_fastapi_lifespan_starts_and_stops_engine():
    """The real application lifespan must start, serve health, and shut down."""
    app = create_app()
    with TestClient(app) as client:
        response = client.get('/health')
        assert response.status_code == 200
        assert app.state.engine._running is True
    assert app.state.engine._running is False


def test_guard_runtime_endpoints_use_property_api():
    app = create_app()
    with TestClient(app) as client:
        snap = client.get('/api/guard/snapshot')
        assert snap.status_code == 200
        data = snap.json()
        assert data['ok'] is True
        assert isinstance(data['violation_count'], int)
        assert isinstance(data['recent'], list)

        ack = client.post('/api/guard/ack')
        assert ack.status_code == 200
        ack_data = ack.json()
        assert ack_data['ok'] is True
        assert isinstance(ack_data['ack']['acked_violation_count'], int)


def test_preflight_does_not_require_media_path_for_break_slot():
    playlist = Playlist(items=[PlaylistItem(slot_type=SlotType.BREAK, title='Commercial break')])
    report = preflight_playlist(playlist, {'paths': {'root': ''}})
    assert report.ok is True
    assert report.errors == []


def test_preflight_still_checks_existing_optional_media_paths(tmp_path: Path):
    missing = tmp_path / 'missing.mp4'
    playlist = Playlist(items=[
        PlaylistItem(slot_type=SlotType.BREAK, title='Break with optional media', media_path=str(missing))
    ])
    report = preflight_playlist(playlist, {'paths': {'root': ''}})
    assert report.ok is False
    assert str(missing) in report.missing_media


@pytest.mark.parametrize('bad_name', [
    '../escape.json',
    '..\\escape.json',
    '/tmp/escape.json',
    r'C:\\temp\\escape.json',
    r'C:escape.json',
    'nested/escape.json',
    'nested\\escape.json',
    '',
    '.',
    '..',
])
def test_playlist_output_path_rejects_traversal_and_absolute_paths(tmp_path: Path, bad_name: str):
    with pytest.raises(ValueError):
        _resolve_playlist_output_path(str(tmp_path), bad_name)


def test_playlist_output_path_accepts_simple_filename(tmp_path: Path):
    resolved = Path(_resolve_playlist_output_path(str(tmp_path), 'playlist_ai.json'))
    assert resolved == (tmp_path / 'playlist_ai.json').resolve()
    assert resolved.parent == tmp_path.resolve()


def test_preview_adapter_start_stop_are_idempotent():
    adapter = PreviewWindowAdapter()
    adapter.start()
    first_worker = adapter._worker
    adapter.start()
    assert adapter._worker is first_worker
    adapter.stop()
    assert adapter._running is False
    assert adapter._worker is None
    adapter.stop()


@pytest.mark.parametrize('bad_identifier', [
    '../secret.json',
    '..\\secret.json',
    '/etc/passwd',
    r'C:\Users\operator\secret.json',
    r'C:secret.json',
    'nested/playlist.json',
    'nested\\playlist.json',
    '',
    '.',
    '..',
])
def test_playlist_input_path_rejects_host_paths(tmp_path: Path, bad_identifier: str):
    config = {'paths': {'playlist_dir': str(tmp_path)}}
    with pytest.raises(ValueError):
        _resolve_playlist_input_path(config, bad_identifier)


def test_playlist_input_path_accepts_filename_inside_configured_directory(tmp_path: Path):
    config = {'paths': {'playlist_dir': str(tmp_path)}}
    resolved = _resolve_playlist_input_path(config, 'today.json')
    assert resolved == (tmp_path / 'today.json').resolve()


def test_playlist_input_path_rejects_symlink_escape(tmp_path: Path):
    playlist_dir = tmp_path / 'playlists'
    playlist_dir.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_text('{"items": []}')
    link = playlist_dir / 'escape.json'
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks are unavailable in this environment')

    config = {'paths': {'playlist_dir': str(playlist_dir)}}
    with pytest.raises(ValueError):
        _resolve_playlist_input_path(config, 'escape.json')


def test_load_file_api_rejects_absolute_path_before_reading(tmp_path: Path):
    secret = tmp_path / 'secret.json'
    secret.write_text('{"token": "DO_NOT_ECHO"}')

    app = create_app()
    with TestClient(app) as client:
        response = client.post('/api/playlist/load_file', params={'path': str(secret)})

    assert response.status_code == 400
    assert 'DO_NOT_ECHO' not in response.text


def test_load_file_schema_error_does_not_echo_file_values(tmp_path: Path):
    playlist_dir = tmp_path / 'playlists'
    playlist_dir.mkdir()
    bad = playlist_dir / 'bad.json'
    bad.write_text('{"items": [{"slot_type": "SECRET_VALUE", "media_path": "ignored.mp4"}]}')

    app = create_app()
    app.state.config = dict(app.state.config)
    app.state.config['paths'] = dict(app.state.config.get('paths', {}))
    app.state.config['paths']['playlist_dir'] = str(playlist_dir)
    with TestClient(app) as client:
        response = client.post('/api/playlist/load_file', params={'path': 'bad.json'})

    # The file is inside the allowed directory, but any schema/preflight failure
    # must not reflect sensitive file contents in the response.
    assert response.status_code in {422}
    assert 'SECRET_VALUE' not in response.text


def test_dropin_rejects_missing_program_media_before_engine(tmp_path: Path):
    """The time-critical drop-in path must not bypass broadcast preflight."""
    missing = tmp_path / 'missing-program.mp4'
    app = create_app()

    with TestClient(app) as client:
        # If preflight is working, the engine must never receive this item.
        calls = []
        original = app.state.engine.drop_in_next
        app.state.engine.drop_in_next = calls.append
        try:
            response = client.post('/api/dropin', json={
                'media_path': str(missing),
                'title': 'Unsafe drop-in',
                'duration_seconds': 10.0,
                'slot_type': 'program',
            })
        finally:
            app.state.engine.drop_in_next = original

    assert response.status_code == 422
    assert response.json()['detail'] == 'Drop-in failed broadcast preflight'
    assert calls == []
    assert str(missing) not in response.text


def test_dropin_accepts_preflighted_program_media(tmp_path: Path):
    """A valid drop-in still reaches the engine after deterministic preflight."""
    media = tmp_path / 'breaking-news.mp4'
    media.write_bytes(b'fixture')
    app = create_app()

    with TestClient(app) as client:
        calls = []
        original = app.state.engine.drop_in_next
        app.state.engine.drop_in_next = calls.append
        try:
            response = client.post('/api/dropin', json={
                'media_path': str(media),
                'title': 'Breaking news',
                'duration_seconds': 10.0,
                'slot_type': 'program',
            })
        finally:
            app.state.engine.drop_in_next = original

    assert response.status_code == 200
    data = response.json()
    assert data['status'] == 'accepted'
    assert data['guard_ok'] is True
    assert len(calls) == 1
    assert calls[0].media_path == str(media)
