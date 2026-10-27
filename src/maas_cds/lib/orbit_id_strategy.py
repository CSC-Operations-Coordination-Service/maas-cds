"""Strategies to walk the orbit identifiers of a mission

Spotting the orbits missing between two known ones, so a completely missing
orbit can be materialized and read as missing instead of not existing at all, is
needed in several places: the S3 and S5 completeness documents, keyed by
datatake identifier, and the S3 downlink sessions, keyed by downlink orbit.

Only the way an identifier is compared and incremented changes from one to the
other, so this module gathers those strategies and the missing orbit detection
is written once.
"""

import logging
import re
import typing
from datetime import datetime, timedelta, timezone

__all__ = [
    "OrbitIdStrategy",
    "AbsoluteOrbitIdStrategy",
    "CycleOrbitIdStrategy",
    "S3DatatakeIdStrategy",
    "S5DatatakeIdStrategy",
    "S3DownlinkOrbitStrategy",
]


LOGGER = logging.getLogger("OrbitIdStrategy")


class OrbitIdStrategy:
    """Arithmetic over the orbit identifiers of a mission

    An identifier holds an orbit counter, sometimes prefixed by the satellite it
    belongs to. Implementations tell how to read that counter (:meth:`rank`) and
    how to build the identifier of the next orbit (:meth:`next_id`); everything
    else is derived from those two.
    """

    # Identifier format, checked with re.search as the historical implementations did
    ID_REGEX_FORMAT = None

    # Duration of an orbit, used to check a gap of identifiers against the time
    # elapsed between its two ends. None skips that check.
    ORBIT_DURATION = None

    @classmethod
    def is_orbit_id(cls, orbit_id: str) -> bool:
        """Tell if an identifier respects the format of the strategy

        Args:
            orbit_id (str): the identifier

        Returns:
            bool: True if the identifier can take part in the orbit arithmetic
        """
        return isinstance(orbit_id, str) and bool(
            re.search(cls.ID_REGEX_FORMAT, orbit_id)
        )

    @classmethod
    def check_ids(cls, *orbit_ids: str):
        """Ensure identifiers respect the format of the strategy

        Args:
            *orbit_ids (str): the identifiers to check

        Raises:
            ValueError: if one of the identifiers does not respect the format
        """
        if not all(cls.is_orbit_id(orbit_id) for orbit_id in orbit_ids):
            raise ValueError("Inputs arguments does not respect the expected format")

    @classmethod
    def rank(cls, orbit_id: str) -> int:
        """Position of an identifier in the orbit sequence

        Args:
            orbit_id (str): the identifier

        Returns:
            int: an integer growing with the orbit, only meaningful when
                compared with the rank of another identifier of the same satellite
        """
        raise NotImplementedError()

    @classmethod
    def next_id(cls, orbit_id: str) -> str:
        """Identifier of the orbit following the given one

        Args:
            orbit_id (str): the identifier

        Returns:
            str: the identifier of the next orbit
        """
        raise NotImplementedError()

    @classmethod
    def sort_ids(cls, orbit_id_1: str, orbit_id_2: str) -> typing.Tuple[str, str]:
        """Sort two identifiers in ascending orbit order

        Args:
            orbit_id_1 (str): first identifier
            orbit_id_2 (str): second identifier

        Raises:
            ValueError: if one of the identifiers does not respect the format

        Returns:
            typing.Tuple[str, str]: the two identifiers, lowest orbit first
        """
        cls.check_ids(orbit_id_1, orbit_id_2)

        if cls.rank(orbit_id_2) >= cls.rank(orbit_id_1):
            return (orbit_id_1, orbit_id_2)

        return (orbit_id_2, orbit_id_1)

    @classmethod
    def ids_between(
        cls, orbit_id_1: str, orbit_id_2: str, descending: bool = False
    ) -> typing.List[str]:
        """All the identifiers strictly between two identifiers

        The two references are given in any order and are never part of the
        result.

        Args:
            orbit_id_1 (str): first identifier
            orbit_id_2 (str): second identifier
            descending (bool): return the identifiers from the highest orbit to
                the lowest instead of the natural ascending order

        Raises:
            ValueError: if one of the identifiers does not respect the format

        Returns:
            typing.List[str]: the identifiers between the two references
        """
        cls.check_ids(orbit_id_1, orbit_id_2)

        if orbit_id_1 == orbit_id_2:
            return []

        min_id, max_id = cls.sort_ids(orbit_id_1, orbit_id_2)

        LOGGER.debug("Generate missing orbit between %s and %s", min_id, max_id)

        max_rank = cls.rank(max_id)

        orbit_ids = []

        current_id = cls.next_id(min_id)

        while cls.rank(current_id) < max_rank:
            LOGGER.debug("- Add  missing %s", current_id)
            orbit_ids.append(current_id)
            current_id = cls.next_id(current_id)

        if descending:
            orbit_ids.reverse()

        return orbit_ids

    @classmethod
    def missing_ids(
        cls,
        previous_id: str,
        previous_start: datetime,
        current_id: str,
        current_start: datetime,
        descending: bool = False,
    ) -> typing.List[str]:
        """The identifiers missing between an orbit and the one before it in time

        Unlike :meth:`ids_between`, nothing is listed when the two identifiers
        can not be trusted to follow each other:

        - one of them does not respect the format, like the tagged identifiers
          of a period with an irregular numbering;
        - the numbering goes backwards while the time goes forward;
        - more identifiers are missing than the elapsed time can hold orbits.

        Args:
            previous_id (str): identifier of the previous orbit in time
            previous_start (datetime): start of the previous orbit
            current_id (str): identifier of the current orbit
            current_start (datetime): start of the current orbit
            descending (bool): return the identifiers from the highest orbit to
                the lowest instead of the natural ascending order

        Returns:
            typing.List[str]: the missing identifiers
        """
        if not (cls.is_orbit_id(previous_id) and cls.is_orbit_id(current_id)):
            LOGGER.info(
                "No missing orbit between %s and %s: out of the orbit numbering",
                previous_id,
                current_id,
            )
            return []

        if cls.rank(previous_id) >= cls.rank(current_id):
            LOGGER.warning(
                "No missing orbit between %s and %s: the numbering goes backwards",
                previous_id,
                current_id,
            )
            return []

        orbit_ids = cls.ids_between(previous_id, current_id, descending=descending)

        if cls.ORBIT_DURATION is not None and orbit_ids:
            max_missing = round((current_start - previous_start) / cls.ORBIT_DURATION)

            if len(orbit_ids) > max_missing:
                LOGGER.warning(
                    "No missing orbit between %s and %s: %s orbits can not fit in %s",
                    previous_id,
                    current_id,
                    len(orbit_ids),
                    current_start - previous_start,
                )
                return []

        return orbit_ids


class AbsoluteOrbitIdStrategy(OrbitIdStrategy):
    """Identifiers holding an absolute orbit counter

    The counter is the last part of the identifier, optionally prefixed by the
    satellite it belongs to: ``S5P-12345``, or ``052724`` for a bare counter.
    """

    # Separator between the prefix and the counter. An identifier without it is
    # a bare counter.
    SEPARATOR = "-"

    # Number of digits of the zero padded counter
    ORBIT_WIDTH = 5

    @classmethod
    def split_id(cls, orbit_id: str) -> typing.Tuple[str, int]:
        """Split an identifier into its prefix and its orbit counter

        Args:
            orbit_id (str): the identifier

        Returns:
            typing.Tuple[str, int]: the prefix, separator included, and the counter
        """
        prefix, separator, orbit = orbit_id.rpartition(cls.SEPARATOR)

        return f"{prefix}{separator}", int(orbit)

    @classmethod
    def rank(cls, orbit_id: str) -> int:
        """override"""
        return cls.split_id(orbit_id)[1]

    @classmethod
    def next_id(cls, orbit_id: str) -> str:
        """override"""
        prefix, orbit = cls.split_id(orbit_id)

        return f"{prefix}{orbit + 1:0{cls.ORBIT_WIDTH}}"


class CycleOrbitIdStrategy(OrbitIdStrategy):
    """Identifiers holding a cycle and a relative orbit: ``S3A-069-007``

    The relative orbit is reset to 1 at the beginning of each cycle. The number
    of orbits of a cycle is not fixed (commissioning, orbit manoeuvres), so the
    end of a cycle is never guessed: across cycles, only the orbits of the
    latest cycle, from its first one, are listed.
    """

    SEPARATOR = "-"

    CYCLE_WIDTH = 3

    ORBIT_WIDTH = 3

    @classmethod
    def split_id(cls, orbit_id: str) -> typing.Tuple[str, int, int]:
        """Split an identifier into its satellite, cycle and relative orbit

        Args:
            orbit_id (str): the identifier

        Returns:
            typing.Tuple[str, int, int]: the satellite, the cycle and the
                relative orbit
        """
        satellite, cycle, relative_orbit = orbit_id.split(cls.SEPARATOR)

        return satellite, int(cycle), int(relative_orbit)

    @classmethod
    def rank(cls, orbit_id: str) -> int:
        """override"""
        _, cycle, relative_orbit = cls.split_id(orbit_id)

        return cycle * 10**cls.ORBIT_WIDTH + relative_orbit

    @classmethod
    def next_id(cls, orbit_id: str) -> str:
        """override: the next orbit in the same cycle, the end of a cycle is unknown"""
        satellite, cycle, relative_orbit = cls.split_id(orbit_id)

        return cls.build_id(satellite, cycle, relative_orbit + 1)

    @classmethod
    def build_id(cls, satellite: str, cycle: int, relative_orbit: int) -> str:
        """Build an identifier from its parts

        Args:
            satellite (str): the satellite
            cycle (int): the cycle
            relative_orbit (int): the relative orbit

        Returns:
            str: the identifier
        """
        return (
            f"{satellite}{cls.SEPARATOR}{cycle:0{cls.CYCLE_WIDTH}}"
            f"{cls.SEPARATOR}{relative_orbit:0{cls.ORBIT_WIDTH}}"
        )

    @classmethod
    def ids_between(
        cls, orbit_id_1: str, orbit_id_2: str, descending: bool = False
    ) -> typing.List[str]:
        """override: across cycles, start from the first orbit of the latest cycle"""
        cls.check_ids(orbit_id_1, orbit_id_2)

        min_id, max_id = cls.sort_ids(orbit_id_1, orbit_id_2)

        _, min_cycle, min_relative_orbit = cls.split_id(min_id)
        satellite, max_cycle, max_relative_orbit = cls.split_id(max_id)

        if max_cycle == min_cycle:
            first_relative_orbit = min_relative_orbit + 1
        else:
            if max_cycle - min_cycle > 1:
                LOGGER.warning(
                    "Cycles skipped between %s and %s, their orbits are unknown",
                    min_id,
                    max_id,
                )
            first_relative_orbit = 1

        orbit_ids = [
            cls.build_id(satellite, max_cycle, relative_orbit)
            for relative_orbit in range(first_relative_orbit, max_relative_orbit)
        ]

        if descending:
            orbit_ids.reverse()

        return orbit_ids


class S3DatatakeIdStrategy(CycleOrbitIdStrategy):
    """S3 datatake identifiers: ``S3A-069-007``

    The numbering of a satellite can stop following its orbit, as during the S3C
    commissioning: 003-110 -> 004-001 ... 009-189 -> 003-111. The nominal
    numbering then reuses those identifiers later. Such an abnormal identifier is
    tagged, ``S3C-004-001-COM``: it no longer respects the format and is kept out
    of the arithmetic, and its completeness never mixes with the nominal orbit.
    """

    ID_REGEX_FORMAT = r"^S3[A-Z]-\d\d\d-\d\d\d$"

    ORBIT_DURATION = timedelta(minutes=101)

    # Reference of each satellite: a datatake of the nominal numbering and its
    # start sensing time. The orbit expected at another time is counted from it.
    # A satellite without reference is never tagged.
    #   satellite_unit -> (datatake_id, start sensing time)
    ORBIT_REFERENCES = {
        # S3C commissioning, numbering broken from 2026-09-21 to 2026-10-20: the
        # nominal numbering before and after it follows this reference
        "S3C": ("S3C-003-121", datetime(2026, 10, 26, 8, 32, 12, tzinfo=timezone.utc)),
    }

    # The nominal orbit repeats its ground track every 27 days, in 385 orbits.
    # Only used to tell the orbit expected at a time, never to list missing ones.
    NOMINAL_ORBITS_PER_CYCLE = 385

    NOMINAL_CYCLE_DURATION = timedelta(days=27)

    # A product, as the reference, starts anywhere in its orbit: the expected
    # orbit is known give or take one
    ORBIT_TOLERANCE = 2

    ABNORMAL_TAG = "COM"

    @classmethod
    def orbit_count(cls, datatake_id: str) -> int:
        """Number of nominal orbits since the first orbit of the first cycle

        Args:
            datatake_id (str): the identifier

        Returns:
            int: the orbit count
        """
        _, cycle, relative_orbit = cls.split_id(datatake_id)

        return (cycle - 1) * cls.NOMINAL_ORBITS_PER_CYCLE + relative_orbit - 1

    @classmethod
    def matches_sensing_time(cls, datatake_id: str, start: datetime) -> bool:
        """Tell if an identifier is the orbit expected at a time by the reference

        Args:
            datatake_id (str): the identifier
            start (datetime): start sensing time of the orbit

        Returns:
            bool: True if it matches, or the satellite has no reference
        """
        satellite = cls.split_id(datatake_id)[0]

        if satellite not in cls.ORBIT_REFERENCES:
            return True

        reference_id, reference_start = cls.ORBIT_REFERENCES[satellite]

        expected_count = cls.orbit_count(reference_id) + round(
            (start - reference_start)
            / (cls.NOMINAL_CYCLE_DURATION / cls.NOMINAL_ORBITS_PER_CYCLE)
        )

        return abs(cls.orbit_count(datatake_id) - expected_count) <= cls.ORBIT_TOLERANCE

    @classmethod
    def tag_abnormal(cls, datatake_id: str, start: datetime) -> str:
        """Tag an identifier that is not the orbit expected at its time

        Args:
            datatake_id (str): the identifier
            start (datetime): start sensing time of the orbit

        Returns:
            str: the identifier, tagged if abnormal
        """
        if not cls.is_orbit_id(datatake_id) or cls.matches_sensing_time(
            datatake_id, start
        ):
            return datatake_id

        return f"{datatake_id}{cls.SEPARATOR}{cls.ABNORMAL_TAG}"


class S5DatatakeIdStrategy(AbsoluteOrbitIdStrategy):
    """S5 datatake identifiers: ``S5P-12345``"""

    ID_REGEX_FORMAT = r"S5[A-Z]-\d\d\d\d\d"

    ORBIT_WIDTH = 5


class S3DownlinkOrbitStrategy(AbsoluteOrbitIdStrategy):
    """S3 downlink orbit of a session: ``052724``

    Unlike the datatake identifiers, a downlink orbit does not carry its
    satellite: it is held by the session document beside it.
    """

    ID_REGEX_FORMAT = r"^\d{6}$"

    ORBIT_WIDTH = 6
