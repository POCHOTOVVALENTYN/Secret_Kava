# app/bot/states/admin.py
from aiogram.fsm.state import State, StatesGroup


class AdminMenuFSM(StatesGroup):
    """States for the task-oriented admin panel."""
    # Navigation
    MainMenu = State()
    PricesMenu = State()
    RegistriesMenu = State()

    # Price editing flow
    SelectItem = State()       # selecting specialist / room / event
    ViewPrices = State()       # viewing price card
    EnterNewPrice = State()    # typing new price value

    # Registry viewing
    ViewingRegistry = State()

    # Review moderation
    ModeratingReviews = State()

    # Slot management flow
    ManageSlots = State()
    SelectSlotDate = State()
    EnterSlotTime = State()


