from aiogram.fsm.state import State, StatesGroup


class RegistrationStates(StatesGroup):
    species = State()
    name = State()
    birthday = State()


class CodeStates(StatesGroup):
    waiting = State()


class NewClassStates(StatesGroup):
    day = State()
    time = State()
    title = State()
    code = State()
    confirm = State()
