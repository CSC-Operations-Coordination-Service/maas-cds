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

    @classmethod
    def check_ids(cls, *orbit_ids: str):
        """Ensure identifiers respect the format of the strategy

        Args:
            *orbit_ids (str): the identifiers to check

        Raises:
            ValueError: if one of the identifiers does not respect the format
        """
        if not all(re.search(cls.ID_REGEX_FORMAT, orbit_id) for orbit_id in orbit_ids):
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

    The relative orbit is reset to 1 at the beginning of each cycle.
    """

    SEPARATOR = "-"

    # Number of orbits a cycle holds, the relative orbit ranges from 1 to it
    ORBITS_PER_CYCLE = 385

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

        return cycle * cls.ORBITS_PER_CYCLE + relative_orbit

    @classmethod
    def next_id(cls, orbit_id: str) -> str:
        """override"""
        satellite, cycle, relative_orbit = cls.split_id(orbit_id)

        relative_orbit += 1

        if relative_orbit > cls.ORBITS_PER_CYCLE:
            relative_orbit = 1
            cycle += 1

        return (
            f"{satellite}{cls.SEPARATOR}{cycle:0{cls.CYCLE_WIDTH}}"
            f"{cls.SEPARATOR}{relative_orbit:0{cls.ORBIT_WIDTH}}"
        )


class S3DatatakeIdStrategy(CycleOrbitIdStrategy):
    """S3 datatake identifiers: ``S3A-069-007``"""

    ID_REGEX_FORMAT = r"S3[A-Z]-\d\d\d-\d\d\d"


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
