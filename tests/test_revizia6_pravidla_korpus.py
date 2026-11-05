"""6. revízia – korpus formulácií FC0 – FC7 a blízkych variantov (strana pravidiel FA).

Prípady s pôvodom „r6-pravidla“ v ``tests/data/posta_korpus/oracle.json`` (formulácie zo záverečného overenia a vyše
40 blízkych variantov) prejdú tým istým porovnaním s orákulom ako celý korpus (``test_revizia5_korpus``) a navyše:

* zmena výluky má zámer z orákula (``zamer``: presun, doplnenie, predĺženie, skrátenie, zmena času),
* zrušenie len časti výluky bez určeného termínu (v orákule ``bez_terminu``) má príznak ``partial`` a žiadny termín
  (zápis ho odmietne),
* žiadny prípad nedá nebezpečný výsledok (stratený termín, zrušenie navyše, miernejší druh).

Presnú zhodu (s ``xfail`` pri známych konzervatívnych odchýlkach) overuje ``test_korpus_presne``.
"""
from __future__ import annotations

import pytest

from test_revizia5_korpus import CASES, _judge, _run, korpus  # noqa: F401 – fixture korpus

R6 = [c for c in CASES if str(c.get("origin", "")).startswith("r6-pravidla")]


def _ids(c: dict) -> str:
    return c["id"]


def test_korpus6_rozsah():
    """Korpus má každú formuláciu FC0 – FC7, sieť proti strate termínu a aspoň 40 blízkych variantov."""
    groups = {c["origin"].split()[1] for c in R6}
    assert {"FC0", "FC1", "FC2", "FC3", "FC4", "FC5", "FC6", "FC7", "sieť"} <= groups, groups
    assert len([c for c in R6 if "blízky variant" in c["origin"]]) >= 40
    assert len(R6) >= 100 and len({c["id"] for c in R6}) == len(R6)
    # fiktívny obsah: len adresy *.example
    assert all(str(c.get("sender", "")).endswith(".example") for c in R6)


@pytest.mark.parametrize("case", R6, ids=_ids)
def test_korpus6_bezpecne_zamer_a_ciastocne_zrusenie(korpus, case):
    """Bez nebezpečného výsledku; zámer zmeny a čiastočné zrušenie bez termínu podľa orákula."""
    items = _run(korpus, case)
    _inexact, unsafe = _judge(case, items)
    assert not unsafe, f"{case['id']}: " + "; ".join(unsafe)
    for x in case["expect"]:
        if x.get("zamer"):
            zamers = [it.data.get("zamer") for it in items if it.kind == "vyluka_zmena"]
            assert x["zamer"] in zamers, f"{case['id']}: zámer {zamers} != {x['zamer']}"
        if x.get("bez_terminu"):
            part = [it for it in items if it.kind == "vyluka_zrusenie" and it.data.get("partial") is True
                    and not it.data.get("start") and not it.data.get("end")]
            assert part, f"{case['id']}: chýba zrušenie časti výluky bez termínu: " + \
                "; ".join(f"{it.kind} {it.data}" for it in items)
