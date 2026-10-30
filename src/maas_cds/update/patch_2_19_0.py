#!/usr/bin/env python3
"""

Migration script to migrate CDS from 2.18 to 2.19

cds-ai-production-completeness maps absolute_orbit (keyword), read from the
AI_RAW name by COMPUTE_AI_RELATED. It is a single index: the template is put
and the field added to the mapping of the existing index, which is reindexed
when it mapped the field otherwise. The existing documents then get the orbit
of their AI_RAW (input_name).
"""

import sys

from maas_engine.update.migration import MaasMigrator, migration_main

from maas_cds.lib.parsing_name.parsing_name_s1 import (
    extract_absolute_orbit_from_product_name_s1,
)
from maas_cds.model import CdsAiProductionCompleteness

AI_COMPLETENESS_INDEX = "cds-ai-production-completeness"

# only the new field: the others may be mapped otherwise on an old index,
# which would reject the whole template mapping
ABSOLUTE_ORBIT_MAPPING = {"properties": {"absolute_orbit": {"type": "keyword"}}}


class PatchV2_19_0(MaasMigrator):
    """Migration tool to migrate from 2.18 to 2.19"""

    def map_absolute_orbit(self):
        """put the template; add absolute_orbit to the existing index, or
        reindex it when it mapped absolute_orbit otherwise"""
        if self.args.dry_run:
            self.logger.info(
                "Dry run: %s template and mapping not put", AI_COMPLETENESS_INDEX
            )
            return

        self.update_template(AI_COMPLETENESS_INDEX)

        if not self.es_conn.indices.exists(AI_COMPLETENESS_INDEX):
            return

        mapping = self.es_conn.indices.get_mapping(index=AI_COMPLETENESS_INDEX)
        field = (
            mapping[AI_COMPLETENESS_INDEX]["mappings"]
            .get("properties", {})
            .get("absolute_orbit")
        )
        if field is not None and field.get("type") != "keyword":
            self.migrate_index_template(AI_COMPLETENESS_INDEX, None)
            return

        self.es_conn.indices.put_mapping(
            body=ABSOLUTE_ORBIT_MAPPING,
            index=AI_COMPLETENESS_INDEX,
            request_timeout=self.args.es_timeout,
        )

    @MaasMigrator.migration_action_iterator
    def absolute_orbit_iterator(self):
        """Fill the absolute orbit of the AI completeness from its AI_RAW name"""
        search = (
            CdsAiProductionCompleteness.search()
            .filter("exists", field="input_name")
            .exclude("exists", field="absolute_orbit")
        )

        for document in search.params(ignore=404).scan():
            absolute_orbit = extract_absolute_orbit_from_product_name_s1(
                document.input_name
            )

            if absolute_orbit is None:
                self.logger.warning(
                    "No absolute orbit in %s, skipping", document.input_name
                )
                continue

            yield {
                "_op_type": "update",
                "_index": document.meta.index,
                "_id": document.meta.id,
                "doc": {"absolute_orbit": str(absolute_orbit)},
            }

    def run(self):
        """override"""
        # the mapping first: the documents are then written to a keyword
        self.map_absolute_orbit()

        self.bulk_exec(self.absolute_orbit_iterator())


def run():
    migration_main(sys.argv[1:], PatchV2_19_0)


if __name__ == "__main__":
    run()
