#!/usr/bin/env python3
"""

Migration script to migrate CDS from 2.15 to 2.16

The OCS-specific configurations move from maas-config-* to ocs-config-*:
maas-config-* is left to what the MAAS framework itself configures, such as
the collectors. Each new index is installed from its template and gets a
copy of the old one's documents, ids kept; the old indices stay, to be
deleted by hand once the engines run on the new ones.

maas-config-collector maps interface_name as a keyword from now on, the field
TRACK_DELETION and the configuration API search: an index that mapped it
otherwise is reindexed with the new template.
"""

import sys

from maas_engine.update.migration import MaasMigrator, migration_main

# old index -> new index
MOVED_CONFIGURATIONS = {
    "maas-config-dataflow": "ocs-config-dataflow",
    "maas-config-completeness": "ocs-config-completeness",
    "maas-config-completeness-s3": "ocs-config-completeness-s3",
    "maas-config-completeness-s5": "ocs-config-completeness-s5",
    "maas-config-mission": "ocs-config-mission",
    "maas-config-satellite": "ocs-config-satellite",
    "maas-config-service": "ocs-config-service",
}

COLLECTOR_INDEX = "maas-config-collector"


class PatchV2_16_0(MaasMigrator):
    """Migration tool to migrate from 2.15 to 2.16"""

    def move_configuration(self, old_index, new_index):
        """install new_index, then copy old_index's documents into it

        A document already in new_index wins: a rerun, or one written there
        since, is left alone.
        """
        self.install_index(new_index)

        if not self.es_conn.indices.exists(old_index):
            self.logger.info("%s does not exist: nothing to copy", old_index)
            return

        self.logger.info("Copying %s into %s", old_index, new_index)

        if self.args.dry_run:
            return

        result = self.es_conn.reindex(
            body={
                "conflicts": "proceed",
                "source": {"index": old_index},
                "dest": {"index": new_index, "op_type": "create"},
            },
            refresh=True,
            request_timeout=self.args.es_timeout,
        )

        self.logger.info(
            "%s: %s documents copied, %s there already",
            new_index,
            result.get("created"),
            result.get("version_conflicts"),
        )

    def remap_collector_configuration(self):
        """put the maas-config-collector template; reindex the index when it
        does not map interface_name as a keyword yet"""
        if self.args.dry_run:
            self.logger.info("Dry run: %s template not put", COLLECTOR_INDEX)
            return

        self.update_template(COLLECTOR_INDEX)

        if not self.es_conn.indices.exists(COLLECTOR_INDEX):
            return

        mapping = self.es_conn.indices.get_mapping(index=COLLECTOR_INDEX)
        field = (
            mapping[COLLECTOR_INDEX]["mappings"]
            .get("properties", {})
            .get("interface_name", {})
        )
        if field.get("type") == "keyword":
            self.logger.info("%s maps interface_name already", COLLECTOR_INDEX)
            return

        self.migrate_index_template(COLLECTOR_INDEX, None)

    def run(self):
        """override"""
        for old_index, new_index in MOVED_CONFIGURATIONS.items():
            self.move_configuration(old_index, new_index)

        self.remap_collector_configuration()

        self.logger.warning(
            "Once the engines run on ocs-config-*, delete the old indices: %s",
            ", ".join(MOVED_CONFIGURATIONS),
        )


def run():
    migration_main(sys.argv[1:], PatchV2_16_0)


if __name__ == "__main__":
    run()
