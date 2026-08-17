from dataclasses import dataclass
from enum import StrEnum

from app.policy.models import DistanceEvidenceBasis
from app.routing.models import RouteQuery


class TripPattern(StrEnum):
    ROUND_TRIP = "ROUND_TRIP"
    OUTBOUND_ONLY_END_AFTER_SCHEDULE = "OUTBOUND_ONLY_END_AFTER_SCHEDULE"
    RETURN_ONLY_DIRECT_TO_DESTINATION = "RETURN_ONLY_DIRECT_TO_DESTINATION"


class RouteDirection(StrEnum):
    OUTBOUND = "OUTBOUND"
    RETURN = "RETURN"


@dataclass(frozen=True)
class PlannedTripLeg:
    direction: RouteDirection
    query: RouteQuery


__all__ = [
    "DistanceEvidenceBasis",
    "PlannedTripLeg",
    "RouteDirection",
    "TripPattern",
]
