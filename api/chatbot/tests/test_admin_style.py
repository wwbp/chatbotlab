"""
The admin's Swiss-style stylesheet must load on every admin page, once.

It used to load only via BaseAdmin.Media, so the index, login and auth pages
were unstyled; templates/admin/base_site.html now adds it to every page. The
file lives in the chatbot app's static dir so collectstatic finds it even
when DEBUG is off (api/static is only a STATICFILES_DIRS entry under DEBUG).
"""

import pytest
from django.contrib.staticfiles.finders import AppDirectoriesFinder
from django.urls import reverse

STYLESHEET = "admin/css/custom_admin.css"
HREF = f'href="/static/{STYLESHEET}"'


def test_stylesheet_is_found_by_the_app_finder_regardless_of_debug():
    assert AppDirectoriesFinder().find(STYLESHEET)


@pytest.mark.django_db
def test_login_page_loads_stylesheet_once(client):
    response = client.get(reverse("admin:login"))

    assert response.content.decode().count(HREF) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name", ["admin:index", "admin:chatbot_persona_changelist"]
)
def test_admin_pages_load_stylesheet_once(admin_client, url_name):
    response = admin_client.get(reverse(url_name))

    assert response.status_code == 200
    assert response.content.decode().count(HREF) == 1
