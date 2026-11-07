"""Keeps the species table in sync with content/uk/species.yaml (runs at every start)."""

from __future__ import annotations

from enchanted_companions.content import Content
from enchanted_companions.db.repositories import SpeciesRepo
from enchanted_companions.db.session import SessionFactory


async def seed_species(sm: SessionFactory, content: Content) -> None:
    rows = [
        {
            "id": sp.slug,
            "name": sp.name,
            "emoji": sp.emoji,
            "character": sp.character,
            "favorite_place": sp.favorite_place,
            "sort_order": sp.sort_order,
            "is_active": True,
        }
        for sp in content.species
    ]
    async with sm() as s, s.begin():
        await SpeciesRepo.upsert_all(s, rows)
