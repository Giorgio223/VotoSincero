from aiogram.fsm.state import State, StatesGroup


class Reg(StatesGroup):
    name = State()
    age = State()
    city = State()
    bio = State()
    photo = State()
    gender = State()
    rate_pref = State()
    be_rated_by = State()


class EditProfile(StatesGroup):
    name = State()
    photo = State()
    gender = State()
    age = State()
    city = State()
    bio = State()
    be_rated_by = State()
    rate_pref = State()


class RateFlow(StatesGroup):
    rating = State()
    message = State()
