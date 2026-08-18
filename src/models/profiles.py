"""Profils de risque pre-regles.

Un profil fixe d'un coup les sept parametres correles (largeur de plage, nombre
de niveaux, exposition, seuils de perte). Un debutant choisit un mot au lieu de
sept nombres, et ne peut pas produire une combinaison incoherente.

Toute valeur ecrite explicitement dans le YAML a la priorite sur le profil.
"""

from __future__ import annotations

from typing import Any, Literal

ProfileName = Literal["prudent", "equilibre", "agressif"]

# atr_multiple : largeur de la plage, en multiples de la volatilite journaliere
# recente (ATR 14). Plus la plage est large, plus le prix reste dedans
# longtemps, mais moins les cycles sont frequents.
PROFILES: dict[str, dict[str, Any]] = {
    "prudent": {
        "_description": (
            "Plage tres large, peu de niveaux, pertes plafonnees bas. "
            "Le bot trade peu mais sort rarement de sa plage."
        ),
        "strategy": {
            "atr_multiple": 4.0,
            "target_grids": 8,
            "active_orders_per_side": 3,
            "leverage": 1,
        },
        "risk": {
            "max_daily_drawdown_pct": 1.0,
            "stop_loss_pct": 3.0,
            "max_position_size_pct": 30.0,
        },
    },
    "equilibre": {
        "_description": (
            "Compromis par defaut : plage moyenne, cycles reguliers, "
            "pertes plafonnees a 2% par jour."
        ),
        "strategy": {
            "atr_multiple": 3.0,
            "target_grids": 12,
            "active_orders_per_side": 5,
            "leverage": 1,
        },
        "risk": {
            "max_daily_drawdown_pct": 2.0,
            "stop_loss_pct": 5.0,
            "max_position_size_pct": 40.0,
        },
    },
    "agressif": {
        "_description": (
            "Plage serree, beaucoup de niveaux, plus de cycles mais le prix "
            "sort de la plage bien plus souvent. Reserve au testnet."
        ),
        "strategy": {
            "atr_multiple": 2.0,
            "target_grids": 20,
            "active_orders_per_side": 8,
            "leverage": 1,
        },
        "risk": {
            "max_daily_drawdown_pct": 3.0,
            "stop_loss_pct": 8.0,
            "max_position_size_pct": 60.0,
        },
    },
}


def describe(name: str) -> str:
    profile = PROFILES.get(name)
    return profile["_description"] if profile else "profil inconnu"


def apply_profile(raw: dict[str, Any]) -> dict[str, Any]:
    """Fusionne le profil demande sous les valeurs explicites de l'utilisateur.

    Les valeurs du YAML gagnent toujours : le profil ne fait que combler les
    trous, il n'ecrase jamais un choix explicite.
    """
    name = raw.get("profile")
    if name is None:
        return raw
    preset = PROFILES.get(str(name))
    if preset is None:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"profil inconnu : '{name}'. Valeurs possibles : {valid}.")

    merged = dict(raw)
    for section in ("strategy", "risk"):
        defaults = preset[section]
        user_section = dict(merged.get(section) or {})
        merged[section] = {**defaults, **user_section}
    return merged
