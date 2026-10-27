#!/usr/bin/env python3
"""

Migration script to migrate CDS from 2.17 to 2.18

During the S3C commissioning, the cycle / relative orbit numbering went
003-110 -> 004-001 ... 009-189 -> 003-111: the nominal numbering reuses those
identifiers later. From 2.18, a S3 datatake_id that is not the orbit expected at
its sensing time, counted from the reference of the satellite
(S3DatatakeIdStrategy.ORBIT_REFERENCES), is tagged: S3C-004-001-COM. This patch
brings the existing documents in line:

1. the products and publications get the tagged datatake_id;
2. the completeness documents (cds-s3-completeness and the splitted ones) of a
   tagged datatake, keyed on the datatake_id, are copied under the tagged key
   and the untagged one is deleted;
3. the other abnormal completeness documents, generated as missing orbits from
   the broken numbering (cycles wrapped at 385, 009-189 -> 003-111), are deleted.

Run it before the nominal numbering reaches the abnormal identifiers again
(S3C-004-001).
"""

import sys

from opensearchpy import Q

from maas_engine.update.migration import MaasMigrator, migration_main

from maas_cds.lib.orbit_id_strategy import S3DatatakeIdStrategy
from maas_cds.lib.parsing_name.parsing_name_s3 import (
    extract_data_from_product_name_s3,
)
from maas_cds.model import (
    CdsCompletenessSplittedS3,
    CdsProduct,
    CdsPublication,
    CdsS3Completeness,
)

TAG_SUFFIX = f"{S3DatatakeIdStrategy.SEPARATOR}{S3DatatakeIdStrategy.ABNORMAL_TAG}"


class PatchV2_18_0(MaasMigrator):
    """Migration tool to migrate from 2.17 to 2.18"""

    def __init__(self, args):
        super().__init__(args)

        # untagged datatake_id of the products tagged
        self.abnormal_datatake_ids = set()

    @staticmethod
    def untagged_search(model, satellite_unit):
        """Search the documents of a satellite not tagged yet"""
        return (
            model.search()
            .filter("term", satellite_unit=satellite_unit)
            .filter("bool", must_not=Q("wildcard", datatake_id=f"*{TAG_SUFFIX}"))
        )

    @MaasMigrator.migration_action_iterator
    def tag_products_iterator(self):
        """Tag the abnormal datatake_id of the S3 products and publications"""
        for satellite_unit in S3DatatakeIdStrategy.ORBIT_REFERENCES:
            for model in (CdsProduct, CdsPublication):
                search = self.untagged_search(model, satellite_unit)

                self.logger.info(
                    "[%s][%s] - %s documents to check",
                    model.__name__,
                    satellite_unit,
                    search.count(),
                )

                for document in search.params(ignore=404).scan():
                    datatake_id = extract_data_from_product_name_s3(document.name).get(
                        "datatake_id"
                    )

                    if not datatake_id or datatake_id == document.datatake_id:
                        continue

                    self.abnormal_datatake_ids.add(document.datatake_id)

                    yield {
                        "_op_type": "update",
                        "_index": document.meta.index,
                        "_id": document.meta.id,
                        "doc": {"datatake_id": datatake_id},
                    }

    @MaasMigrator.migration_action_iterator
    def rekey_completeness_iterator(self):
        """Tag the abnormal S3 completeness, delete the generated ones"""
        for satellite_unit in S3DatatakeIdStrategy.ORBIT_REFERENCES:
            for model in (CdsS3Completeness, CdsCompletenessSplittedS3):
                search = self.untagged_search(model, satellite_unit)

                for document in search.params(ignore=404).scan():
                    datatake_id = document.datatake_id

                    # the observation of a completeness comes from its products:
                    # matching its orbit, it is a nominal one
                    if not S3DatatakeIdStrategy.is_orbit_id(
                        datatake_id
                    ) or S3DatatakeIdStrategy.matches_sensing_time(
                        datatake_id, document.observation_time_start
                    ):
                        continue

                    if datatake_id in self.abnormal_datatake_ids:
                        tagged_datatake_id = datatake_id + TAG_SUFFIX
                        tagged_key = (
                            tagged_datatake_id + document.key[len(datatake_id) :]
                        )

                        self.logger.info(
                            "[%s] - %s becomes %s",
                            model.__name__,
                            document.meta.id,
                            tagged_key,
                        )

                        yield {
                            "_op_type": "index",
                            "_index": document.meta.index,
                            "_id": tagged_key,
                            "_source": document.to_dict()
                            | {"datatake_id": tagged_datatake_id, "key": tagged_key},
                        }

                    else:
                        self.logger.info(
                            "[%s] - %s generated from the broken numbering, deleted",
                            model.__name__,
                            document.meta.id,
                        )

                    yield {
                        "_op_type": "delete",
                        "_index": document.meta.index,
                        "_id": document.meta.id,
                    }

    def run(self):
        """override"""
        if not S3DatatakeIdStrategy.ORBIT_REFERENCES:
            self.logger.error("No S3 orbit reference configured: nothing to do")
            return

        # the products first: they tell the abnormal datatakes apart
        self.bulk_exec(self.tag_products_iterator())

        self.bulk_exec(self.rekey_completeness_iterator())


def run():
    migration_main(sys.argv[1:], PatchV2_18_0)


if __name__ == "__main__":
    run()
