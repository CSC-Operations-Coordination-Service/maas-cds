"""Update entities after some container products are see"""

import typing
from datetime import datetime, timedelta, UTC
from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.model.configuration.maas_config import OcsConfigCompleteness
from opensearchpy import Q

from maas_engine.engine.rawdata import DataEngine
from maas_engine.engine.base import EngineReport
from maas_model import (
    MAASMessage,
    MAASDocument,
    datestr_to_utc_datetime,
)
from maas_cds.engines.reports.mission_mixin import MissionMixinEngine
from maas_cds.engines.reports.anomaly_impact import (
    AnomalyImpactMixinEngine,
    anomaly_link,
)

from maas_cds.model.generated import (
    MpProduct,
)
from maas_cds.model import (
    CdsCamsTickets,
)


class PostConsolidateMpFileEngine(
    MissionMixinEngine, AnomalyImpactMixinEngine, DataEngine
):
    """Deletetion of datatake due to rescheduled"""

    ENGINE_ID = "POST_CONSOLIDATE_MP_FILE"

    TOLERANCE_IN_MINUTES = 30

    def __init__(
        self,
        args=None,
        raw_data_type=None,
        consolidated_data_type=None,
        raw_data_time_start_field_name=None,
        consolidated_data_time_start_field_name=None,
        chunk_size=1,
        send_reports=True,
        tolerance_value: int = 30,
        merge_reports: bool = True,
    ):
        super().__init__(args, chunk_size=chunk_size, send_reports=send_reports)
        self.raw_data_type = raw_data_type
        self.consolidated_data_type = consolidated_data_type
        self.raw_data = self.get_model(self.raw_data_type)
        self.consolidated_data = self.get_model(self.consolidated_data_type)
        self.raw_data_time_start_field_name = raw_data_time_start_field_name
        self.consolidated_data_time_start_field_name = (
            consolidated_data_time_start_field_name
        )
        self.tolerance_value = tolerance_value
        self.config_manager = MaasConfigManager(
            config_model_class=OcsConfigCompleteness()
        )

        self.merge_reports = merge_reports

    def action_iterator(self) -> typing.Generator:
        """override

        Iter throught input documents and find products who are inside
        Then add informations on these products

        Yields:
            Iterator[typing.Generator]: bulk actions
        """

        for local_report_name in self.input_documents:
            # check if the report is empty i.e. there is no raw data depending on this report
            nbr_raw_data_for_report = self.get_nbr_raw_data_for_report(
                local_report_name
            )
            if nbr_raw_data_for_report == 0:
                self.logger.info(
                    "[%s] report empty no completeness computed for empty report",
                    local_report_name,
                )
                continue

            # retrieve satelite id from report
            sat_id = local_report_name[0:3]

            # A report lists its AIS datatakes hours after its first SAR one, the
            # consolidation deletes them from the previous report: every S1 unit
            # flying an AIS (S1C, S1D) needs them back
            if not sat_id.startswith("S1"):
                continue

            previous_repport_name = self.get_previous_report(sat_id, local_report_name)
            if previous_repport_name is None:
                continue

            for mp_product in self.get_ais_to_recreate(
                sat_id, local_report_name, previous_repport_name
            ):
                self.logger.info(
                    "[%s-%s] - Recreate this missing datatake",
                    mp_product.satellite_id,
                    mp_product.datatake_id,
                )
                produced = self.consolidate_CdsDatatake_from_MpProduct(mp_product)
                if produced:
                    yield produced.to_bulk_action()

    def get_report_min_date(self, report_name, instrument_mode=None):
        """Get the first observation start of a report

        Args:
            report_name (str): the report to inspect
            instrument_mode (str, optional): only the rows of this instrument mode

        Returns:
            datetime: the first observation start, None without row
        """
        search = self.raw_data.search().filter("term", reportName=report_name)
        if instrument_mode:
            search = search.filter("term", instrument_mode=instrument_mode)

        search.aggs.metric(
            "data_produced", "min", field=self.raw_data_time_start_field_name
        )

        value = search[0].execute().aggregations.data_produced["value"]

        if value is None:
            return None

        return datetime.fromtimestamp(value / 1000, tz=UTC)

    def get_next_report(self, sat_id, local_report_name):
        """Get the report following the given one

        Args:
            sat_id (str): satellite unit of the report
            local_report_name (str): the report name to inspect from

        Returns:
            str: name of the next report, None when it is the last one
        """
        search = (
            self.raw_data.search()
            .filter("term", satellite_id=sat_id)
            .filter("range", reportName={"gt": local_report_name})
            .sort("reportName")
            .params(size=1)
        )

        res = list(search.execute())

        return res[0].reportName if res else None

    def get_ais_to_recreate(self, sat_id, report_name, previous_report_name):
        """AIS datatakes of the previous report that the report deleted

        The consolidation of a report deletes the datatakes from its first
        observation start, but its first AIS datatake starts hours later: those
        of the previous report in between are lost. They are the ones to recreate,
        unless a datatake with their id exists already.

        Args:
            sat_id (str): satellite unit of the reports
            report_name (str): the report just consolidated
            previous_report_name (str): the report before it

        Returns:
            list(MpProduct): the AIS rows of the previous report to recreate
        """
        report_ais_start = self.get_report_min_date(report_name, "AIS")

        # No AIS on this unit (S1A)
        if report_ais_start is None:
            return []

        report_start = self.get_report_min_date(report_name)

        # a report consolidated again: the next one owns its own period
        next_report_name = self.get_next_report(sat_id, report_name)
        if next_report_name:
            report_ais_start = min(
                report_ais_start, self.get_report_min_date(next_report_name)
            )

        if report_start >= report_ais_start:
            return []

        mp_products = list(
            self.raw_data.search()
            .filter("term", reportName=previous_report_name)
            .filter("term", instrument_mode="AIS")
            .filter(
                "range",
                **{
                    self.raw_data_time_start_field_name: {
                        "gte": report_start,
                        "lt": report_ais_start,
                    }
                },
            )
            .params(ignore=404)
            .scan()
        )

        if not mp_products:
            return []

        existing_datatake_ids = {
            datatake.datatake_id
            for datatake in self.consolidated_data.search()
            .filter("term", satellite_unit=sat_id)
            .filter(
                "terms",
                datatake_id=[mp_product.datatake_id for mp_product in mp_products],
            )
            .source(["datatake_id"])
            .params(ignore=404)
            .scan()
        }

        self.logger.info(
            "[%s] - %s AIS datatakes of %s between %s and %s, %s exist already",
            report_name,
            len(mp_products),
            previous_report_name,
            report_start,
            report_ais_start,
            len(existing_datatake_ids),
        )

        return [
            mp_product
            for mp_product in mp_products
            if mp_product.datatake_id not in existing_datatake_ids
        ]

    def get_nbr_raw_data_for_report(self, local_report_name):
        """Get the number of value for the given report.

        Args:
            local_report_name (str): the report name to inspect in

        Returns:
            nbr_raw_data_for_report (int): the number of raw data in the report
        """
        nbr_raw_data_for_report = 0
        search = self.raw_data.search().filter("term", reportName=local_report_name)
        self.logger.debug(
            "[%s] get raw data nbr from report query : %s",
            local_report_name,
            search,
        )
        nbr_raw_data_for_report = search.count()
        self.logger.debug(
            "[%s] get raw data nbr from report query result : %s",
            local_report_name,
            nbr_raw_data_for_report,
        )
        return nbr_raw_data_for_report

    def get_previous_report(self, sat_id, local_report_name):
        """Get the next report name from the given report name.
        As report name fisrt date is the report availiabiliy
        the next report is the next availiabiliy.
        If there is no next report the next report name is set to wildcard "*"

        Args:
            local_report_name (str): the report name to inspect from

        Returns:
            next_repport_name: str name of the next report
        """
        previous_repport_name = None
        search = (
            self.raw_data.search()
            .filter("term", satellite_id=sat_id)
            .filter("range", reportName={"lt": local_report_name})
            .sort({"reportName": {"order": "desc"}})
            .params(size=1)
        )
        self.logger.debug("[%s] Previous report query : %s", local_report_name, search)

        res = search.execute()

        self.logger.debug(
            "[%s] Previous report query result : %s", local_report_name, res
        )

        res = list(res)

        if res:
            previous_repport_name = res[0].reportName

        return previous_repport_name

    def get_input_documents(self, message: MAASMessage) -> list[str]:
        """Get the input documents. Can be overriden for custom behaviour

        Args:
            message (maas_model.MAASMessage): input message

        Returns:
            list[str]: list of documents id (path of mp file in this particular case)
        """
        return message.document_ids

    # consolidate_OutputModelClass_from_InputModelClass
    # pylint: disable=C0103
    @anomaly_link
    def consolidate_CdsDatatake_from_MpProduct(
        self, mp_product: MpProduct
    ) -> MAASDocument:
        """generate a CDSDatatake from a MpProduct

        Args:
            mp_product (raw_data): the MpProduct to consolidate

        Returns:
            consolidated_data: the consolidated CdsDatatake
        """

        # NOT_RECORDING are test file
        if mp_product.timeliness == "NOT_RECORDING":
            return None

        cds_datatake = self.consolidated_data()

        # Get application date
        raw_document_application_date = datestr_to_utc_datetime(
            mp_product.reportName[16:31]
        )

        cds_datatake.name = mp_product.reportName
        cds_datatake.key = f"{mp_product.satellite_id}-{mp_product.datatake_id}"
        cds_datatake.meta.id = cds_datatake.key
        cds_datatake.datatake_id = mp_product.datatake_id
        if mp_product.satellite_id.startswith("S1"):
            cds_datatake.hex_datatake_id = (
                hex(int(mp_product.datatake_id, 10)).replace("0x", "").upper()
            )
        cds_datatake.satellite_unit = mp_product.satellite_id
        cds_datatake.mission = mp_product.satellite_id[:2]
        cds_datatake.observation_time_start = mp_product.observation_time_start
        cds_datatake.observation_duration = mp_product.observation_duration * 1000

        # Only S2 have observation_time_stop for S1 observation_time_stop must be computed
        if mp_product.observation_time_stop:
            cds_datatake.observation_time_stop = mp_product.observation_time_stop
        else:
            cds_datatake.observation_time_stop = (
                mp_product.observation_time_start
                + timedelta(milliseconds=mp_product.observation_duration)
            )

        # Only S1 have l0_sensing
        if mp_product.l0_sensing_duration:
            cds_datatake.l0_sensing_duration = mp_product.l0_sensing_duration * 1000
            cds_datatake.l0_sensing_time_start = mp_product.l0_sensing_time_start

            cds_datatake.l0_sensing_time_stop = (
                mp_product.l0_sensing_time_start
                + timedelta(milliseconds=mp_product.l0_sensing_duration)
            )

        # Only S2 have number_of_scenes
        if mp_product.number_of_scenes:
            cds_datatake.number_of_scenes = mp_product.number_of_scenes

        cds_datatake.absolute_orbit = (
            mp_product.absolute_orbit.lstrip("0")
            if mp_product.absolute_orbit is not None
            else None
        )
        cds_datatake.relative_orbit = (
            mp_product.relative_orbit.lstrip("0")
            if mp_product.relative_orbit is not None
            else None
        )

        # Only S1 have polarization
        if mp_product.polarization:
            cds_datatake.polarization = mp_product.polarization

        if mp_product.timeliness:
            cds_datatake.timeliness = mp_product.timeliness

        cds_datatake.instrument_mode = mp_product.instrument_mode

        # Only S1 have instrument_swath
        if mp_product.instrument_swath:
            cds_datatake.instrument_swath = mp_product.instrument_swath

        cds_datatake.application_date = raw_document_application_date

        return cds_datatake

    def _generate_report_from_action_per_documents(self, classname, action, documents):
        """Override default strategy to make report"""
        index_to_documents = {}
        for document in documents:
            index_name = document.partition_index_name
            if index_name not in index_to_documents:
                index_to_documents[index_name] = []
            index_to_documents[index_name].append(document)

        for index_name, docs in index_to_documents.items():

            self.logger.debug(
                "Using custom report strategy: %s - %s", index_name, classname
            )

            yield EngineReport(
                action,
                [doc.meta.id for doc in docs],
                document_class=classname,
                chunk_size=self.chunk_size,
                document_indices=[index_name],
            )

    def _populate_ticket_cache(self, mp_products):
        """
        Fill the ticket cache

        Args:
            mp_products (list): list of raw mp products
        """

        self._cams_tickets_dict = {}

        if self.consolidated_data_type == "CdsHktmAcquisitionCompleteness":
            self._populate_by_CdsHktmAcquisitionCompleteness(mp_products)
            return

        if not self.consolidated_data_type == "CdsDatatake":
            self.logger.debug(
                "No anomaly correlation on %s", self.consolidated_data_type
            )
            return

        tickets = (
            CdsCamsTickets()
            .search()
            .filter(
                "terms",
                datatake_ids=[
                    f"{mp_product.satellite_id}-{mp_product.datatake_id}"
                    for mp_product in mp_products
                ],
            )
            .sort({"updated": {"order": "asc"}})
            .params(size=10000)
            .execute()
        )

        for ticket in tickets:
            for datatake_id in ticket.datatake_ids:
                if datatake_id in self._cams_tickets_dict:
                    self._cams_tickets_dict[datatake_id].append(ticket)
                else:
                    self._cams_tickets_dict[datatake_id] = [ticket]
