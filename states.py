from aiogram.fsm.state import StatesGroup, State

class Reg(StatesGroup):
    name = State()
    age = State()
    city = State()
    bio = State()
    gender = State()
    rate_pref = State()
    be_rated_by = State()
    photo = State()

class Rate(StatesGroup):
    waiting_score = State()
    waiting_message = State()

class Admin(StatesGroup):
    add_channel = State()
