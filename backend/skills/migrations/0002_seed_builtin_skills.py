"""Seed the built-in skills (docs/skills.md 2. fejezet).

``workspace=None`` + ``is_builtin=True`` marks a global seed skill. The seed is
idempotent (``get_or_create`` by slug) and reversible: the reverse migration
deletes exactly the two seeded slugs and nothing else.
"""

from django.db import migrations

BUILTIN_SKILLS = [
    {
        "slug": "phone-holder",
        "name": "Telefontartó",
        "kind": "template",
        "template_key": "phone_holder",
        "object_kind": "phone_holder",
        "description": "Beépített telefontartó sablon.",
        "guidance": "",
        "defaults_json": {},
        "constraints_json": {},
    },
    {
        "slug": "cookie-cutter",
        "name": "Süti kinyomó",
        "kind": "guidance",
        "template_key": "",
        "object_kind": "cookie_cutter",
        "description": "Vékony falú süti kinyomó.",
        "guidance": (
            "A süti kinyomó vékony falú (kb. 1.2 mm), 2D körvonalból extrudált "
            "alak, 20-30 mm fal\u00admagassággal, lekerekített felső éllel; "
            "opcionálisan fogantyú a tetején. A darab a tálcán álljon "
            "(min Z = 0)."
        ),
        "defaults_json": {"wall_thickness": 1.2, "material": "PLA"},
        "constraints_json": {
            "min_wall_mm": 1.2,
            "must_rest_on_plate": True,
            "require_primitives": ["extrude"],
        },
    },
]

SEED_SLUGS = [skill["slug"] for skill in BUILTIN_SKILLS]


def seed_builtin_skills(apps, schema_editor):
    skill_model = apps.get_model("skills", "Skill")
    for entry in BUILTIN_SKILLS:
        defaults = {key: value for key, value in entry.items() if key != "slug"}
        defaults["is_builtin"] = True
        skill_model.objects.get_or_create(slug=entry["slug"], defaults=defaults)


def remove_builtin_skills(apps, schema_editor):
    skill_model = apps.get_model("skills", "Skill")
    skill_model.objects.filter(slug__in=SEED_SLUGS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skills", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_builtin_skills, remove_builtin_skills),
    ]
