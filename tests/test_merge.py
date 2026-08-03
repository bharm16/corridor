import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.merge import (
    STATION_TOLERANCE_FT,
    parse_station,
    rank_matches,
    resolve_org,
    score_match,
    station_score,
)
from corridor.models import Dependency, ExternalOrg, Project


# --------------------------------------------------------------------- units


def test_a_station_parses_to_feet():
    """`1149+00` is 114,900 feet along the alignment, not the number 114900."""
    assert parse_station("1149+00") == 114900.0
    assert parse_station("0+00") == 0.0
    assert parse_station("1143+44.01") == pytest.approx(114344.01)
    assert parse_station(" STA 1149+00 ") == 114900.0


def test_unparseable_stations_are_none_not_zero():
    """Zero is a real station. Guessing it would place a record at the origin."""
    for junk in ["", None, "NA", "n/a", "Crossing CL", "1149", "abc+de"]:
        assert parse_station(junk) is None


def test_overlapping_ranges_score_high():
    assert station_score("1149+00", "1153+17", "1149+00", "1153+17") == 1.0
    assert station_score("1149+00", "1153+17", "1150+00", "1152+00") > 0.9


def test_distant_ranges_score_zero():
    """`245+00` and `445+00` are near-identical as text, 20,000 feet apart."""
    assert station_score("245+00", "246+00", "445+00", "446+00") == 0.0


def test_adjacent_points_score_by_proximity():
    assert station_score("1149+00", None, "1149+00", None) == 1.0
    assert station_score("1149+00", None, "1149+40", None) > 0.0
    assert station_score("1149+00", None, "1180+00", None) == 0.0


def test_a_point_inside_a_range_scores_high():
    assert station_score("1150+00", None, "1149+00", "1153+17") > 0.9


def test_a_missing_station_scores_neutral_not_zero():
    """Minutes and email rarely carry stationing.

    Scoring absence as zero would rank every such candidate below every
    other, which is exactly backwards — absence is no evidence, not
    contrary evidence.
    """
    assert station_score(None, None, "1149+00", "1153+17") is None
    assert station_score("1149+00", None, None, None) is None


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session):
    p = Project(slug="merge-test", name="Merge Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def make_org(session, name, aliases=()):
    org = ExternalOrg(name=name, org_type="utility", aliases=list(aliases))
    session.add(org)
    session.flush()
    return org


def make_dep(session, project, org, ref, **kw):
    dep = Dependency(
        project_id=project.id,
        ref_code=ref,
        dep_type=kw.pop("dep_type", "utility_relocation"),
        title=kw.pop("title", "Telecom — MT AT&T"),
        external_org_id=org.id if org else None,
        status="identified",
        criticality="normal",
        **kw,
    )
    session.add(dep)
    session.flush()
    return dep


# ------------------------------------------------------------------ blocking


def test_aliases_collapse_one_party_named_many_ways(session):
    """AT&T appears as at least three strings across these documents."""
    org = make_org(
        session, "MT AT&T Texas (SWBT)", ["MT AT&T", "MT AT and T", "MT Southwestern Bell"]
    )
    for name in ["MT AT&T Texas (SWBT)", "MT AT&T", "MT AT and T", "MT Southwestern Bell"]:
        assert resolve_org(session, name).id == org.id


def test_alias_matching_ignores_case_and_spacing(session):
    org = make_org(session, "MT CenterPoint Energy", ["MT Center Point Energy"])
    assert resolve_org(session, "  mt centerpoint   energy ").id == org.id


def test_an_unknown_party_resolves_to_nothing(session):
    make_org(session, "MT AT&T Texas (SWBT)")
    assert resolve_org(session, "Nonexistent Utility Company") is None


def test_ranking_blocks_on_the_resolved_party(session, project):
    """A different owner is not a weak match. It is not a match."""
    att = make_org(session, "MT AT&T Texas (SWBT)", ["MT AT&T"])
    comcast = make_org(session, "MT Comcast")
    make_dep(session, project, att, "DEP-1", station_from="1149+00", station_to="1153+17")
    make_dep(session, project, comcast, "DEP-2", station_from="1149+00", station_to="1153+17")

    matches = rank_matches(
        session,
        project.id,
        {"external_org": "MT AT&T", "station_from": "1149+00", "station_to": "1153+17"},
    )
    assert [m.dependency.ref_code for m in matches] == ["DEP-1"]


# ------------------------------------------------------------------- scoring


def test_the_true_match_ranks_first(session, project):
    org = make_org(session, "MT AT&T Texas (SWBT)")
    make_dep(session, project, org, "DEP-far", station_from="1900+00", station_to="1905+00")
    right = make_dep(
        session, project, org, "DEP-right",
        station_from="1149+00", station_to="1153+17", title="Telecom — MT AT&T Texas (SWBT)",
    )
    make_dep(session, project, org, "DEP-other", station_from="1600+00", station_to="1610+00")

    matches = rank_matches(
        session,
        project.id,
        {
            "external_org": "MT AT&T Texas (SWBT)",
            "station_from": "1149+00",
            "station_to": "1153+17",
            "utility_type": "Telecom",
        },
    )
    assert matches[0].dependency.id == right.id
    assert matches[0].total > matches[1].total


def test_every_signal_reports_its_own_contribution(session, project):
    """The reason must be visible: when the top suggestion is wrong you have
    to see why, and an embedding that ranks badly just ranks badly."""
    org = make_org(session, "MT AT&T Texas (SWBT)")
    dep = make_dep(
        session, project, org, "DEP-1",
        station_from="1149+00", station_to="1153+17", title="Telecom — MT AT&T Texas (SWBT)",
    )
    match = score_match(
        {
            "external_org": "MT AT&T Texas (SWBT)",
            "station_from": "1149+00",
            "station_to": "1153+17",
            "utility_type": "Telecom",
        },
        dep,
    )
    names = {s.name for s in match.signals}
    assert {"station", "text"} <= names
    assert all(0.0 <= s.score <= 1.0 for s in match.signals)
    assert any(s.detail for s in match.signals)


def test_stationing_outweighs_text_similarity(session, project):
    """Two records with near-identical text 20,000 feet apart are not the same.

    This is the case embeddings destroy: `245+00` and `445+00` are one
    character apart as strings.
    """
    org = make_org(session, "MT CenterPoint Energy")
    near = make_dep(
        session, project, org, "DEP-near",
        station_from="245+00", station_to="246+00", title="Gas — MT CenterPoint Energy",
    )
    far = make_dep(
        session, project, org, "DEP-far",
        station_from="445+00", station_to="446+00", title="Gas — MT CenterPoint Energy",
    )
    matches = rank_matches(
        session,
        project.id,
        {
            "external_org": "MT CenterPoint Energy",
            "station_from": "245+00",
            "station_to": "246+00",
            "utility_type": "Gas",
        },
    )
    assert matches[0].dependency.id == near.id
    assert matches[0].total > next(m for m in matches if m.dependency.id == far.id).total


def test_text_alone_still_ranks_when_stationing_is_absent(session, project):
    """Minutes and email carry no stationing; the search must still work."""
    org = make_org(session, "MT CenterPoint Energy")
    gas = make_dep(session, project, org, "DEP-gas", title="Gas — MT CenterPoint Energy")
    make_dep(session, project, org, "DEP-elec", title="Electric — MT CenterPoint Energy")

    matches = rank_matches(
        session,
        project.id,
        {"external_org": "MT CenterPoint Energy", "utility_type": "Gas"},
    )
    assert matches[0].dependency.id == gas.id


def test_station_tolerance_is_the_documented_one():
    assert STATION_TOLERANCE_FT == 500.0
