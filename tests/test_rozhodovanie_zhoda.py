"""Kanonický modul vlaky/rozhodovanie.py musí dávať rovnaké výsledky ako referencia."""
import importlib.util
import os
import sys

from vlaky import rozhodovanie as kanon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("ref_rozhodovanie", os.path.join(ROOT, "referencia", "rozhodovanie.py"))
ref = importlib.util.module_from_spec(spec)
sys.modules["ref_rozhodovanie"] = ref  # dataclasses potrebujú modul v sys.modules
spec.loader.exec_module(ref)


def _run(mod):
    p = mod.Params(samples=800)
    primary, detour = mod.example()
    out = []
    for ready, routes, alloc in [(330, [primary], [330]), (390, [primary], [330]), (390, [primary, detour], [330])]:
        rec = mod.recommend(routes, now=ready - 30, ready=ready, deadline=1080, p=p, horizon_min=720,
                            step_min=10, allocated_departures=alloc)
        out.append((rec.decision, rec.chosen.route, rec.chosen.depart, round(rec.chosen.expected_cost, 6),
                    rec.chosen.status))
    return out


def test_zhoda_s_referenciou():
    assert _run(kanon) == _run(ref)


def test_parametre_zhodne():
    assert kanon.Params() == ref.Params() or vars(kanon.Params()) == vars(ref.Params())
