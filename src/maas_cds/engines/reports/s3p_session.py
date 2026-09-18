"""S3P Session consolidation"""

from maas_cds.lib.config_manager import MaasConfigManager
from maas_cds.lib.parsing_name.parsing_name_s3 import extract_data_from_product_name_s3
from maas_cds.model.configuration import MaasConfigCompletenessS3
from maas_cds.model.s3p_session import S3pMetricsCirculationAgent
from maas_engine.engine import DataEngine
from opensearchpy.exceptions import NotFoundError

import maas_cds.model as model
from maas_model.date_utils import datestr_to_utc_datetime, datetime_to_zulu


class S3pSessionConsolidatorEngine(DataEngine):
    """Consolidate raw metrics to S3pSession"""

    ENGINE_ID = "CONSOLIDATE_S3P_SESSION"

    CONSOLIDATED_MODEL = model.S3pSession

    def __init__(
        self,
        args=None,
        raw_data_type=None,
        chunk_size=None,
        send_reports=True,
    ):
        super().__init__(args, chunk_size=chunk_size, send_reports=send_reports)

        self.raw_data_type = raw_data_type

        # needed to get the expected values of the session completeness. The
        # session KPI do not depend on it: a missing configuration index shall
        # not prevent the consolidation.
        try:
            self.config_manager = MaasConfigManager(
                config_model_class=[MaasConfigCompletenessS3()]
            )
        except NotFoundError:
            self.logger.warning(
                "No S3 completeness configuration: the session completeness "
                "will not be computed"
            )

        self.local_session_cache = {}

        # sessions created for a downlink orbit that turned out to have a real
        # one after all: they are dropped when their orbit is consolidated
        self.superseded_missing_sessions = {}

    def action_iterator(self):
        """override

        Yields:
            Iterator[typing.Generator]: bulk actions
        """

        for s3p_metrics_doc in self.input_documents:

            session_name = s3p_metrics_doc.s3p_session_name

            if session_name is None:
                self.logger.debug(
                    "Skip the %s, as there is no session we skip the document",
                    s3p_metrics_doc,
                )
                continue

            # Retrieve session_name to store it into cache
            session = self.local_session_cache.get(session_name)
            if session is None:
                session = model.S3pSession.get_by_id(session_name)
                if session is None:
                    session = model.S3pSession.from_session_name(session_name)

                self.local_session_cache[session_name] = session

                self.collect_superseded_missing_session(session)

            if session is None:
                self.logger.warning(
                    "Not able to find the session with %s - %s",
                    session_name,
                    s3p_metrics_doc,
                )
                continue

            # Consolidate from the appropriate way
            if self.raw_data_type == "S3pMetricsCirculationAgent":
                self.consolidate_from_S3pMetricsCirculationAgent(
                    s3p_metrics_doc, session
                )
            elif self.raw_data_type == "S3pMetricsRestCaduPollingAgent":
                self.consolidate_from_S3pMetricsRestCaduPollingAgent(
                    s3p_metrics_doc, session
                )
            elif self.raw_data_type == "S3pMetricsThinLayer":
                self.consolidate_from_S3pMetricsThinLayer(s3p_metrics_doc, session)
            else:
                self.logger.warning(
                    "Not able to consolidate data with raw_data_type: %s",
                    self.raw_data_type,
                )

            session.compute_kpi()

            yield session.to_bulk_action()

        yield from self.missing_sessions_actions()

    def collect_superseded_missing_session(self, session: model.S3pSession):
        """Spot the session created for the downlink orbit of a real session

        A downlink orbit reported as missing may eventually get its logs, late
        or replayed: the session standing for it is then a duplicate of the real
        one and has to go.

        Args:
            session (model.S3pSession): a session seen for the first time
        """
        if not session.satellite_id or not session.downlink_orbit:
            return

        missing_session_name = model.S3pSession.missing_session_name(
            session.satellite_id, session.downlink_orbit
        )

        if missing_session_name in self.superseded_missing_sessions:
            return

        missing_session = model.S3pSession.get_by_id(missing_session_name)

        if missing_session is None:
            return

        self.logger.info(
            "The downlink orbit %s was reported as missing and has a session "
            "after all: dropping %s",
            session.downlink_orbit,
            missing_session_name,
        )

        self.superseded_missing_sessions[missing_session_name] = missing_session

    def missing_sessions_actions(self):
        """Create the sessions of the downlink orbits no session was seen for

        Runs once per consolidated session rather than once per raw metric, and
        only for the sessions holding the trigger product type.

        Yields:
            Iterator[typing.Generator]: bulk actions
        """
        for missing_session in self.superseded_missing_sessions.values():
            yield missing_session.to_bulk_action("delete")

        # the sessions of this batch are not indexed yet: their orbits would
        # otherwise be seen as missing
        known_orbits = {
            (session.satellite_id, session.downlink_orbit)
            for session in self.local_session_cache.values()
        }

        for session in self.local_session_cache.values():

            if not session.is_session_to_check_missing_orbit():
                continue

            for missing_session in session.generate_missing_sessions():

                if (
                    missing_session.satellite_id,
                    missing_session.downlink_orbit,
                ) in known_orbits:
                    continue

                yield missing_session.to_bulk_action()

    # consolidate_from_ModelClass
    # pylint: disable=C0103
    def consolidate_from_S3pMetricsCirculationAgent(
        self, raw_document: model.S3pMetricsCirculationAgent, document: model.S3pSession
    ) -> model.S3pSession:

        # hkraw delivery time  - the logdate When the to_url will match   à It can be fond in the log “M&C|Data Circulation|DC|OUT|”, for the moment this is not linked to a timelinesskey using the TL|OUT message, nevertheless it contains the downlink orbit number in the filename, e.g.  the HKRAW for downlink session SVL__DCS_03_S3A_20260402064724052724_dat is S3A_OPER__HK__RAW___20260402T064733_20260402T065339_O52724_0001.TGZ and its delivery time is in the log “M&C|Data Circulation|DC|OUT|” with tourl containing “eumetsat.int”:
        # Apr  2 06:56:50 s3p-s3a-pf-acq-01 CirculationAgent[808464]: M&C|Data Circulation|DC|OUT|filename="S3A_OPER__HK__RAW___20260402T064733_20260402T065339_O52724_0001.TGZ"|queueid=102978|tourl="sftp://s3cgs@vids.eumetsat.int/out/toEUMFOS/S3A/S3A_OPER__HK__RAW___20260402T064733_20260402T065339_O52724_0001.TGZ"|filesize=3500070|

        if "OPER__HK__RAW" in raw_document.filename:
            if document is not None and document.hkraw_name != raw_document.filename:
                self.logger.warning("We are overriding the exisiting hkraw information")

            document.hkraw_name = raw_document.filename
            document.hkraw_size = raw_document.filesize
            document.hkraw_delivery_time = raw_document.log_date

        elif "_G_" in raw_document.filename:
            for granule in document.l0pp_granules:
                if granule.product_name == raw_document.filename:
                    granule.delivery_date_to_eum = datetime_to_zulu(
                        raw_document.log_date
                    )
                    granule.filesize = raw_document.filesize

                    flux = raw_document.flux
                    granule_flux = getattr(granule, "flux", None)
                    if flux:
                        if granule_flux and granule_flux != flux:
                            self.logger.warning(
                                "The flux of the granule %s changed from %s to %s, keeping the first one",
                                granule.product_name,
                                granule_flux,
                                flux,
                            )
                        elif not granule_flux:
                            granule.flux = flux

                    thinlayer_transfer_start = (
                        S3pMetricsCirculationAgent.search()
                        .filter("term", queueid=raw_document.queueid)
                        .filter("term", filename=raw_document.filename)
                        .filter("term", action="RUNNING")
                        .filter("term", status="QUEUE_OUT")
                        .sort({"log_date": {"order": "asc"}})
                        .execute()
                    )
                    if len(thinlayer_transfer_start) == 0:
                        self.logger.warning(
                            "Not able to find the transfer start for raw_data_type: %s",
                            raw_document,
                        )
                    else:
                        granule.delivery_start_date_to_eum = datetime_to_zulu(
                            thinlayer_transfer_start[0].log_date
                        )

                        granule.transfer_duration_to_eum = (
                            datestr_to_utc_datetime(granule.delivery_date_to_eum)
                            - datestr_to_utc_datetime(
                                granule.delivery_start_date_to_eum
                            )
                        ).total_seconds()

                        granule.transfer_bandwith_to_eum = (
                            granule.filesize / granule.transfer_duration_to_eum
                        )

                        break
            # but a log if a GR is missing

        elif raw_document.domain == "Data Circulation":
            finded = False

            COND_IN = raw_document.action == "IN"
            COND_OUT = raw_document.action == "OUT"

            if COND_IN or COND_OUT:

                for cadu in document.cadu_files:
                    if cadu.cadu_name == raw_document.filename:
                        finded = True

                        if COND_IN:
                            document.cadu_delivery_in = datetime_to_zulu(
                                raw_document.log_date
                            )
                        elif COND_OUT:
                            document.cadu_delivery_out = datetime_to_zulu(
                                raw_document.log_date
                            )
                        else:
                            self.logger.debug(
                                "There is nothing to do with this cadu file %s",
                                raw_document,
                            )
                        break

                if not finded:
                    document.cadu_files.append(
                        {
                            "cadu_name": raw_document.filename,
                            "cadu_delivery_in": (
                                datetime_to_zulu(raw_document.log_date)
                                if COND_IN
                                else None
                            ),
                            "cadu_delivery_out": (
                                datetime_to_zulu(raw_document.log_date)
                                if COND_OUT
                                else None
                            ),
                        }
                    )
            else:
                self.logger.debug("cadu input therenot handle %s", raw_document)
        else:
            self.logger.warning(
                "Not the place to be, there is a mismatch between session_name matching and this function"
            )
        return document

    # consolidate_from_ModelClass
    # pylint: disable=C0103
    def consolidate_from_S3pMetricsRestCaduPollingAgent(
        self,
        raw_document: model.S3pMetricsRestCaduPollingAgent,
        document: model.S3pSession,
    ) -> model.S3pSession:

        if raw_document.domain == "Data Import":
            document.acquisition_start_time = datetime_to_zulu(
                raw_document.creationtime
            )

        elif raw_document.domain == "Timeliness":
            document.acquisition_stop_time = datetime_to_zulu(raw_document.eventtime)
            document.timeliness_key = raw_document.timelinessKey

        else:
            self.logger.warning(
                "Not the place to be, there is a mismatch between session_name matching and this function"
            )
        return document

    # consolidate_from_ModelClass
    # pylint: disable=C0103
    def consolidate_from_S3pMetricsThinLayer(
        self, raw_document: model.S3pMetricsThinLayer, document: model.S3pSession
    ) -> model.S3pSession:

        #   It is the time of delivery to EUM of the last L0PP granule of a given downlink session, it can be found in the log “M&C|Data Circulation|DC|OUT|” with totoUrl containing “to_MRN”:
        # Apr  2 06:57:13 s3p-s3a-pf-acq-01 ThinLayer[1044962]: M&C|Timeliness|TL|OUT|eventtime="2026-04-02T06:57:13"|eventname="LOPP"|check="OK"|timelinessKey="SVL__DCS_03_S3A_20260402064724052724_dat"|filename="S3A_TM_0_NAT__G_20260402T050553_20260402T064744_20260402T064923_6111______________SVL_O_NR_OPE.ISIP"|pmode="N"|validitystart="2026-04-02T05:05:53.000000"|validitystop="2026-04-02T06:47:44.000000"|generationtime="2026-04-02T06:47:44.000000"|env="N"|ptype="0"|level="0"|sat="S3A"|message=""|reftime="ground"|

        # check if this GR is already
        if document.l0pp_granules is None:
            document.l0pp_granules = []

        raw_log_date = datetime_to_zulu(raw_document.log_date)
        generation_time = datetime_to_zulu(raw_document.generationtime)

        for granule in document.l0pp_granules:

            if granule.product_name == raw_document.filename:

                granule.validitystart = datetime_to_zulu(raw_document.validitystart)
                granule.validitystop = datetime_to_zulu(raw_document.validitystop)

                if (
                    hasattr(granule, "thin_layer_log_date")
                    and granule.thin_layer_log_date != raw_log_date
                ):

                    self.logger.warning(
                        "This l0pp_granules is already but the log_date changed take the new lowest"
                    )
                    if granule.thin_layer_log_date and raw_log_date:
                        granule.thin_layer_log_date = min(
                            raw_log_date, granule.thin_layer_log_date
                        )
                    else:
                        granule.thin_layer_log_date = (
                            raw_log_date or granule.thin_layer_log_date
                        )

                if (
                    hasattr(granule, "raw_data_generation_time")
                    and granule.raw_data_generation_time != generation_time
                ):

                    self.logger.warning(
                        "This l0pp_granules is already but the raw_data_generation_time changed take the new highest"
                    )
                    if granule.raw_data_generation_time and generation_time:

                        granule.raw_data_generation_time = max(
                            generation_time, granule.raw_data_generation_time
                        )
                    else:
                        granule.raw_data_generation_time = (
                            generation_time or granule.raw_data_generation_time
                        )

                self.logger.debug("This l0pp_granules is already registred")
                break

        else:
            granule_data = extract_data_from_product_name_s3(raw_document.filename)

            document.l0pp_granules.append(
                {
                    "product_name": raw_document.filename,
                    "thin_layer_log_date": raw_log_date,
                    "raw_data_generation_time": generation_time,
                    "validitystart": datetime_to_zulu(raw_document.validitystart),
                    "validitystop": datetime_to_zulu(raw_document.validitystop),
                    "product_type": granule_data["product_type"],
                    "timeliness": granule_data.get("timeliness"),
                }
            )

        return document
