from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    """Enable the PostgreSQL ``vector`` extension (pgvector)."""

    initial = True

    dependencies = []

    operations = [
        VectorExtension(),
    ]
