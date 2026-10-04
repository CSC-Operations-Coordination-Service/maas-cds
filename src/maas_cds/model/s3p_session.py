"""Custom CDS model definition"""

import datetime
import logging

from maas_cds.lib.orbit_id_strategy import S3DownlinkOrbitStrategy
from maas_cds.lib.parsing_name.parsing_name_s3 import (
    extract_data_from_product_name_s3,
    granule_product_type_to_product_type,
)
from maas_cds.lib.periodutils import Period
from maas_cds.model import generated
from maas_cds.model.completeness_mixin import CompletenessMixin
from maas_cds.model.enumeration import CompletenessScope
from maas_model.date_utils import (
    datestr_to_utc_datetime,
    datetime_to_zulu,
)
from opensearchpy import Keyword

__all__ = [
    "S3pSession",
    "S3pMetricsThinLayer",
    "S3pMetricsCirculationAgent",
    "S3pMetricsRestCaduPollingAgent",
]


LOGGER = logging.getLogger("S3pSession")


# The flux of a GR is given by the first location of its circulation tourl
# ex: ftp://user:password@s3-refidcs01/data/to_MRN/to_IDC_NEW/from_ACQ/High/S3A_SR_0_SRA__G_[...].ISIP
NRT_FLUX = "NRT"

QRT_FLUX = "QRT"

FLUX_BY_LOCATION = {
    "s3-opeidcs01": NRT_FLUX,
    "s3-refidcs01": QRT_FLUX,
}

# Position of the level in a S3 product type, ex: TM_0_NAT___
PRODUCT_LEVEL_INDEX = 3

LEVEL_0_INDICATOR = "0"

# A session name is the satellite, the acquisition timestamp and the downlink
# orbit: S3A_20260402064724052724. The session standing for a downlink orbit no
# session was ever seen for takes this marker instead of the timestamp, keeping
# the name parseable and, above all, deriving only from the orbit.
MISSING_SESSION_TIMESTAMP = "MISSING_______"


def to_datetime(value):
    """Read a date whatever the way it is stored

    A date read back from the index is a datetime, one just set by the
    consolidation may still be the string it was logged as.

    Args:
        value: the date to read

    Returns:
        datetime.datetime: the date, None if there is none
    """
    if isinstance(value, str):
        return datestr_to_utc_datetime(value)

    return value


class S3pMetricsCirculationAgent(generated.S3pMetricsCirculationAgent):
    @property
    def flux(self) -> str:
        """Flux (NRT / QRT) deduced from the first location of the tourl

        Returns:
            str: the flux name, None if the tourl does not hold a known location
        """
        if not self.tourl:
            return None

        # drop the scheme and the credentials to get the first location
        location = self.tourl.split("://")[-1].split("@")[-1].split("/")[0]

        flux = FLUX_BY_LOCATION.get(location)

        if flux is None:
            LOGGER.warning("Unknown flux location %r in tourl %r", location, self.tourl)

        return flux

    @property
    def s3p_session_name(self) -> str:
        # HK CASE
        if (
            self.code == "DC"
            and self.action == "OUT"
            and "OPER__HK__RAW" in self.filename
            and "eumetsat.int" in self.tourl
        ):
            # S3A_OPER__HK__RAW___20260316T105033_20260316T105636_O52484_0001.TGZ
            orbit = self.filename.split("_O")[2].split("_")[0]

            # Here find the s3p session using the orbit
            rest_cadu_doc = (
                generated.S3pSession.search()
                .filter("term", downlink_orbit=orbit.zfill(6))
                .execute()
            )

            if len(rest_cadu_doc) == 0:
                return None

            return rest_cadu_doc[0].downlink_session

        # GR Case
        if (
            self.code == "DC"
            and self.action == "OUT"
            and "_G_" in self.filename
            and "to_MRN" in self.tourl
        ):
            # Here find the s3p session using the filename with the thinlayer
            thinlayer_doc = (
                S3pMetricsThinLayer.search()
                .filter("term", filename=self.filename)
                .execute()
            )
            if len(thinlayer_doc) == 0:
                return None

            return thinlayer_doc[0].timelinessKey[12:-4]

        if (
            self.domain == "Data Circulation"
            and self.code == "DC"
            and self.action in ("OUT", "IN")
            and "DSIB.xml" in self.filename
        ):
            return self.filename[7:31]


class S3pMetricsRestCaduPollingAgent(generated.S3pMetricsRestCaduPollingAgent):
    @property
    def s3p_session_name(self) -> str:
        # Start CASE
        # lucene query : domain: "Data Import" AND code: IMP AND action: IN
        if (
            self.domain == "Data Import"
            and self.code == "IMP"
            and self.action == "IN"
            and self.filename
        ):
            return self.filename

        # Stop Case
        elif (
            self.domain == "Timeliness"
            and self.code == "TL"
            and self.action == "REF"
            and self.timelinessKey
            and self.eventtime
        ):
            return self.timelinessKey[12:-4]

        else:
            # Match nothing
            return None


class S3pMetricsThinLayer(generated.S3pMetricsThinLayer):
    @property
    def s3p_session_name(self) -> str:
        #  It is the time of delivery to EUM of the last L0PP granule of a given downlink session, it can be found in the log “M&C|Data Circulation|DC|OUT|” with totoUrl containing “to_MRN”:
        # Currently the filter is made at the collect level

        # Here find the s3p session using the timelinessKey
        return self.timelinessKey[12:-4]


class S3pSession(CompletenessMixin, generated.S3pSession):
    """

    Override to store business logic
    """

    MISSION = "S3"

    # All the granules of a session are transferred in near real time, whatever
    # the timeliness their products are published with
    GRANULE_TIMELINESS = "NR"

    # Nominal sensing of the product types a session carries but the completeness
    # configuration does not hold: it is keyed on the published product types,
    # and the OL_0_CR granules are published as OL_0_CR0___ / OL_0_CR1___.
    # A configuration record of the same product type takes precedence.
    SESSION_SENSING_IN_MINUTES = {
        "OL_0_CR____": 44,
    }

    # Arithmetic over the downlink orbits, to spot the missing ones
    ORBIT_ID_STRATEGY = S3DownlinkOrbitStrategy

    # Product type of the granule triggering the missing downlink orbit
    # detection: the housekeeping telemetry every downlink session carries
    MISSING_ORBIT_PRODUCT_TYPE = "TM_0_HKM__G"

    # Nominal duration of a S3 orbit, used to place a missing session on the
    # time axis when the surrounding sessions give no better estimation
    ORBIT_DURATION = datetime.timedelta(minutes=101)

    # A wrong previous session, or a long collect outage, shall not flood the
    # index with sessions: only the most recent missing orbits are created
    MAX_MISSING_SESSIONS = 100

    l0pp_granules = Keyword(multi=True)

    cadu_files = Keyword(multi=True)

    completeness = Keyword(multi=True)

    @classmethod
    def from_session_name(cls, session_name: str) -> "S3pSession":
        """Create a new instance from a session name"""

        document = cls()
        document.meta.id = session_name
        document.downlink_session = session_name
        document.satellite_id = session_name[:3]
        document.downlink_orbit = session_name[18:]
        return document

    def compute_kpi(self):

        # self.last_cadu_chunk_delivery = max(
        #     (f.cadu_delivery_out for f in self.cadu_files), default=None
        # )

        if self.acquisition_stop_time:
            self.acquisition_stop_time = to_datetime(self.acquisition_stop_time)

        if self.acquisition_start_time:
            self.acquisition_start_time = to_datetime(self.acquisition_start_time)

        granules = self.l0pp_granules or []

        # KPI over all the granules of the session
        self.compute_granules_kpi(granules)

        # Same KPI but restricted to the QRT granules, stored with the "_qrt" suffix
        qrt_granules = [g for g in granules if getattr(g, "flux", None) == QRT_FLUX]

        if qrt_granules:
            self.compute_granules_kpi(qrt_granules, suffix="_qrt")
        else:
            LOGGER.debug("No QRT granule: the QRT kpi are left unset")

        # Sensing completeness of the session, per product type and timeliness
        self.compute_completeness(granules)

    def compute_granules_kpi(self, granules: list, suffix: str = ""):
        """Compute the granule related KPI of the session

        Args:
            granules (list): the granules to take into account
            suffix (str): suffix appended to the name of the computed attributes
        """

        latest_gr_published_to_eum = max(
            (
                f.delivery_date_to_eum
                for f in granules
                if hasattr(f, "delivery_date_to_eum") and f.delivery_date_to_eum
            ),
            default=None,
        )
        if latest_gr_published_to_eum and isinstance(latest_gr_published_to_eum, str):
            latest_gr_published_to_eum = datestr_to_utc_datetime(
                latest_gr_published_to_eum
            )

        latest_gr_generated = max(
            (
                f.raw_data_generation_time
                for f in granules
                if hasattr(f, "raw_data_generation_time") and f.raw_data_generation_time
            ),
            default=None,
        )

        if latest_gr_generated and isinstance(latest_gr_generated, str):
            latest_gr_generated = datestr_to_utc_datetime(latest_gr_generated)

        if granules:

            # GR Completeness
            gr_delivered = [
                f.delivery_date_to_eum
                for f in granules
                if hasattr(f, "delivery_date_to_eum") and f.delivery_date_to_eum
            ]

            setattr(
                self,
                f"delivery_to_eum_completeness{suffix}",
                len(gr_delivered) / len(granules),
            )

        else:
            LOGGER.debug("No gr set the completeness to 0")
            setattr(self, f"delivery_to_eum_completeness{suffix}", 0)

        if latest_gr_published_to_eum and self.acquisition_stop_time:
            setattr(
                self,
                f"delivery_to_eum_timeliness{suffix}",
                (
                    latest_gr_published_to_eum - self.acquisition_stop_time
                ).total_seconds(),
            )
        else:
            LOGGER.debug(
                "Missing information can't set timeliness: stop  %s  latest %s",
                self.acquisition_stop_time,
                latest_gr_published_to_eum,
            )

        if latest_gr_published_to_eum and self.acquisition_start_time:
            setattr(
                self,
                f"delivery_to_eum_timeliness_from_acq_start{suffix}",
                (
                    latest_gr_published_to_eum - self.acquisition_start_time
                ).total_seconds(),
            )
        else:
            LOGGER.debug(
                "Missing information can't set timeliness: start  %s  latest %s",
                self.acquisition_start_time,
                latest_gr_published_to_eum,
            )

        if latest_gr_generated and self.acquisition_start_time:
            setattr(
                self,
                f"generation_timeliness_from_acq_start{suffix}",
                (latest_gr_generated - self.acquisition_start_time).total_seconds(),
            )
        else:
            LOGGER.debug(
                "Missing information can't set timeliness: stop  %s  latest %s",
                self.acquisition_start_time,
                latest_gr_generated,
            )

        if latest_gr_generated and self.acquisition_stop_time:
            setattr(
                self,
                f"generation_timeliness_from_acq_stop{suffix}",
                (latest_gr_generated - self.acquisition_stop_time).total_seconds(),
            )
        else:
            LOGGER.debug(
                "Missing information can't set timeliness: stop  %s  latest %s",
                self.acquisition_stop_time,
                latest_gr_generated,
            )

    def granule_product_type(self, granule) -> str:
        """Product type of a granule, normalized to its full orbit flavour

        The completeness configuration and the products indices hold the full
        orbit flavour of a product type (TM_0_NAT___), the granules the granule
        one (TM_0_NAT__G).

        Args:
            granule: a l0pp granule of the session

        Returns:
            str: the product type, None if it cannot be resolved
        """
        product_type = getattr(granule, "product_type", None)

        product_name = getattr(granule, "product_name", None)

        if not product_type and product_name:
            product_type = extract_data_from_product_name_s3(product_name).get(
                "product_type"
            )

        if not product_type:
            LOGGER.warning(
                "[%s] - Cannot find the product type of granule %s",
                self.meta.id,
                product_name,
            )
            return None

        return granule_product_type_to_product_type(product_type)

    def granule_period(self, granule) -> Period:
        """Sensing period covered by a granule

        Args:
            granule: a l0pp granule of the session

        Returns:
            Period: the validity period of the granule, None if it is unusable
        """
        start = getattr(granule, "validitystart", None)
        end = getattr(granule, "validitystop", None)

        if not start or not end:
            LOGGER.debug(
                "[%s] - Granule %s has no validity period yet",
                self.meta.id,
                getattr(granule, "product_name", None),
            )
            return None

        if isinstance(start, str):
            start = datestr_to_utc_datetime(start)

        if isinstance(end, str):
            end = datestr_to_utc_datetime(end)

        if end <= start:
            LOGGER.warning(
                "[%s] - Granule %s has an inconsistent validity period: %s / %s",
                self.meta.id,
                getattr(granule, "product_name", None),
                start,
                end,
            )
            return None

        return Period(start, end)

    def group_granules_periods(self, granules: list) -> dict:
        """Group the validity periods of the granules by product type

        Args:
            granules (list): the granules to take into account

        Returns:
            dict: the periods list of each product type
        """
        granules_periods = {}

        for granule in granules:
            product_type = self.granule_product_type(granule)

            if product_type is None:
                continue

            period = self.granule_period(granule)

            if period is None:
                continue

            granules_periods.setdefault(product_type, []).append(period)

        return granules_periods

    def expected_product_types(self) -> list:
        """Product types a downlink session is expected to carry

        A session only holds L0PP granules: the L1 and L2 product types of the
        completeness configuration are generated later in the chain and are not
        part of a session. The product types a session carries without being
        published as such are added.

        Returns:
            list(str): the product types expected in a session
        """
        product_types = {
            record.product_type
            for record in self.completeness_configuration()
            if record.product_type[PRODUCT_LEVEL_INDEX] == LEVEL_0_INDICATOR
        }

        return sorted(product_types | set(self.SESSION_SENSING_IN_MINUTES))

    def get_session_expected_value(self, product_type: str) -> int:
        """Expected sensing value of a product type of the session

        Args:
            product_type (str): the product type

        Returns:
            int: the expected value in microseconds, 0 if it cannot be resolved
        """
        is_configured = any(
            record.product_type == product_type
            for record in self.completeness_configuration()
        )

        if is_configured:
            return self.get_expected_value_for_product_type(
                product_type, self.GRANULE_TIMELINESS
            )

        sensing_in_minutes = self.SESSION_SENSING_IN_MINUTES.get(product_type)

        if sensing_in_minutes is None:
            LOGGER.warning(
                "[%s] - No expected sensing for product_type=%s, neither in the "
                "configuration nor in the session ones",
                self.meta.id,
                product_type,
            )
            return 0

        return self.expected_value_from_sensing_in_minutes(
            product_type, sensing_in_minutes
        )

    @staticmethod
    def completeness_entry(
        product_type: str,
        values,
        observation_period: Period,
        missing_sensing_date: datetime.datetime = None,
    ) -> dict:
        """Completeness of a single product type of the session

        Args:
            product_type (str): the product type
            values (CompletenessValues): the computed completeness values
            observation_period (Period): the sensing the granules of that
                product type cover, None when the session carries none
            missing_sensing_date (datetime.datetime): date to fall back on when
                the product type has no sensing period of its own

        Returns:
            dict: an entry of the completeness list
        """
        entry = {"product_type": product_type}

        entry.update(values._asdict())

        if observation_period is not None:
            entry["sensing_start_date"] = datetime_to_zulu(observation_period.start)
            entry["sensing_stop_date"] = datetime_to_zulu(observation_period.end)

        elif missing_sensing_date is not None:
            # a period of no duration at all: the product type is placed on the
            # time axis without claiming it covers any sensing
            entry["sensing_start_date"] = entry["sensing_stop_date"] = datetime_to_zulu(
                missing_sensing_date
            )

        return entry

    def missing_sensing_date(self, granules_periods: dict) -> datetime.datetime:
        """Date standing for the sensing of a product type the session misses

        A product type with no granule has no sensing period, so nothing places
        it on a time axis and it is invisible on the dashboards next to the
        product types the session did carry. The middle of the sensing the
        session covers is used instead, and the acquisition window of the
        session when it carries no granule at all.

        Args:
            granules_periods (dict): the periods of each product type of the session

        Returns:
            datetime.datetime: the date, None if the session is not dated at all
        """
        periods = [
            period for periods in granules_periods.values() for period in periods
        ]

        if periods:
            start = min(period.start for period in periods)
            end = max(period.end for period in periods)

        else:
            acquisition_period = self.acquisition_period()

            if acquisition_period is None:
                # an undated session: the start alone is better than nothing
                return to_datetime(self.acquisition_start_time)

            start, end = acquisition_period

        return start + (end - start) / 2

    def completeness_for(self, product_type: str) -> dict:
        """Completeness entry of a product type

        Args:
            product_type (str): the product type

        Returns:
            dict: the entry, None if the session has none for that product type
        """
        for entry in self.completeness or []:
            if entry["product_type"] == product_type:
                return entry

        return None

    def compute_completeness(self, granules: list):
        """Compute the sensing completeness of the session

        The ``completeness`` list holds one entry per product type a session is
        expected to carry, so a product type completely missing from the session
        reads 0% instead of nothing, placed on the time axis by a sensing period
        of no duration. The global completeness aggregating them is stored on
        the session itself.

        The timeliness is not part of the key: all the granules of a session are
        transferred in NR whatever the timeliness their products are published
        with, and the expected sensing of a product type is the same for all of
        them.

        Args:
            granules (list): the granules to take into account
        """
        granules_periods = self.group_granules_periods(granules)

        missing_sensing_date = self.missing_sensing_date(granules_periods)

        completeness = []

        global_value = 0
        global_expected = 0

        for product_type in self.expected_product_types():

            sensing_value, observation_period = self.compute_sensing_value(
                granules_periods.pop(product_type, [])
            )

            completeness_values = self.compute_completeness_values(
                sensing_value, self.get_session_expected_value(product_type)
            )

            if completeness_values is None:
                continue

            completeness.append(
                self.completeness_entry(
                    product_type,
                    completeness_values,
                    observation_period,
                    missing_sensing_date,
                )
            )

            global_value += completeness_values.value_adjusted
            global_expected += completeness_values.expected

        self.completeness = completeness

        if granules_periods:
            LOGGER.debug(
                "[%s] - Granule product types out of the completeness "
                "configuration, ignored: %s",
                self.meta.id,
                sorted(granules_periods),
            )

        global_values = self.compute_completeness_values(global_value, global_expected)

        if global_values is not None:
            self.set_completeness_attributes(
                None, CompletenessScope.GLOBAL, global_values
            )

    @classmethod
    def missing_session_name(cls, satellite_id: str, downlink_orbit: str) -> str:
        """Name of the session standing for a downlink orbit no session was seen for

        The name has the shape of a real one so it is parsed the same way, and
        it only depends on the orbit: consolidating the same missing orbit twice
        updates a single document instead of piling up duplicates.

        Args:
            satellite_id (str): the satellite of the session
            downlink_orbit (str): the downlink orbit of the session

        Returns:
            str: the session name
        """
        return f"{satellite_id}_{MISSING_SESSION_TIMESTAMP}{downlink_orbit}"

    @classmethod
    def from_missing_orbit(cls, satellite_id: str, downlink_orbit: str) -> "S3pSession":
        """Create the session standing for a downlink orbit no session was seen for

        Args:
            satellite_id (str): the satellite of the session
            downlink_orbit (str): the downlink orbit of the session

        Returns:
            S3pSession: an empty session, flagged as missing
        """
        session = cls.from_session_name(
            cls.missing_session_name(satellite_id, downlink_orbit)
        )
        session.is_missing = True

        return session

    def is_session_to_check_missing_orbit(self) -> bool:
        """Whether this session triggers the missing downlink orbit detection

        A single product type triggers it, so a session is not looked up as many
        times as it holds granules. It is the one every downlink session carries
        whatever the instruments involved.

        Returns:
            bool: True if the session holds a granule of the trigger product type
        """
        return any(
            getattr(granule, "product_type", None) == self.MISSING_ORBIT_PRODUCT_TYPE
            for granule in (self.l0pp_granules or [])
        )

    def acquisition_period(self) -> Period:
        """Period the session was acquired over

        Falls back on the validity of the granules when the CADU polling logs
        the acquisition times come from did not reach the database.

        Returns:
            Period: the acquisition period, None if it cannot be resolved
        """
        start = to_datetime(self.acquisition_start_time)

        end = to_datetime(self.acquisition_stop_time)

        if start and end:
            return Period(start, end)

        granules_periods = [
            period
            for period in (
                self.granule_period(granule) for granule in (self.l0pp_granules or [])
            )
            if period is not None
        ]

        if not granules_periods:
            return None

        return Period(
            start or min(period.start for period in granules_periods),
            end or max(period.end for period in granules_periods),
        )

    def get_previous_orbit_document(self) -> "S3pSession":
        """Session of the closest downlink orbit preceding this one

        Returns:
            S3pSession: the previous session, None if there is none
        """
        query = (
            self.search()
            .filter("term", satellite_id=self.satellite_id)
            .filter("range", downlink_orbit={"lt": self.downlink_orbit})
            .sort({"downlink_orbit": {"order": "desc"}})
            .params(size=1, ignore=404)
        )

        previous_sessions = list(query.execute())

        if not previous_sessions:
            LOGGER.info("[%s] - No previous orbit for this session", self.meta.id)
            return None

        return previous_sessions[0]

    def missing_downlink_orbits(self, previous_session: "S3pSession") -> list:
        """Downlink orbits between the previous session and this one

        Args:
            previous_session (S3pSession): the closest preceding session

        Returns:
            list(str): the missing downlink orbits, the most recent one first,
                empty if the orbits cannot be compared
        """
        try:
            missing_orbits = self.ORBIT_ID_STRATEGY.ids_between(
                previous_session.downlink_orbit,
                self.downlink_orbit,
                descending=True,
            )
        except ValueError:
            LOGGER.warning(
                "[%s] - Cannot compare the downlink orbit with the one of the "
                "previous session %s",
                self.meta.id,
                previous_session.meta.id,
            )
            return []

        return missing_orbits

    def missing_sessions_step(
        self, previous_session: "S3pSession", missing_count: int
    ) -> datetime.timedelta:
        """Time between two consecutive downlink sessions

        Measured between this session and the previous one when both are dated,
        so the missing sessions are spread over the actual gap, and falling back
        on the nominal orbit duration otherwise.

        Args:
            previous_session (S3pSession): the closest preceding session
            missing_count (int): the number of missing sessions in between

        Returns:
            datetime.timedelta: the time between two consecutive sessions
        """
        period = self.acquisition_period()

        previous_period = previous_session.acquisition_period()

        if period is None or previous_period is None:
            return self.ORBIT_DURATION

        step = (period.start - previous_period.start) / (missing_count + 1)

        if step <= datetime.timedelta(0):
            LOGGER.warning(
                "[%s] - The previous session %s is not older: falling back on "
                "the nominal orbit duration",
                self.meta.id,
                previous_session.meta.id,
            )
            return self.ORBIT_DURATION

        return step

    def generate_missing_sessions(self) -> list:
        """Create the sessions of the downlink orbits no session was seen for

        Without them a completely missed session is not a session at 0%: it is
        no document at all, and nothing to see on the dashboards.

        Returns:
            list(S3pSession): the sessions to index, empty if none is missing
        """
        previous_session = self.get_previous_orbit_document()

        if previous_session is None:
            return []

        missing_orbits = self.missing_downlink_orbits(previous_session)

        if not missing_orbits:
            LOGGER.debug(
                "[%s] - No missing downlink orbit since the previous session %s",
                self.meta.id,
                previous_session.meta.id,
            )
            return []

        # measured over the whole gap, before it is capped below: the sessions
        # that are created shall keep the real pace of the downlink sessions
        step = self.missing_sessions_step(previous_session, len(missing_orbits))

        if len(missing_orbits) > self.MAX_MISSING_SESSIONS:
            LOGGER.warning(
                "[%s] - %s downlink orbits missing since the previous session "
                "%s: only the %s most recent ones are created",
                self.meta.id,
                len(missing_orbits),
                previous_session.meta.id,
                self.MAX_MISSING_SESSIONS,
            )
            missing_orbits = missing_orbits[: self.MAX_MISSING_SESSIONS]

        period = self.acquisition_period()

        if period is None:
            LOGGER.warning(
                "[%s] - No date to place the %s missing session(s) on the time axis",
                self.meta.id,
                len(missing_orbits),
            )

        duration = period.end - period.start if period else step

        missing_sessions = []

        # the orbits come from the most recent one, right before this session,
        # to the oldest one, right after the previous session
        for rank, downlink_orbit in enumerate(missing_orbits, start=1):

            LOGGER.info(
                "[%s] - Creating the session of the missing downlink orbit %s",
                self.meta.id,
                downlink_orbit,
            )

            missing_session = self.from_missing_orbit(self.satellite_id, downlink_orbit)

            if period is not None:
                missing_session.acquisition_start_time = period.start - rank * step
                missing_session.acquisition_stop_time = (
                    missing_session.acquisition_start_time + duration
                )

            # an empty session: every expected product type reads 0%
            missing_session.compute_kpi()

            missing_sessions.append(missing_session)

        return missing_sessions
