# app/bot/states/booking.py
from aiogram.fsm.state import State, StatesGroup

class BookingFSM(StatesGroup):
    """Finite State Machine states representing steps of booking a consultation."""
    SelectPsychologist = State()
    SelectFormat = State()
    SelectDate = State()
    SelectSlot = State()
    EnterName = State()
    EnterPhone = State()
    ConfirmAndPay = State()


class RoomRentalFSM(StatesGroup):
    """Finite State Machine states representing steps of hourly room rentals."""
    SelectRoom = State()
    SelectDate = State()
    SelectSlot = State()
    SelectDuration = State()
    EnterName = State()
    EnterPhone = State()
    ConfirmAndPay = State()


class EventFSM(StatesGroup):
    """Finite State Machine states representing registering to a studio event."""
    SelectEvent = State()
    EnterName = State()
    EnterPhone = State()
    ConfirmAndPay = State()


class SpaceLeaseFSM(StatesGroup):
    """Finite State Machine states representing space lease lead generation."""
    EnterName = State()
    EnterPhone = State()


class WomensCircleFSM(StatesGroup):
    """Finite State Machine states representing Women's Circle registration."""
    SelectDate = State()
    EnterName = State()
    EnterPhone = State()
    ConfirmAndPay = State()


class HostEventFSM(StatesGroup):
    """Finite State Machine states for hosting an event registration."""
    EnterTitle = State()
    EnterHost = State()
    SelectDate = State()
    SelectSlot = State()
    SelectDuration = State()
    EnterLimit = State()
    EnterPrice = State()
    EnterName = State()
    EnterPhone = State()
    ConfirmAndPay = State()
