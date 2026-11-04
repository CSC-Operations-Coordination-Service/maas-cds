"""Query to find datatake"""

import logging

from datetime import timedelta
from opensearchpy import Q

from maas_cds.model.datatake import CdsDatatake

__all__ = ["find_datatake_from_sensing", "sort_datatakes_by_distance"]

LOGGER = logging.getLogger("QueryUtils")

# S2C datatake observation times are ~30s late compared to the real sensing
S2C_MP_SHIFT_SECONDS = 30


def sort_datatakes_by_distance(datatake_documents, satellite, middle_timestamp):
    """Sort datatakes by distance of their observation window to a sensing date

    The distance is 0 when the date falls inside the window, so the datatake
    covering the date comes first. The S2C MP shift is applied to the date.

    Args:
        datatake_documents (list(CdsDatatake)): the datatakes to sort in place
        satellite (str): the satellite of the datatakes
        middle_timestamp (float): the sensing date as a timestamp

    Returns:
        list(CdsDatatake): the sorted datatakes
    """
    if satellite == "S2C":
        # Move the middle of 30 seconds due to the offset
        middle_timestamp += S2C_MP_SHIFT_SECONDS

    def distance(datatake_doc):
        start = datatake_doc.observation_time_start.timestamp()
        stop = datatake_doc.observation_time_stop.timestamp()

        if start <= middle_timestamp <= stop:
            return 0

        if middle_timestamp > stop:
            return middle_timestamp - stop

        return start - middle_timestamp

    datatake_documents.sort(key=distance)

    return datatake_documents


def find_datatake_from_sensing(mission, satellite, start_date, end_date, delta=20):
    """find datatake_doc function from sensing date

    Args:
        mission (str): the mission of the searched datatake
        satellite (str): the satellite of the searched datatake
        start_date (date): the start date that the searched datatake must cover
        end_date (date): the end date that the searched datatake must cover
        delta (int, optional): The delta in seconds add to extremum. Defaults to 0.

    Returns:
        list(CdsDatatake): the list of the datatake that match the input
    """

    adjsuted_start_date = start_date + timedelta(seconds=delta)
    adjsuted_end_date = end_date - timedelta(seconds=delta)

    if satellite == "S2C":
        # Move the product to 30sec in the futur base on mp shift
        adjsuted_start_date += timedelta(seconds=S2C_MP_SHIFT_SECONDS)
        adjsuted_end_date += timedelta(seconds=S2C_MP_SHIFT_SECONDS)

    # FIXME: Magic number 8
    max_document = 8

    # nominal use for search datake with product date information expected one datatake only
    search_request = (
        CdsDatatake.search()
        .filter("term", satellite_unit=satellite)
        .filter("term", mission=mission)
        .filter("range", observation_time_start={"lte": adjsuted_start_date})
        .filter("range", observation_time_stop={"gte": adjsuted_end_date})
        .params(ignore=404, size=max_document)
    )

    res = search_request.execute()

    if not res:
        LOGGER.debug(
            "No datatake found yet for %s: %s / %s",
            satellite,
            start_date,
            end_date,
        )
        return []

    datatake_document_that_match = list(res)

    if len(datatake_document_that_match) == max_document:
        LOGGER.critical(
            "Max document size reached, potentially missing the nearest datatake "
            "The delta seems to be inappropriate"
        )

    # sort by distance from middle

    product_start_date = start_date.timestamp()
    product_end_date = end_date.timestamp()
    middle_date = product_start_date + (product_end_date - product_start_date) / 2

    return sort_datatakes_by_distance(
        datatake_document_that_match, satellite, middle_date
    )
