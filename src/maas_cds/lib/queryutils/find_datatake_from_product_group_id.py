"""Query to find datatake"""

import datetime
import logging
import typing

from maas_cds.lib.queryutils.find_datatake_from_sensing import (
    S2C_MP_SHIFT_SECONDS,
    sort_datatakes_by_distance,
)
from maas_cds.model.datatake import CdsDatatake

__all__ = ["find_datatake_from_product_group_id"]

LOGGER = logging.getLogger("QueryUtils")


def find_datatake_from_product_group_id(
    mission: str, satellite: str, product_group_id: str
) -> typing.List[CdsDatatake]:
    """find datatake_doc function using a product group id identifier

    Args:
        mission (str): the mission of the searched datatake
        satellite (str): the satellite of the searched datatake
        product_group_id (str): datastrip id used to search in db

    Returns:
        list(CdsDatatake): the list of the datatake that match the input
    """
    if not product_group_id:
        return []

    try:
        product_id_data = extract_data_from_product_id(product_group_id)
    except ValueError:
        LOGGER.debug(
            "Could not use the product_group_id %s to rattach the product to a datatake",
            product_group_id,
        )
        return []

    # The product group date is in UTC
    product_group_date = product_id_data["date"].replace(tzinfo=datetime.UTC)

    # nominal use for search datake with product date information expected one datatake only

    if satellite != "S2C":
        datatake_documents = search_datatakes_around_date(
            mission,
            satellite,
            product_id_data["absolute_orbit"],
            product_group_date,
        )

    else:
        # Rely first on the exact product_group_id already registered on the
        # datatake by the DS path (datatake.product_group_ids)
        datatake_documents = list(
            CdsDatatake.search()
            .filter("term", mission=mission)
            .filter("term", satellite_unit=satellite)
            .filter("term", product_group_ids=product_group_id)
            .params(ignore=404)
            .execute()
        )

        if len(datatake_documents) > 1:
            # A datatake can still hold the product_group_id of a datastrip that
            # moved to its neighbour: prefer the one covering the product group date
            LOGGER.warning(
                "%s datatakes hold product_group_id %s, keeping the closest in time",
                len(datatake_documents),
                product_group_id,
            )

        elif not datatake_documents:
            # The L0 granules come with their datastrip, before any datatake holds
            # their group: match the product group date like the other units,
            # but with the ~30s MP shift of the S2C observation times
            datatake_documents = search_datatakes_around_date(
                mission,
                satellite,
                product_id_data["absolute_orbit"],
                product_group_date + datetime.timedelta(seconds=S2C_MP_SHIFT_SECONDS),
            )

        sort_datatakes_by_distance(
            datatake_documents, satellite, product_group_date.timestamp()
        )

    if not datatake_documents:
        LOGGER.debug(
            "No datatake found yet for %s: which contain product_group_id : %s",
            satellite,
            product_group_id,
        )

    return datatake_documents


def search_datatakes_around_date(
    mission: str, satellite: str, absolute_orbit: str, date: datetime.datetime
) -> typing.List[CdsDatatake]:
    """Search the datatakes of an orbit observing around a date (20s tolerance)

    Args:
        mission (str): the mission of the searched datatake
        satellite (str): the satellite of the searched datatake
        absolute_orbit (str): the absolute orbit of the searched datatake
        date (datetime): the date the datatake observation must cover

    Returns:
        list(CdsDatatake): the matching datatakes
    """
    search_request = (
        CdsDatatake.search()
        .filter("term", mission=mission)
        .filter("term", satellite_unit=satellite)
        .filter("term", absolute_orbit=absolute_orbit)
        .filter(
            "range",
            observation_time_start={"lte": date + datetime.timedelta(seconds=20)},
        )
        .filter(
            "range",
            observation_time_stop={"gte": date - datetime.timedelta(seconds=20)},
        )
        .params(ignore=404)
    )

    return list(search_request.execute())


def extract_data_from_product_id(
    product_group_id: str,
) -> typing.Dict[str, str | datetime.datetime]:
    """Function which retrieve the different info contained in the product group id string and return a dict with them

    Args:
        product_group_id (str): _description_

    Raises:
        ValueError: _description_

    Returns:
        _type_: _description_
    """
    try:
        # Expecting the following format GS2A_20240207T101201_045064_N05.10"
        sat_unit, date_str, absolute_orbit, instrument = product_group_id.split("_")

        # Remove G prefix in front of sat unit
        sat_unit = sat_unit[1:]

        # Remove trailing 0
        absolute_orbit = str(int(absolute_orbit))

        product_id_date = datetime.datetime.strptime(date_str, r"%Y%m%dT%H%M%S")

    except (IndexError, ValueError) as exc:
        raise ValueError(
            f"Could not extract data from the following product_group_id {product_group_id}."
        ) from exc
    else:
        return {
            "satellite_unit": sat_unit,
            "date": product_id_date,
            "absolute_orbit": absolute_orbit,
            "instrument": instrument,
        }
