"""Tests for HTML session export."""

from qanot.export_html import render_session_html


class TestRenderSessionHTML:
    """Test HTML rendering of conversation messages."""

    def test_basic_roundtrip(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        html = render_session_html(msgs, bot_name="TestBot", model="test-model")
        assert "<!DOCTYPE html>" in html
        assert "TestBot" in html
        assert "test-model" in html
        assert "Hello" in html
        assert "Hi there!" in html
        assert "1 turn" in html

    def test_code_blocks_rendered(self):
        msgs = [
            {"role": "assistant", "content": "```python\nprint('hi')\n```"},
        ]
        html = render_session_html(msgs)
        assert "code-block" in html
        assert "python" in html
        assert "print(&#x27;hi&#x27;)" in html  # escaped

    def test_inline_code(self):
        msgs = [{"role": "user", "content": "Use `variable` here"}]
        html = render_session_html(msgs)
        assert '<code class="inline">variable</code>' in html

    def test_bold_and_italic(self):
        msgs = [{"role": "user", "content": "**bold** and *italic*"}]
        html = render_session_html(msgs)
        assert "<strong>bold</strong>" in html
        assert "<em>italic</em>" in html

    def test_structured_content(self):
        msgs = [{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Searching..."},
                {"type": "tool_use", "name": "web_search", "input": {}},
            ],
        }]
        html = render_session_html(msgs)
        assert "Searching..." in html
        assert "[Tool: web_search]" in html

    def test_empty_messages_skipped(self):
        msgs = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "Real reply"},
        ]
        html = render_session_html(msgs)
        assert "Real reply" in html
        # Should only have one message div
        assert html.count('class="message') == 1

    def test_html_escaping(self):
        msgs = [{"role": "user", "content": "<script>alert('xss')</script>"}]
        html = render_session_html(msgs)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_dark_theme_styles(self):
        html = render_session_html([{"role": "user", "content": "hi"}])
        assert "#0d1117" in html  # dark background
        assert "code-bg" in html

    def test_turn_count(self):
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        html = render_session_html(msgs)
        assert "2 turn" in html
        assert "4 xabar" in html
