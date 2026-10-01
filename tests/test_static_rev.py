import re

from app.template_filters import make_static_rev


def test_static_rev_changes_with_file_content(tmp_path):
    css = tmp_path / "css" / "app.css"
    css.parent.mkdir()
    css.write_text("body{color:red}")
    rev = make_static_rev(str(tmp_path))
    first = rev("css/app.css")
    assert first == rev("css/app.css")
    css.write_text("body{color:blue}")
    assert rev("css/app.css") != first
    assert rev("css/fehlt.css") == "0"


def test_pages_reference_css_with_content_hash(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert re.search(r"/static/css/app\.css\?v=[0-9a-f]{12}\"", html)
