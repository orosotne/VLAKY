"""Plánovač a vyhodnocovanie vlakov (ROZHRANIE – implementácia sa dopĺňa).

Verejné funkcie (ostatné moduly ich volajú, signatúry sú záväzné):

    evaluate_train(session, train_id, now=None, trigger="") -> Recommendation | None
        Prepočíta jeden vlak (trasa, výluky s rezervami, preberanie, pracovný čas rušňovodiča,
        semafor, rozhodnutie, návrh trasy). Uloží nový záznam Recommendation (ak sa výsledok
        zmenil oproti poslednému – podľa fingerprint) a vráti ho; inak vráti posledný.

    evaluate_all(session, now=None, trigger="") -> list[Recommendation]
        Prepočíta všetky aktívne vlaky (status mimo ukonceny/zruseny/odovzdany).

    plan_order(session, order_id, now=None) -> dict
        Navrhne varianty prepravy pre objednávku (bez uloženia vlaku): trasy, časy, rezervy.
"""
from __future__ import annotations


def evaluate_train(session, train_id, now=None, trigger=""):  # pragma: no cover - doplní sa
    raise NotImplementedError


def evaluate_all(session, now=None, trigger=""):  # pragma: no cover - doplní sa
    raise NotImplementedError


def plan_order(session, order_id, now=None):  # pragma: no cover - doplní sa
    raise NotImplementedError
