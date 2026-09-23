"""Tests for the server-rendered skills page (docs/skills.md 6.)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="ada", email="ada@example.com", password="pw")


@pytest.fixture
def auth_client(user):
    client = Client()
    client.force_login(user)
    return client


def test_skills_list_url_reverses():
    assert reverse("skills:list") == "/skills/"


def test_skills_page_requires_login():
    response = Client().get("/skills/")

    assert response.status_code == 302
    assert "/login/" in response.url


def test_skills_page_renders_the_list(auth_client):
    response = auth_client.get("/skills/")

    assert response.status_code == 200
    assert response.templates[0].name == "skills/list.html"
    assert "Skillek" in response.content.decode()
