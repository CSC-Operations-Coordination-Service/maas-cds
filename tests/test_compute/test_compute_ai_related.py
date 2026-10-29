import pytest

from maas_cds.engines.compute.compute_ai_related import ComputeAiRelatedEngine

from maas_cds import model


def _product(name, product_type, **extra):
    product = model.CdsProductS1(
        name=name,
        product_type=product_type,
        mission="S1",
        satellite_unit="S1C",
        sensing_start_date="2026-04-19T20:18:46.000Z",
        sensing_end_date="2026-04-19T20:31:17.000Z",
        **extra,
    )
    product.full_clean()
    return product


@pytest.fixture
def ai_raw_product():
    return _product(
        "S1C_AI_RAW__0____20260419T201846_20260419T203117_007290________2DFD.SAFE",
        "AI_RAW__0_",
        timeliness="_",
    )


@pytest.fixture
def aisaux_product():
    return _product(
        "S1C_OPER_AUX_AISAUX_20260419T201846_20260419T203117.EOF",
        "AISAUX",
    )


def test_ai_raw_sets_absolute_orbit_from_name(ai_raw_product):
    engine = ComputeAiRelatedEngine()

    completeness = engine.consolidate(
        ai_raw_product, model.CdsAiProductionCompleteness()
    )

    # Leading zeros dropped, as the datatake and HKTM orbits
    assert completeness.absolute_orbit == "7290"
    assert completeness.input_name == ai_raw_product.name
    assert completeness.completeness == 0


def test_ai_raw_keeps_orbit_when_aisaux_arrives(ai_raw_product, aisaux_product):
    engine = ComputeAiRelatedEngine()

    completeness = engine.consolidate(
        ai_raw_product, model.CdsAiProductionCompleteness()
    )
    completeness = engine.consolidate(aisaux_product, completeness)

    assert completeness.absolute_orbit == "7290"
    assert completeness.completeness == 1


def test_aisaux_alone_has_no_absolute_orbit(aisaux_product):
    engine = ComputeAiRelatedEngine()

    completeness = engine.consolidate(
        aisaux_product, model.CdsAiProductionCompleteness()
    )

    assert completeness.absolute_orbit is None
    assert completeness.completeness == 1


def test_ai_raw_without_orbit_in_name_leaves_it_unset():
    engine = ComputeAiRelatedEngine()
    product = _product("S1C_AI_RAW__0____malformed", "AI_RAW__0_")

    completeness = engine.consolidate(product, model.CdsAiProductionCompleteness())

    assert completeness.absolute_orbit is None
    assert completeness.input_name == product.name
