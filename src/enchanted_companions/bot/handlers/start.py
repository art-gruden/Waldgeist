"""/start: registration and onboarding — species, name, optional birthday."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from enchanted_companions.bot import keyboards as kb
from enchanted_companions.bot.callbacks import PickSpecies, Registration
from enchanted_companions.bot.handlers.common import is_group_member, show_profile
from enchanted_companions.bot.states import RegistrationStates
from enchanted_companions.container import Container
from enchanted_companions.services.registration import (
    AlreadyRegistered,
    InvalidBirthday,
    InvalidName,
    SpeciesTaken,
    parse_birthday,
)

router = Router(name="start")


@router.message(CommandStart(), F.chat.type.in_({"group", "supergroup"}))
async def start_in_group(message: Message, container: Container) -> None:
    t = container.content.texts
    await message.reply(t.render("start.in_group"), reply_markup=kb.open_bot(t, container.bot_username))


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message, state: FSMContext, container: Container) -> None:
    c, t, user = container, container.content.texts, message.from_user
    if user is None:
        return
    await state.clear()
    _, pet = await c.registration.start(user.id, user.username, user.first_name)
    if pet is not None:
        await message.answer(t.render("start.welcome_back", name=pet.name))
        await show_profile(c, message.chat.id, user.id)
        return
    if not await is_group_member(c, user.id):
        await message.answer(t.render("start.not_member"))
        return
    await message.answer(t.render("start.intro", first_name=user.first_name))
    board = await c.registration.species_board()
    await message.answer(t.render("register.choose_species"), reply_markup=kb.species_picker(t, board))
    await state.set_state(RegistrationStates.species)


@router.callback_query(PickSpecies.filter(), RegistrationStates.species)
async def pick_species(
    query: CallbackQuery, callback_data: PickSpecies, state: FSMContext, container: Container
) -> None:
    c, t = container, container.content.texts
    species = c.content.species_by_slug(callback_data.slug)
    if species is None:
        await query.answer()
        return
    if await c.registration.is_taken(species.slug):
        await query.answer(t.render("register.species_taken_alert", species=species.name), show_alert=True)
        return
    await state.update_data(species=species.slug)
    await state.set_state(RegistrationStates.name)
    await query.answer()
    if query.message:
        await query.message.answer(
            t.render("register.ask_name", emoji=species.emoji, species=species.name, character=species.character)
        )


@router.callback_query(PickSpecies.filter())
async def pick_species_outside_flow(query: CallbackQuery, container: Container) -> None:
    await query.answer(container.content.texts.render("register.restart_hint"), show_alert=True)


@router.message(RegistrationStates.name, F.text, ~F.text.startswith("/"))
async def got_name(message: Message, state: FSMContext, container: Container) -> None:
    c, t = container, container.content.texts
    try:
        name = c.registration.clean_name(message.text or "")
    except InvalidName as exc:
        await message.answer(
            t.render(
                f"register.bad_name_{exc.args[0]}",
                min=c.cfg.registration.name_min_length,
                max=c.cfg.registration.name_max_length,
            )
        )
        return
    await state.update_data(name=name)
    await state.set_state(RegistrationStates.birthday)
    await message.answer(t.render("register.ask_birthday", name=name), reply_markup=kb.skip_birthday(t))


@router.message(RegistrationStates.birthday, F.text, ~F.text.startswith("/"))
async def got_birthday(message: Message, state: FSMContext, container: Container) -> None:
    try:
        birthday = parse_birthday(message.text or "")
    except InvalidBirthday:
        await message.answer(container.content.texts.render("register.bad_birthday"))
        return
    await _finish(message, state, container, birthday)


@router.callback_query(Registration.filter(F.action == "skip_birthday"), RegistrationStates.birthday)
async def skip_birthday(query: CallbackQuery, state: FSMContext, container: Container) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await _finish(query.message, state, container, None, from_user=query.from_user)


async def _finish(message: Message, state: FSMContext, c: Container, birthday, from_user=None) -> None:
    t = c.content.texts
    user = from_user or message.from_user
    data = await state.get_data()
    try:
        pet = await c.registration.register(
            user.id, user.username, user.first_name, data["species"], data["name"], birthday
        )
    except SpeciesTaken:
        await state.set_state(RegistrationStates.species)
        board = await c.registration.species_board()
        await message.answer(t.render("register.species_taken_late"), reply_markup=kb.species_picker(t, board))
        return
    except AlreadyRegistered:
        await state.clear()
        await show_profile(c, message.chat.id, user.id)
        return
    except (KeyError, InvalidName):
        await state.clear()
        await message.answer(t.render("register.restart_hint"))
        return
    await state.clear()
    await message.answer(t.render("register.done", name=pet.name, species=c.messenger.species_name(pet)))
    await show_profile(c, message.chat.id, user.id)
    await c.notifier.group_text(
        t.render(
            "register.group_welcome",
            html_values={"label": c.messenger.label(pet)},
            species=c.messenger.species_name(pet),
        )
    )


@router.message(Command("cancel"), F.chat.type == "private")
async def cancel(message: Message, state: FSMContext, container: Container) -> None:
    await state.clear()
    await message.answer(
        container.content.texts.render("common.cancelled"), reply_markup=kb.main_menu(container.content.texts)
    )
