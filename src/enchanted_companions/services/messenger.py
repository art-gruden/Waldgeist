"""Composes the bot's recurring messages (profile, wake-up, level-up) from content texts."""

from __future__ import annotations

from enchanted_companions.content import Content
from enchanted_companions.db.models import Pet
from enchanted_companions.domain.progression import Progress
from enchanted_companions.domain.sleep import SleepState, shows_sleep_image
from enchanted_companions.services.notifier import MediaLibrary, Notifier
from enchanted_companions.services.pets import ProfileView
from enchanted_companions.services.reward_engine import LevelUp
from enchanted_companions.settings import GameConfig


def progress_bar(progress: Progress, thresholds: list[int], width: int = 10) -> str:
    if progress.is_max:
        return "▰" * width
    floor = thresholds[progress.level - 1]
    span = max(1, (progress.next_threshold or floor) - floor)
    filled = max(0, min(width, round((progress.energy - floor) / span * width)))
    return "▰" * filled + "▱" * (width - filled)


class Messenger:
    def __init__(self, notifier: Notifier, media: MediaLibrary, content: Content, cfg: GameConfig) -> None:
        self.notifier, self.media, self.content, self.cfg = notifier, media, content, cfg
        self.texts = content.texts

    def species_name(self, pet: Pet) -> str:
        info = self.content.species_by_slug(pet.species_id)
        return info.name if info else pet.species_id

    def emoji(self, pet: Pet) -> str:
        info = self.content.species_by_slug(pet.species_id)
        return info.emoji if info else "✨"

    def stage_name(self, stage: int) -> str:
        return self.texts.render(f"stages.s{stage}")

    def label(self, pet: Pet) -> str:
        """Emoji + escaped pet name, ready for HTML."""
        return self.texts.render("pet_label", emoji=self.emoji(pet), name=pet.name)

    def profile_caption(self, view: ProfileView) -> str:
        p = view.progress
        if p.is_max:
            progress_line = self.texts.render("profile.max_level")
        else:
            progress_line = self.texts.render(
                "profile.progress",
                bar=progress_bar(p, self.cfg.levels.thresholds),
                next_level=p.level + 1,
                left=p.to_next,
            )
        return self.texts.render(
            "profile.card",
            html_values={"label": self.label(view.pet), "progress_line": progress_line},
            species=self.species_name(view.pet),
            level=p.level,
            stage=self.stage_name(p.stage),
            energy=p.energy,
            state=self.texts.render(f"sleep_state.{view.state.value}", name=view.pet.name),
        )

    def profile_image(self, view: ProfileView):
        return self.media.pet_image(view.pet.species_id, view.progress.stage, shows_sleep_image(view.state))

    def wake_text(self, pet: Pet, previous: SleepState) -> str:
        info = self.content.species_by_slug(pet.species_id)
        return self.texts.render(
            f"wake.{previous.value}", name=pet.name, species=self.species_name(pet), trait=info.trait if info else ""
        )

    async def send_wake(self, chat_id: int, pet: Pet, previous: SleepState) -> None:
        await self.notifier.text(chat_id, self.wake_text(pet, previous))

    async def announce_level_up(self, pet: Pet, level_up: LevelUp, owner_tg_id: int) -> None:
        stage = self.stage_name(level_up.new_stage)
        if level_up.evolved:
            image = self.media.pet_image(pet.species_id, level_up.new_stage)
            dm = self.texts.render("level_up.evolved_dm", name=pet.name, level=level_up.new_level, stage=stage)
            group = self.texts.render(
                "level_up.evolved_group",
                html_values={"label": self.label(pet)},
                level=level_up.new_level,
                stage=stage,
            )
            await self.notifier.photo(owner_tg_id, image, dm)
            await self.notifier.group_photo(image, group)
        else:
            await self.notifier.text(
                owner_tg_id, self.texts.render("level_up.dm", name=pet.name, level=level_up.new_level)
            )
            await self.notifier.group_text(
                self.texts.render("level_up.group", html_values={"label": self.label(pet)}, level=level_up.new_level)
            )
