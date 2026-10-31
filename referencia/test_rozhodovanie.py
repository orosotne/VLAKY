"""Testy referenčnej rozhodovacej logiky.

Spustenie: python3 -m unittest discover -s referencia -v
"""
import os
import re
import sys
import unittest
from dataclasses import fields

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rozhodovanie import (  # noqa: E402
    CERVENA, ODSTAVIT, POCKAT, PRESMEROVAT, VYPRAVIT, ZELENA, ESKALOVAT,
    Closure, Params, Route, Segment, break_even_wait_hours, evaluate, recommend)

FAST = dict(samples=300)
# Takmer deterministické zdržanie (medián 0,01 min) - na testy presných časov.
EXACT = dict(delay_p50=0.01, delay_p90=0.02)


class TestRozhodovanie(unittest.TestCase):

    def test_bez_vyluky_vypravit_hned(self):
        r = Route("A-B", (Segment("A-B", 120, 2, 8),))
        rec = recommend([r], now=0, ready=60, deadline=600, p=Params(**FAST), horizon_min=120)
        self.assertEqual(rec.decision, VYPRAVIT)
        self.assertEqual(rec.chosen.status, ZELENA)
        self.assertEqual(rec.chosen.depart, 60)

    def test_okna_odchodu_pred_a_po_vyluke(self):
        # Výluka na 2. úseku 05:00-07:00, rezervy 30/30 -> úsek musí byť voľný mimo 04:30-07:30.
        # Vlak je v 2. úseku v čase [t+60, t+120]:
        #   pred výlukou: t + 120 <= 270  -> t <= 150 (najneskorší bezpečný odchod 02:30)
        #   po výluke:    t + 60  >= 450  -> t >= 390 (najskorší bezpečný odchod 06:30)
        c = Closure(300, 420)
        r = Route("A-C", (Segment("A-B", 60, **EXACT), Segment("B-C", 60, closures=(c,), **EXACT)))
        p = Params(deadline_buffer_min=0, **FAST)
        rec = recommend([r], now=0, ready=0, deadline=24 * 60, p=p, horizon_min=600, step_min=5)
        first, second = rec.green_windows["A-C"][:2]
        self.assertEqual(first[0], 0)
        # 150 je presná hranica; aj nepatrné zdržanie ju posunie o krok (5 min) skôr
        self.assertTrue(145 <= first[1] <= 150, first)
        self.assertEqual(second[0], 390)

    def test_vyluka_vynuti_cakanie(self):
        c = Closure(300, 420)
        r = Route("A-C", (Segment("A-B", 60, **EXACT), Segment("B-C", 60, closures=(c,), **EXACT)))
        p = Params(deadline_buffer_min=0, **FAST)
        rec = recommend([r], now=0, ready=180, deadline=24 * 60, p=p, horizon_min=600, step_min=5)
        self.assertEqual(rec.chosen.depart, 390)
        self.assertIn(rec.decision, (POCKAT, ODSTAVIT))

    def test_kratke_cakanie_pockat_dlhe_odstavit(self):
        p = Params(deadline_buffer_min=0, **FAST)
        self.assertAlmostEqual(break_even_wait_hours(p), 600 / (180 - 60))  # 5 h
        c = Closure(300, 420)
        r = Route("A-C", (Segment("A-B", 60, **EXACT), Segment("B-C", 60, closures=(c,), **EXACT)))
        # pripravený 04:00 -> čaká do 06:30 = 2,5 h < 5 h -> POČKAŤ
        short = recommend([r], now=0, ready=240, deadline=24 * 60, p=p, horizon_min=600, step_min=5)
        self.assertEqual((short.decision, short.chosen.depart), (POCKAT, 390))
        # výluka 05:00-13:00, pripravený 03:00 -> čaká do 12:30 = 9,5 h > 5 h -> ODSTAVIŤ
        c2 = Closure(300, 780)
        r2 = Route("A-C", (Segment("A-B", 60, **EXACT), Segment("B-C", 60, closures=(c2,), **EXACT)))
        long_ = recommend([r2], now=0, ready=180, deadline=24 * 60, p=p, horizon_min=900, step_min=5)
        self.assertEqual((long_.decision, long_.chosen.depart), (ODSTAVIT, 750))

    def test_vypnute_trakcne_vedenie_neobmedzuje_motorovy_rusen(self):
        c = Closure(0, 24 * 60, electric_only=True)
        seg = Segment("A-B", 60, 1, 3, (c,))
        p = Params(**FAST)
        ev_el = evaluate(Route("el", (seg,), electric=True), 60, 60, 3000, p)
        ev_di = evaluate(Route("di", (seg,), electric=False), 60, 60, 3000, p)
        self.assertEqual(ev_di.p_hold, 0)
        self.assertEqual(ev_el.p_hold, 1)

    def test_ziadne_miesto_na_cakanie_je_cervena(self):
        c = Closure(100, 200)
        r = Route("x", (Segment("A-B", 60, 1, 3, (c,), can_wait_before=False),))
        ev = evaluate(r, depart=80, ready=80, deadline=1000, p=Params(**FAST))
        self.assertEqual(ev.status, CERVENA)

    def test_nova_trasa_az_po_lehote(self):
        # Pridelená trasa o 01:00; iný odchod potrebuje novú trasu, ktorá je možná až now + 180 min.
        r = Route("A-B", (Segment("A-B", 60, 1, 3),))
        p = Params(**FAST)
        rec = recommend([r], now=0, ready=60, deadline=24 * 60, p=p, horizon_min=300,
                        step_min=10, allocated_departures=[60])
        departs = sorted(e.depart for e in rec.candidates)
        self.assertIn(60, departs)
        self.assertNotIn(120, departs)       # mimo tolerancie a pred lehotou na novú trasu
        self.assertIn(180, departs)          # now + 180
        self.assertTrue(all(e.needs_new_path for e in rec.candidates if e.depart >= 180))

    def test_presmerovanie_ked_hlavna_trasa_nevyhovuje(self):
        c = Closure(0, 24 * 60)  # hlavná trasa zavretá celý deň
        main = Route("hlavná", (Segment("A-B", 60, 1, 3, (c,)),))
        alt = Route("obchádzka", (Segment("A-X-B", 120, 2, 6),), is_primary=False)
        p = Params(**FAST)
        rec = recommend([main, alt], now=0, ready=240, deadline=900, p=p, horizon_min=240)
        self.assertEqual(rec.decision, PRESMEROVAT)
        self.assertTrue(rec.chosen.needs_new_path)

    def test_nic_bezpecne_eskalovat(self):
        r = Route("A-B", (Segment("A-B", 600, 5, 20),))
        rec = recommend([r], now=0, ready=0, deadline=300, p=Params(**FAST), horizon_min=60)
        self.assertEqual(rec.decision, ESKALOVAT)


class TestSuladSPromptom(unittest.TestCase):
    """Predvolené parametre v PROMPT.md (YAML blok) sa musia zhodovať s Params."""

    def test_yaml_v_prompte_zodpoveda_params(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "PROMPT.md"), encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"# PARAMETRE-ZACIATOK(.*?)# PARAMETRE-KONIEC", text, re.S)
        self.assertIsNotNone(m, "V PROMPT.md chýba blok # PARAMETRE-ZACIATOK ... # PARAMETRE-KONIEC")
        yaml_vals = {k: float(v) for k, v in
                     re.findall(r"^\s*([a-z_0-9]+):\s*([0-9.]+)", m.group(1), re.M)}
        defaults = Params()
        expected = {f.name: float(getattr(defaults, f.name)) for f in fields(Params)
                    if f.name not in ("samples", "seed")}
        self.assertEqual(set(yaml_vals), set(expected))
        for key, val in expected.items():
            self.assertAlmostEqual(yaml_vals[key], val, msg=key)


if __name__ == "__main__":
    unittest.main()
