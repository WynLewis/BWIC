from enum import Enum, IntEnum


class Compounding(IntEnum):
    SIMPLE = 0
    COMPOUNDED = 1
    CONTINUOUS = 2
    SIMPLE_THEN_COMPOUNDED = 3


class Frequency(IntEnum):
    NO_FREQUENCY = -1
    ONCE = 0
    ANNUAL = 1
    SEMIANNUAL = 2
    EVERY_FOURTH_MONTH = 3
    QUARTERLY = 4
    BIMONTHLY = 6
    MONTHLY = 12
    EVERY_FOURTH_WEEK = 13
    BIWEEKLY = 26
    WEEKLY = 52
    DAILY = 365
    OTHER = 999


class CouponType(str, Enum):
    FIXED = "Fixed"
    FLOATING = "Floating"
    TRANCHE_WAC = "TrancheWac"
    FORMULA = "Formula"
    RESIDUAL_INTEREST = "ResidualInterest"


class CashflowType(str, Enum):
    PI = "PI"
    IO = "IO"
    PO = "PO"
    EXPENSE = "Expense"
    RESERVE = "Reserve"


class InterestRateType(str, Enum):
    FRM = "FRM"
    ARM = "ARM"
    STEP = "STEP"
    IO = "IO"


class TrancheType(str, Enum):
    OFFERED = "Offered"
    CERTIFICATE = "Certificate"
    RESIDUAL = "Residual"


class PrepaymentType(str, Enum):
    CPR = "CPR"
    ABS = "ABS"
    SMM = "SMM"


class DayCountConvention(str, Enum):
    THIRTY_360 = "30/360"
    ACTUAL_360 = "Actual360"
    ACTUAL_365 = "Actual365"
    ACTUAL_ACTUAL_ISDA = "ActualActualISDA"
    ACTUAL_ACTUAL_ISMA = "ActualActualISMA"


class WaterfallOrder(str, Enum):
    STANDARD = "Standard"
    INTEREST_FIRST = "InterestFirst"
    PRINCIPAL_FIRST = "PrincipalFirst"


class InterestTreatment(str, Enum):
    COLLATERAL = "Collateral"
    GUARANTEED = "Guaranteed"


class BusinessDayConvention(str, Enum):
    FOLLOWING = "Following"
    MODIFIED_FOLLOWING = "ModifiedFollowing"
    PRECEDING = "Preceding"
    UNADJUSTED = "Unadjusted"
