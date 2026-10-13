# Changelog

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

- maas-cds now requires `maas-engine>=3.1.0,<4` (was `>=3.0.1`) and
  `maas-collector>=3.11.0,<4` (was unpinned), their OpenBao / Vault releases. Both
  bring `maas-model[vault]>=3.1.0,<4`, which installs `hvac`.

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
- Once every engine runs 2.16 and nothing else reads them, delete the seven old indices
  by hand. Their `template_maas-config-<kind>` templates can go with them. Keep
  `maas-config-collector` and its template, which stay in use.

Earlier versions are only recorded in the git history.
