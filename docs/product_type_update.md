# Product type update

How to add or change a product type in the dataflow configuration
(`maas-config-dataflow`) and bring existing data in line with it.

The worked example throughout is the S5P `MPL_SPF___` product
(`S5P_OPER_MPL_SPF__20260726T000000_20260823T000000_3201.TGZ`): first producer
FOS, consumed by the Production Service, archived by the DLR and ACRI LTAs.

## How the dataflow configuration is used

`maas-config-dataflow` holds one document per dataflow version. Each document
carries `name`, `version`, `key`, `latest` and a list of `records`, one per
`(mission, product_type)`:

```json
{
  "mission": "S5",
  "satellites": ["S5P"],
  "product_type": "MPL_SPF___",
  "product_level": "AUX",
  "services_config": {
    "fos":  ["P0"],
    "PRIP": ["C-fos", "P"],
    "LTA":  ["C-PRIP", "P"]
  }
}
```

- `services_config` keys are the service keys of the operator app (`PRIP`,
  `LTA`, `DD`, `DA`, `MPCIP`, `fos`, `pod`, `MPIP`, `AUXIP`, ...).
- Values: `P0` = primary producer (one per record), `P` = producer,
  `C-<key>` = consumer of `<key>`.
- An interface is "expected" for a product type when its list holds an item
  starting with `C` or `P` (splitted completeness and datatake deletion
  handling use that rule).

Only the documents with `latest: true` are applied.
`MaasConfigDataflow.load()` merges the records of **all** latest documents and
raises `MaasConfigError` when the same `(product_type, mission, product_level)`
appears twice.

> The product level stored on `cds-product` / `cds-publication` does **not**
> come from `maas-config-dataflow`. It comes from the legacy `cds-dataflow`
> index, looked up by `mission#product_type`
> (`BaseProductConsolidatorEngine.get_product_level`). That index is loaded
> from the dataflow CSV (`Technical-Dashboard/configuration/collector/csv/dataflow/`).
> A new product type needs a row there too, see step 3.

## Prerequisites

- The product name parses to the expected `product_type`. Check it with
  `maas_cds.lib.parsing_name.extract_data_from_product_name(<name>)`. If it
  does not (as for `MPL_SPF__`, whose type is 9 characters with no separator
  before the date), ship the parser fix in maas-cds **before** this procedure.
- Access to the operator app (Grafana plugin, *Dataflow* page) and to
  OpenSearch Dev Tools.
- Rights to restart the `maas-engine` service.

## Step 1 - Duplicate the applicable dataflow

1. Find the dataflow currently applied:

   ```
   GET maas-config-dataflow/_search
   {
     "query": { "term": { "latest": true } },
     "_source": ["key", "name", "version", "latest"]
   }
   ```

   Exactly one document is expected. If several are returned, the records
   are split across them; duplicate the one that holds the mission you change.

2. In the operator app, *Dataflow* page, click **Create New Dataflow**.
3. In **Duplicate records from an existing dataflow**, select the latest
   dataflow found above.
4. Fill **name** and **version** (step 2), then **Create**.

The backend copies the source records into the new document and always saves
it with `latest: false`, so nothing changes for the engines at this point.

## Step 2 - Increment the Z version

Versions follow `x.y.z`. A product type added or changed is a patch: increment
`z` only.

| Current latest | New document |
| --- | --- |
| `1.5.0` | `1.5.1` |
| `1.5.1` | `1.5.2` |

- Keep the same `name`. If the name embeds the version (for example
  `... DATA FLOW CONFIGURATION 1.5`), update it the same way.
- Never reuse a version that already exists in the index.

## Step 3 - Perform the update

On the new document (the page opens on it after creation):

1. Add the record, or edit the existing one, in the dataflow table.
   `product_type` must be exactly what the parser returns.
2. Check the whole record:
   - `mission` and `satellites` (`S5` / `["S5P"]`)
   - `product_level` (`AUX`)
   - one `P0`, and every `C-<key>` pointing to a service used in the record.
3. Save. The backend `PUT` saves the whole document.
4. Add the same product type to the legacy `cds-dataflow` CSV
   (`S5`, `MPL_SPF___`, level `AUX`) and let the CSV collector ingest it.
   Without this row, products get `PRODUCT_LEVEL_MISSING_VALUE` as level and
   do not appear in the dashboards filtered by `product_level`.
5. Check the new document holds no duplicated `(product_type, mission,
   product_level)`.

## Step 4 - Switch the applicable version

Setting `latest` on a document does **not** clear it on the others. Switch both
by hand, one right after the other:

1. Open the previous latest document, untick **Latest**, save.
2. Open the new document, tick **Latest**, save.
3. Check that only the new document is returned:

   ```
   GET maas-config-dataflow/_search
   {
     "query": { "term": { "latest": true } },
     "_source": ["key", "name", "version"]
   }
   ```

Do not restart any engine between 1 and 2. Engines read the configuration only
at start-up:

- no latest document: engines start with no records (completeness and
  deletion handling silently lose the dataflow)
- two latest documents with the same records: `MaasConfigError`, the engine
  fails to start.

Rollback = the same switch in the other direction, then step 5.

## Step 5 - Restart the engines to refresh the cache

maas-cds has no refresh event for configuration. Both caches are
process-wide and filled once, when the engine starts:

| Cache | Filled from | Engines that use it |
| --- | --- | --- |
| `MaasConfigManager.CACHE["MaasConfigDataflow"]` | `maas-config-dataflow` (latest) | `COMPUTE_COMPLETENESS`, `COMPUTE_COMPLETENESS_V2`, `COMPUTE_COMPLETENESS_SPLITTED`, `COMPUTE_MISSING_COMPLETENESS_SPLITTED` |
| `BaseProductConsolidatorEngine.LEVEL_TYPE_MAPPING` | `cds-dataflow` | `CONSOLIDATE_PRODUCT`, `CONSOLIDATE_LTA_PRODUCT`, `CONSOLIDATE_PUBLICATION`, DD product consolidation |
| `CdsS5Completeness._S5_PRODUCTS_TYPES` | `cds-dataflow` (mission `S5`) | `COMPUTE_S5_COMPLETENESS` |

All those engines run in the `maas-engine` service. Restart every replica:

```bash
# Swarm stack "maas" (csc-ocs-ansible/stacks/apps/maas)
docker service update --force maas_maas-engine
docker service ps maas_maas-engine   # all replicas Running, recent start time
```

On deployments where engines run in separate containers (one engine
configuration per container), restart each container whose configuration lists
one of the engines above.

Then check the engine logs:

- `Loading MaasConfigDataflow configuration` without `MaasConfigError`
- `Loaded dataflow product level-type mapping : <n>`, with `n` one more than
  before when a product type was added.

## Step 6 - Migrate historical data

Products and publications already stored keep the values computed before the
change. How to fix them depends on what changed:

| What changed | Fix |
| --- | --- |
| Parser (product type, sensing dates) | Replay the interfaces (6.1) |
| Product level only | Update by query (6.2) |
| `services_config` only | Recompute completeness (6.3) |

### 6.1 Replay the interfaces

Replay recollects a period and forces database updates, so the raw documents
go through consolidation again with the new parser and level mapping.
Consolidated ids are md5 sums of the product name (products) or interface +
product name (publications), so documents are updated in place.

```bash
python -m maas_collector.rawdata.cli.odata -v \
  --replay-interface-name PRIP_S5P_DLR LTA_S5P_DLR LTA_Acri \
  --replay-start-date 2026-07-01T00:00:00Z \
  --replay-end-date 2026-09-28T00:00:00Z
```

- Start date = first publication of the product type.
- Split long periods into chunks (a week or a month) to limit the load on the
  bus.
- A PRIP keeps products only for its rolling retention: older publications
  cannot be replayed from the PRIP. LTA replay covers the full history.

### 6.2 Update the product level by query

When only the level changed and the product type was already parsed correctly:

```
POST cds-product*/_update_by_query?conflicts=proceed
{
  "query": { "bool": { "filter": [
    { "term": { "mission": "S5" } },
    { "term": { "product_type": "MPL_SPF___" } }
  ] } },
  "script": { "source": "ctx._source.product_level = 'AUX'", "lang": "painless" }
}
```

Run the same request on `cds-publication*`. Check it first on a small period,
and never run it on documents whose product type is wrong (use 6.1).

### 6.3 Recompute completeness

Splitted completeness is computed when a product or publication is created or
updated. After a `services_config` change, a replay (6.1) of the period
recomputes it. Product types without a datatake or orbit (AUX, MPL) have no
orbit completeness: nothing to recompute for them.

### Check

```
GET cds-product*/_search
{
  "size": 0,
  "query": { "bool": { "filter": [
    { "term": { "mission": "S5" } },
    { "prefix": { "product_type": "MPL_SPF" } }
  ] } },
  "aggs": {
    "type":  { "terms": { "field": "product_type" } },
    "level": { "terms": { "field": "product_level" } }
  }
}
```

Only `MPL_SPF___` / `AUX` are expected. Any other value (for example
`MPL_SPF__2`, from the old parser) is a document left to migrate.

## Checklist

- [ ] Parser returns the expected `product_type` (fix released if needed)
- [ ] New dataflow duplicated from the latest one, `latest: false`
- [ ] Version `x.y.z+1`, name updated if it embeds the version
- [ ] Record added and saved, no duplicated key
- [ ] `cds-dataflow` CSV row added and ingested
- [ ] Old latest unticked, new latest ticked, one latest document left
- [ ] `maas-engine` restarted, logs checked
- [ ] Historical data migrated and checked
- [ ] Dashboards: product type visible (optional: add it to the mission preset links)
