# Changelog

## Version 2.18.0

Requires maas-engine 3.2.0: QUERY_ENGINE re-emits query results under another class and on the raw exchange (ocs-operator replays).

### S3 missing orbits and the S3C commissioning numbering

- A S3 cycle no longer has a fixed number of orbits (385 until now). Across cycles, the
  missing orbit detection lists only the orbits of the latest cycle, from `<cycle>-001`:
  between `S3B-068-380` and `S3B-069-003` only `S3B-069-001` and `S3B-069-002` are
  missing, the end of cycle 068 is unknown.
- No missing orbit is generated when the numbering goes backwards while the time goes
  forward, or when more S3 orbits are missing than the elapsed time can hold (101
  minutes per orbit). This applies to COMPUTE_S3_COMPLETENESS and to
  COMPUTE_MISSING_COMPLETENESS_SPLITTED; the S5 datatakes skip the time check.
- COMPUTE_MISSING_COMPLETENESS_SPLITTED dated the generated orbits in the reverse order:
  the oldest missing orbit got the most recent estimated dates. They now go back in time
  from the current orbit.
- During the S3C commissioning, the numbering went `003-110 → 004-001 … 009-189 → 003-111`,
  and the nominal numbering reuses those identifiers later. A S3 datatake_id that is not
  the orbit expected at the product sensing time gets the `-COM` suffix
  (`S3C-004-001-COM`): its completeness documents never mix with the nominal orbits, and
  it is kept out of the missing orbit detection. The expected orbit is counted from the
  reference of the satellite, a nominal datatake and its sensing start, at 385 orbits per
  27 days, give or take 2 orbits. The references are `ORBIT_REFERENCES` in
  `S3DatatakeIdStrategy` (`maas_cds/lib/orbit_id_strategy.py`); a satellite without one
  is never tagged.

### Upgrade

Run `patch_2_18_0` (`--dry-run` first) before the S3C nominal numbering reaches
`004-001` again. For each satellite with a reference, it tags the abnormal products and
publications, moves their `cds-s3-completeness` and splitted completeness documents under
the tagged key, and deletes the completeness documents generated as missing from the
broken numbering: those not matching their observation time, without products. It scans
every untagged product and publication of those satellites. S3C is the only satellite with
a reference (`S3C-003-121`, 2026-10-26T08:32:12Z); its numbering was broken from 2026-09-21
to 2026-10-20.

Replaying the S3C products of that period re-tags them and builds the tagged completeness,
but deletes nothing: run the patch anyway, after the replay, to remove the untagged
leftovers.

## Version 2.17.0

### Deletion tickets follow their attachments

- CONSOLIDATE_DELETION can now keep the deletions of a ticket in line with the files
  attached to it. When an attachment is replaced by a newer file for the same service,
  uploaded again with products removed, or detached from a ticket that keeps another
  attachment, the products and publications that only it listed are no longer flagged
  deleted, and its rows in `cds-interface-product-deletion` are deleted.
- The rollback only clears what the ticket set. On a product, it removes the
  `<interface>_<service>_is_deleted`, `_deletion_issue`, `_deletion_date` and
  `_deletion_cause` fields that carry the ticket key, then recounts `nb_dd_deleted` or
  `nb_lta_deleted`. On a publication, it removes `deletion_issue`, `deletion_date` and
  `deletion_cause`.
- An attachment is identified by its JIRA identifier, which changes at every upload,
  even when the file does not. maas-collector 3.10.0 and later stamp it on each row as
  `attachment_id` and `attachment_created` when the JIRA collector configuration of the
  deletion tickets sets `"attachment_extra_fields": {"attachment_id": "id",
  "attachment_created": "created"}` and `"attachment_date_fields":
  ["attachment_created"]`. The ticket gives its current attachments in `attachment_ids`
  and `attachments`: add ``"attachment_ids": "`this`.fields.attachment[:].id"`` and
  ``"attachments": "`this`.fields.attachment[:].filename"`` to its extractor, which
  must keep `"allow_partial": true`, as the deployed ones do: otherwise a ticket
  without attachments fails its extraction. A ticket with one attachment gets them as
  a single value, which reads as a list of one.
- Until that configuration changes, nothing changes: a row without `attachment_id` is
  never rolled back, and a ticket without `attachment_ids` never has a detached
  attachment. Do not change it yet, see "Do not turn on yet" below.
- An attachment belongs to the service its file name names: `werum`, `exprivia`,
  `acri`, `cloudferro`, `dlr`, `cdse`, `dhus`, or the service name itself (new option
  `interface_dict`). When the name names none, it belongs to the only deletion interface
  of the ticket. Within a service, the most recent attachment wins.
- When all the rows of a ticket come from attachments it no longer lists, for example
  while the rows of a new upload are not collected yet, the whole ticket is rolled back,
  with the warning `every attachment is superseded or detached: rolling back the whole
  ticket`. Removing every attachment from the ticket rolls nothing back, see "Do not
  turn on yet" below.
- Tickets of more than 10000 rows or products are handled in full. Before, only the
  first 10000 rows, products and publications were read.
- When its `send_reports` option is true, CONSOLIDATE_DELETION now reports the products
  and publications it marks or clears on the mission routing keys
  (`update.cds-product-s2`, `update.cds-publication-s2`, ...), which recomputes their
  completeness. 2.16 never sent these reports. The option defaults to false.
- The `raw-data-product-deletion` and `cds-interface-product-deletion` templates gain
  `attachment_id` (keyword) and `attachment_created` (date). The
  `raw-data-deletion-issue` and `cds-deletion-issue` templates gain `attachments` and
  `attachment_ids` (keyword). `cds-interface-product-deletion` also maps
  `ingestionTime`.

### How CONSOLIDATE_DELETION processes a message

- A ticket is handled chunk by chunk: the engine looks up the products and publications
  of `terms_chunk_size` names (1024 by default), hands their changes to the bulk, which
  writes them 500 at a time, then moves on to the next names. A big ticket only takes
  longer.
- A `CdsInterfaceProductDeletion` message looks up only the names it carries, unless an
  attachment of the ticket is superseded: the rollback then needs the whole ticket. A
  `CdsDeletionIssue` message still looks up every name of its tickets.
- A `CdsInterfaceProductDeletion` message reads the other rows of its tickets only when
  an attachment identity is recorded, on the ticket (`attachment_ids`) or on one of its
  rows (`attachment_id`), which one count per ticket tells. Then it reads every row of
  the ticket, 4 fields each: a ticket of N rows, sent 128 rows per message, reads about
  N²/128 rows over its first ingestion. Until the collector configuration changes, it
  reads none.
- `max_documents` (50000 by default) limits the documents matched by one chunk. Above
  it, the match is pathological: the message is rejected and logged as
  `NORMAL_REJECTION` instead of being requeued, and the later tickets of the message are
  dropped with it. The changes already sent stay written, but those still waiting for
  a bulk request, up to 500, are dropped with the message, and the rows of a superseded
  attachment are kept.
- `max_documents` also limits the documents written for every ticket of a message but
  the last, whose identifiers the engine keeps until the end of the run. Above it, the
  rest of the message is left to its retry: the engine requeues it once the bulk is
  over, and the retry finds the documents already written unchanged. A message that
  writes W such documents takes about W / `max_documents` + 1 runs.
- When several tickets of a message list a product, the last one marks it: the deletion
  issue, date and cause of a service on the product, and of the publication on that
  service, are the ones of that ticket. The tickets come in payload order in a
  `CdsDeletionIssue` message, in ticket key order in a `CdsInterfaceProductDeletion`
  message. A ticket that only rolls the product back does not count. A ticket listed
  twice in a message is applied once.
- A product or publication changed by several tickets of a message, on different
  services or by a rollback, is written once per run: the message is requeued to
  finish it, once per further ticket that changes it.
- The rows of a superseded attachment are deleted in a bulk of their own, once a run
  has written every change of the message with no error and left nothing to a retry. A
  run that ends in a requeue keeps them, so that its retry reconciles the same tickets
  and redoes a rollback or a mark that failed.
- With `send_reports` enabled, the products and publications written by a run that
  ends in a requeue are not reported: the consumer only sends the reports of a message
  it acknowledges, and the retry finds them unchanged.

### S3P session completeness

- CONSOLIDATE_S3P_SESSION computes the sensing completeness of each session from the
  validity of its L0PP granules. The new `completeness` list has one entry per level-0
  product type of `ocs-config-completeness-s3`, plus `OL_0_CR____` (44 minutes), with
  `product_type`, `value`, `expected`, `value_adjusted`, `percentage`, `status`,
  `sensing_start_date` and `sensing_stop_date`.
- A product type the session misses reads 0%. It is dated at the middle of the sensing
  the session covers, or of its acquisition window, so that it shows on the time axis.
- A granule type counts as its full orbit type: `TM_0_NAT__G` as `TM_0_NAT___`. When
  the configuration has several timeliness for a product type, the `NR` record gives the
  expected sensing.
- The session also gets a completeness over all its product types: `global_value`,
  `global_expected`, `global_value_adjusted`, `global_percentage` and `global_status`.
- Each L0PP granule added from now on records its `timeliness`, read from its name.
- The engine loads `ocs-config-completeness-s3` once per process: restart it after a
  change. Without that index, it logs `No S3 completeness configuration: the session
  completeness will not be computed`, and the sessions only get the `OL_0_CR____` entry.

### S3P missing downlink orbits

- When CONSOLIDATE_S3P_SESSION consolidates a session that holds a `TM_0_HKM__G`
  granule, it looks for the previous session of the satellite, by downlink orbit. For
  each downlink orbit in between, it creates a session
  `<satellite>_MISSING_______<orbit>`, with `is_missing: true` and every expected
  product type at 0%.
- Their acquisition times are spread over the time between the two sessions, or one
  nominal 101-minute orbit apart when the previous session has no date. When the new
  session itself has no date, they are created undated, with the warning `No date to
  place the <count> missing session(s) on the time axis`. Only the 100 most recent
  missing orbits of a gap are created, with a warning.
- When a session arrives for an orbit that has a missing session, the missing session
  is deleted.
- Real sessions have no `is_missing` field: leave the missing ones out with
  `NOT is_missing:true`, not with `is_missing:false`.
- The `s3p-session` template gains `is_missing` (boolean), `l0pp_granules.timeliness`,
  the `completeness` object and the `global_*` fields.

### Other changes

- The S3 and S5 completeness share one implementation of the orbit arithmetic that
  finds the missing datatakes. It finds the same datatakes as before.

### Documentation

- New page "Product type update": how to add or change a product type in the dataflow
  configuration, and bring the existing products and publications in line.

### Do not turn on yet

- **Attachment reconciliation.** Keep the deletion ticket collector configuration as it
  is: four known problems are fixed in a later release.
  - Attachments are grouped by service only. Two lists of one service on one ticket,
    for example one per satellite, do not add up: the older one is rolled back and its
    rows are deleted.
  - Rolling a product back for one ticket clears the mark of that service even when
    another ticket still lists the product and its mark was overwritten by the first.
  - Removing the last attachment of a ticket rolls nothing back: the ticket then has no
    `attachment_ids`, which reads as a ticket collected before attachment identities,
    and its products and publications stay flagged deleted.
  - A new upload is taken as current from its first rows: the first message of its rows
    rolls back every product of the ticket whose new row is not tracked yet, and the
    next messages mark them again, a message at a time.
- **CONSOLIDATE_S3P_SESSION on 2.17.** A missing session is deleted from `s3p-session`
  when its real session arrives, but an export of `s3p-session` to another store keeps
  it. Make such an export drop the missing sessions an orbit's real session replaces
  before the S3P engine runs 2.17.

### Upgrade

- From 2.15 or older, do the 2.16.0 upgrade first, `patch_2_16_0` included: the S3P
  engine reads `ocs-config-completeness-s3`.
- 2.17.0 has no patch command, and the engines never put templates. Before you start
  the 2.17 engines, put the five changed templates (`cds-deletion-issue`,
  `cds-interface-product-deletion`, `raw-data-deletion-issue`,
  `raw-data-product-deletion` and `s3p-session`) and add their new fields to the
  mapping of the existing indices (`PUT <index>/_mapping`): a template only applies to
  the indices created after it. A field written before that is mapped dynamically,
  `global_status` and the other keywords as `text` and `global_value` as `long` or
  `float`, depending on the first session written, and only a reindex changes it.
  `s3p-session` is a single index. Read the mapping of an index first (`GET
  <index>/_mapping`) and leave out of the `PUT` the fields it already maps:
  `ingestionTime` in particular, already mapped as a `date` when it was written, is
  rejected with a different `format`.
- Existing sessions get their completeness when they are next consolidated. Missing
  orbits are only looked for from the sessions consolidated after the upgrade, back to
  the previous session of the satellite.

### Known issue

- COMPUTE_COMPLETENESS_V2 still rejects the S1 and S2 messages with
  `CdsCompleteness.find_brother_products_scan() got an unexpected keyword argument
  'include_deleted'`, as in 2.15 and 2.16.

## Version 2.16.0

### OCS configurations move to `ocs-config-*`

- The configurations that only OCS uses move to `ocs-config-*` indices:
  `maas-config-dataflow`, `maas-config-completeness`, `maas-config-completeness-s3`,
  `maas-config-completeness-s5`, `maas-config-mission`, `maas-config-satellite` and
  `maas-config-service` become `ocs-config-dataflow`, `ocs-config-completeness`,
  `ocs-config-completeness-s3`, `ocs-config-completeness-s5`, `ocs-config-mission`,
  `ocs-config-satellite` and `ocs-config-service`. `maas-config-*` keeps what the MAAS
  framework itself configures, such as the collectors in `maas-config-collector`.
- The 2.16 engines read the dataflow and completeness configurations from
  `ocs-config-*` only. Engines still on 2.15 keep reading `maas-config-*`.
- The templates are renamed after the new indices (`ocs-config-*_template.json`), and
  the configuration classes become `OcsConfigDataflow`, `OcsConfigCompleteness`,
  `OcsConfigCompletenessS3`, `OcsConfigCompletenessS5`, `OcsConfigMission`,
  `OcsConfigSatellite` and `OcsConfigService`. Code that imports `MaasConfigDataflow`
  and the other old names, or asks `MaasConfigManager` for them, must use the new ones.
- The mission, satellite and service templates gain the `valid_from` and `valid_to`
  dates, which ocs-operator-backend fills.

### Migration `patch_2_16_0`

- New command `patch_2_16_0`, with the options of the other patches: `-d` /
  `--dry-run`, `-r` / `--resources` (`/app/resources` by default, as in the image) and
  the OpenSearch connection settings.
- For each of the seven configurations, it puts the `ocs-config-*` template, creates
  the index if needed, then copies the documents of the old `maas-config-*` index into
  it, with their ids. It never overwrites a document already in the new index, so it
  can run again: it logs `<index>: N documents copied, M there already`. A missing old
  index is skipped.
- It also puts the new `maas-config-collector` template. When the
  `maas-config-collector` index does not map `interface_name` as a keyword yet, the
  patch reindexes it with this template: the index is copied to
  `migrating-maas-config-collector`, deleted and filled again, ids kept. Do not write
  to it while the patch runs.
- It neither changes nor deletes the seven old indices, nor their templates. It ends
  with a warning that lists the old indices, to delete by hand once the engines run on
  `ocs-config-*`.
- With `--dry-run`, it logs the templates it would put and the copies it would make,
  and writes nothing. It does not check whether `maas-config-collector` needs the
  reindex.

### TRACK_DELETION and the collector configurations

- TRACK_DELETION finds the collector configuration of an interface by the
  `interface_name` field of the `maas-config-collector` documents, instead of using the
  interface name as the document id: the configuration API gives the documents it
  creates counter ids. It also finds them in an index not remapped yet, through
  `interface_name.keyword`.
- When several documents have the same `interface_name`, TRACK_DELETION uses none of
  them. It logs `Several collector configs for <interface>: none is used` and, as when
  there is no configuration, does not check the products of that interface.
- The `maas-config-collector` template maps `interface_name` as a `keyword` and marks
  the index free-form (`_meta.free_form`), for the configuration API that maas-model
  generates.

### Connections from OpenBao / HashiCorp Vault

- `el_grando_satruman` can read its OpenSearch and AMQP connections from OpenBao or
  HashiCorp Vault, like the engines. Set `ES_VAULT_PATH` and `AMQP_VAULT_PATH`
  (`--es-vault-path`, `--amqp-vault-path`) to the secrets that hold each connection's
  `url`, `username` and `password`, and the vault settings, each with a matching
  `--vault-*` option: `VAULT_ADDR`, `VAULT_KV_MOUNT`, `VAULT_AUTH_METHOD`, then
  `VAULT_ROLE_ID` and `VAULT_SECRET_ID_FILE` for AppRole, or `VAULT_TOKEN_FILE` (else
  `VAULT_TOKEN`) for a token. Without this fix, once maas-engine 3.1.0 was installed,
  a vault path made it stop with an `AttributeError` on `vault_addr`.
- The engines and the `patch_*` migrations, `patch_2_16_0` included, take the same
  settings through maas-engine 3.1.0. The migrations only open the OpenSearch
  connection. maas-engine's `docs/vault.md` describes the secrets, the deployment
  behind a proxy and the errors at startup.

### Fixes

- The expected tiles of a Sentinel-2 product whose footprint crosses the antimeridian
  (longitude ±180°) are now searched with the footprint cut there, into parts that
  stay within [-180, 180]. The old correction dropped the points within 0.01° of
  ±180°, which could leave an unclosed ring that OpenSearch rejected. The search then
  fell back on the original footprint, which across the antimeridian matched a band
  around the globe and thousands of tiles. When OpenSearch rejects a cut footprint, the
  search now uses the bounding box of each part: a few tiles too many, but only around
  the footprint.

### Requirements

- maas-cds now requires maas-engine 3.1.0 or a later 3.x (was `>=3.0.1`) and
  maas-collector 3.11.0 or a later 3.x (was unpinned), their OpenBao / Vault releases.
  Both bring maas-model 3.1.0 or later with its `vault` extra, which installs `hvac`.
- The requirements read `maas-engine>=3.1.0.dev0,<4` and
  `maas-collector>=3.11.0.dev0,<4`. The `.dev0` lets pip also install their
  development builds made after those releases, like 3.1.0.post1.devN, from the Nexus
  dev index, which holds no releases.

### Upgrade

- Run `patch_2_16_0` before starting any 2.16 engine, first with `--dry-run`, then for
  real. The 2.16 engines read their dataflow and completeness configurations from the
  `ocs-config-*` indices that the patch creates and fills.
- From the patch until every engine runs 2.16, do not edit the seven old
  `maas-config-*` indices. The patch never overwrites a document it already copied, so
  running it again would not carry the edit over: the 2.16 engines would never see it,
  and it would be lost with the old index.
- Before TRACK_DELETION runs on 2.16, check that each interface has a single
  `maas-config-collector` document: when it has several, none is used.
- A snapshot policy that selects `maas-*` does not cover the new `ocs-config-*`
  indices. Add them to a snapshot policy, and check that a snapshot holds them, before
  you delete the old indices.
- Once every engine runs 2.16 and nothing else reads them, delete the seven old indices
  by hand. Their `template_maas-config-<kind>` templates can go with them. Keep
  `maas-config-collector` and its template, which stay in use.

Earlier versions are only recorded in the git history.
