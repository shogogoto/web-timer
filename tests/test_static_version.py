from pathlib import Path

from app.main import STATIC_VERSION, templates


def test_static_assets_use_content_version():
    assert len(STATIC_VERSION) == 12
    assert all(character in "0123456789abcdef" for character in STATIC_VERSION)
    assert "style.css?v={{ static_version }}" in templates.env.loader.get_source(templates.env, "base.html")[0]
    assert "htmx.min.js?v={{ static_version }}" in templates.env.loader.get_source(templates.env, "base.html")[0]
    assert "app.js?v={{ static_version }}" in templates.env.loader.get_source(templates.env, "timer.html")[0]


def test_activity_navigation_uses_partial_htmx_updates():
    for template_name in ("timer.html", "admin_user.html"):
        source = templates.env.loader.get_source(templates.env, template_name)[0]
        assert 'id="activity-grid"' in source
        assert 'hx-select="#activity-grid"' in source
        assert 'hx-push-url="true"' in source


def test_timer_exposes_keyboard_controls_without_interfering_with_forms():
    template = templates.env.loader.get_source(templates.env, "timer.html")[0]
    script = (Path(templates.env.loader.searchpath[0]).parent / "static" / "app.js").read_text()
    assert '@keydown.window="handleTimerHotkey($event)"' in template
    assert "event.repeat" in script
    assert "target.closest('input, textarea, select, button, a')" in script
    assert "this.phase === 'running'" in script and "await this.pause()" in script
    assert "this.phase === 'paused'" in script and "await this.resume()" in script
