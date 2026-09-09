"""Seuils de risque par quantile et classification de la prévision GloFAS.

À défaut de seuils de risque "officiels" (périodes de retour calées par
ajustement statistique, comme le fait GloFAS en opérationnel avec les seuils
2 ans / 5 ans / 20 ans), ce module utilise des **quantiles empiriques
calculés sur l'historique** de chaque point comme seuils de référence -- une
approximation simple mais transparente, suffisante pour une démonstration
d'atelier.

Deux étapes :

1. ``compute_historical_thresholds`` : à partir de la série historique
   extraite par ``glofas_extract.extract_glofas_at_points``, calcule par
   point trois seuils (quantiles configurables, 0.80 / 0.90 / 0.98 par
   défaut) qui délimitent quatre situations : **normal**, **risque
   faible**, **risque modéré**, **risque sévère**.
2. ``classify_forecast_risk`` : compare la prévision (extraite par
   ``glofas_forecast.extract_glofas_forecast_at_points``, donc sur la
   *même* maille que l'historique) à ces seuils, pour chaque point et
   chaque échéance -- en tenant compte de l'ensemble des 51 membres
   (statistique centrale + probabilité de dépassement de chaque seuil).

Deux bases possibles pour les quantiles (``basis``) :

- ``"daily"`` (défaut) : quantile calculé sur toutes les valeurs
  journalières de l'historique -- simple, mais mélange saison sèche et
  saison des pluies.
- ``"annual_max"`` : quantile calculé sur les maxima annuels -- plus proche
  de la notion de période de retour (un maximum par an), recommandé si
  l'historique couvre suffisamment d'années (5 ans minimum, idéalement
  bien plus), mais nécessite un historique long.

Utilisation typique ::

    from glofas_risk import compute_historical_thresholds, classify_forecast_risk

    seuils = compute_historical_thresholds(
        "resultats/extraction_series.csv",
        basis="annual_max",
        output="resultats/seuils_risque.csv",
    )
    risque = classify_forecast_risk(
        "resultats/prevision_series.csv",
        seuils,
        output="resultats/prevision_risque.csv",
    )
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

import pandas as pd

LOG = logging.getLogger("glofas_risk")

DEFAULT_QUANTILES: dict[str, float] = {
    "seuil_faible": 0.80,
    "seuil_modere": 0.90,
    "seuil_severe": 0.98,
}

# Ordre croissant de sévérité -- utilisé pour la classification et l'affichage.
RISK_ORDER = ["normal", "risque_faible", "risque_modere", "risque_severe"]
RISK_LABELS = {
    "normal": "Normal",
    "risque_faible": "Risque faible",
    "risque_modere": "Risque modéré",
    "risque_severe": "Risque sévère",
}


def _load_table(source: str | Path | pd.DataFrame) -> pd.DataFrame:
    if isinstance(source, pd.DataFrame):
        return source.copy()
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"Fichier introuvable : {path}")
    return pd.read_csv(path)


def _threshold_columns(thresholds: pd.DataFrame) -> list[str]:
    order_hint = {name: i for i, name in enumerate(DEFAULT_QUANTILES)}
    cols = [c for c in thresholds.columns if c.startswith("seuil_")]
    if not cols:
        raise ValueError(
            "Aucune colonne de seuil ('seuil_*') trouvée -- utilisez compute_historical_thresholds."
        )
    return sorted(cols, key=lambda c: order_hint.get(c, 99))


# --------------------------------------------------------------------------- #
# 1. Seuils historiques (quantiles)
# --------------------------------------------------------------------------- #


def compute_historical_thresholds(
    historical_series: str | Path | pd.DataFrame,
    *,
    quantiles: dict[str, float] | None = None,
    basis: str = "daily",
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    id_col: str = "id",
    date_col: str = "date",
    value_col: str = "value",
    min_years: int = 5,
    output: str | Path | None = None,
) -> pd.DataFrame:
    """Calcule, pour chaque point, un seuil de discharge par niveau de risque.

    ``quantiles`` : dictionnaire ``{nom_seuil: quantile}`` (0 < quantile < 1),
    ``DEFAULT_QUANTILES`` par défaut. Les noms doivent commencer par
    ``"seuil_"`` pour être reconnus par ``classify_forecast_risk``.

    ``start``/``end`` (optionnels, ``"AAAA-MM-JJ"`` ou ``None``) restreignent
    la période historique utilisée pour le calcul des quantiles -- par
    défaut (les deux à ``None``), toute la période disponible dans
    ``historical_series`` est utilisée. Bornes incluses.

    Retour : DataFrame avec une ligne par point (``id``), une colonne par
    seuil, et ``n_valeurs`` (nombre de valeurs -- journalières ou maxima
    annuels selon ``basis`` -- utilisées pour le calcul, à examiner avant de
    faire confiance à un seuil calculé sur trop peu de données).
    """
    quantiles = dict(DEFAULT_QUANTILES if quantiles is None else quantiles)
    if not quantiles:
        raise ValueError("quantiles ne peut pas être vide")
    for name, q in quantiles.items():
        if not name.startswith("seuil_"):
            raise ValueError(f"Les noms de seuil doivent commencer par 'seuil_' : {name!r}")
        if not (0 < q < 1):
            raise ValueError(f"Le quantile '{name}' doit être compris entre 0 et 1 (exclus) : {q}")
    if basis not in {"daily", "annual_max"}:
        raise ValueError("basis doit valoir 'daily' ou 'annual_max'")

    data = _load_table(historical_series)
    if data.empty:
        raise ValueError("Série historique vide.")
    missing = {id_col, date_col, value_col} - set(data.columns)
    if missing:
        raise ValueError(f"Colonnes manquantes dans la série historique : {sorted(missing)}")

    data = data.copy()
    data[date_col] = pd.to_datetime(data[date_col])

    if start is not None or end is not None:
        available_min, available_max = data[date_col].min(), data[date_col].max()
        start_ts = pd.to_datetime(start) if start is not None else available_min
        end_ts = pd.to_datetime(end) if end is not None else available_max
        if start_ts > end_ts:
            raise ValueError(f"start ({start_ts.date()}) postérieur à end ({end_ts.date()}).")
        data = data[(data[date_col] >= start_ts) & (data[date_col] <= end_ts)]
        if data.empty:
            raise ValueError(
                f"Aucune valeur historique entre {start_ts.date()} et {end_ts.date()} "
                f"(période disponible : {available_min.date()} -- {available_max.date()})."
            )
        LOG.info(
            "Période historique restreinte à %s -- %s (disponible : %s -- %s).",
            start_ts.date(), end_ts.date(), available_min.date(), available_max.date(),
        )

    if basis == "annual_max":
        data["annee"] = data[date_col].dt.year
        base = data.groupby([id_col, "annee"], as_index=False)[value_col].max()
        grouped = base.groupby(id_col)[value_col]
    else:
        grouped = data.groupby(id_col)[value_col]

    ordered_names = [name for name, _ in sorted(quantiles.items(), key=lambda kv: kv[1])]

    rows = []
    for point_id, group in grouped:
        thresholds = {name: float(group.quantile(q)) for name, q in quantiles.items()}
        for prev, nxt in zip(ordered_names, ordered_names[1:]):
            if thresholds[prev] > thresholds[nxt]:
                LOG.warning(
                    "Point %s : seuils non strictement croissants (%s=%.2f > %s=%.2f) -- "
                    "distribution très resserrée ; valeurs conservées telles quelles.",
                    point_id, prev, thresholds[prev], nxt, thresholds[nxt],
                )
        row = {"id": point_id, "n_valeurs": int(group.shape[0])}
        row.update(thresholds)
        rows.append(row)

    if basis == "annual_max":
        n_years = base.groupby(id_col)["annee"].nunique()
        too_short = n_years[n_years < min_years]
        if not too_short.empty:
            LOG.warning(
                "Moins de %d années de maxima annuels (basis='annual_max') pour : %s -- "
                "seuils peu fiables, préférez un historique plus long ou basis='daily'.",
                min_years, ", ".join(map(str, too_short.index.tolist())),
            )

    result = pd.DataFrame(rows).sort_values("id").reset_index(drop=True)

    if output is not None:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_path, index=False)
        LOG.info("Écrit : %s (%d point(s), basis=%s)", output_path, len(result), basis)

    return result


# --------------------------------------------------------------------------- #
# 2. Classification de la prévision
# --------------------------------------------------------------------------- #


def classify_forecast_risk(
    forecast_series: str | Path | pd.DataFrame,
    thresholds: str | Path | pd.DataFrame,
    *,
    central_stat: str = "median",
    output: str | Path | None = None,
) -> pd.DataFrame:
    """Classe chaque point/échéance de la prévision par rapport aux seuils
    historiques.

    Pour chaque ``(id, issue_date, leadtime_hours)`` : statistique centrale
    de l'ensemble (``central_stat``, ``'median'`` par défaut -- robuste aux
    membres extrêmes), min/max de l'ensemble, valeur du membre de contrôle,
    probabilité de dépassement de chaque seuil (fraction des membres -- 51
    au total avec les deux produits -- dépassant le seuil), et catégorie de
    risque (``RISK_ORDER``) déduite de la comparaison entre la statistique
    centrale et les seuils.

    La catégorie de risque est donc une lecture simplifiée : pour une vue
    probabiliste complète, gardez les colonnes ``probabilite_depassement_*``
    (l'usage GloFAS opérationnel raisonne typiquement en "probabilité de
    dépasser le seuil X ans", pas en franchissement binaire d'un seul
    membre central).
    """
    forecast = _load_table(forecast_series)
    thresh = _load_table(thresholds)
    if forecast.empty:
        raise ValueError("Série de prévision vide.")
    if thresh.empty:
        raise ValueError("Table de seuils vide.")

    threshold_cols = _threshold_columns(thresh)

    missing_ids = sorted(set(forecast["id"]) - set(thresh["id"]))
    if missing_ids:
        LOG.warning(
            "Pas de seuil historique pour : %s -- ces points seront classés 'normal' faute de référence.",
            ", ".join(map(str, missing_ids)),
        )

    group_cols = ["id", "issue_date", "leadtime_hours", "date"]
    missing_cols = set(group_cols + ["membre", "value"]) - set(forecast.columns)
    if missing_cols:
        raise ValueError(
            f"Colonnes manquantes dans la série de prévision : {sorted(missing_cols)}. "
            "Utilisez glofas_forecast.extract_glofas_forecast_at_points."
        )

    agg = forecast.groupby(group_cols)["value"].agg(
        membre_central=central_stat, minimum="min", maximum="max", n_membres="count"
    ).reset_index()

    control = forecast.loc[forecast["membre"] == "controle", group_cols + ["value"]].rename(
        columns={"value": "valeur_controle"}
    )
    agg = agg.merge(control, on=group_cols, how="left")
    agg = agg.merge(thresh[["id"] + threshold_cols], on="id", how="left")

    for col in threshold_cols:
        level_name = col.replace("seuil_", "")
        merged = forecast.merge(thresh[["id", col]], on="id", how="left")
        prob = (
            (merged["value"] > merged[col])
            .groupby([merged[c] for c in group_cols])
            .mean()
            .rename(f"probabilite_depassement_{level_name}")
            .reset_index()
        )
        agg = agg.merge(prob, on=group_cols, how="left")

    def _classify(row: pd.Series) -> str:
        category = "normal"
        for col in threshold_cols:  # ordre croissant de sévérité
            seuil = row.get(col)
            if pd.notna(seuil) and pd.notna(row["membre_central"]) and row["membre_central"] >= seuil:
                category = col.replace("seuil_", "risque_")
        return category

    agg["risque"] = agg.apply(_classify, axis=1)
    agg["leadtime_jours"] = agg["leadtime_hours"] / 24
    agg = agg.sort_values(["id", "issue_date", "leadtime_hours"]).reset_index(drop=True)

    if output is not None:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        agg.to_csv(output_path, index=False)
        LOG.info("Écrit : %s (%d ligne(s))", output_path, len(agg))

    return agg


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calcule des seuils de risque par quantile historique, et/ou classe une prévision GloFAS."
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_seuils = sub.add_parser("seuils", help="Calcule les seuils de quantile par point à partir de l'historique")
    p_seuils.add_argument("--historique", required=True, help="'*_series.csv' de l'extraction historique")
    p_seuils.add_argument("--basis", choices=["daily", "annual_max"], default="daily")
    p_seuils.add_argument("--debut", default=None, help="Date de début (AAAA-MM-JJ) -- défaut : toute la période disponible")
    p_seuils.add_argument("--fin", default=None, help="Date de fin (AAAA-MM-JJ) -- défaut : toute la période disponible")
    p_seuils.add_argument("--faible", type=float, default=DEFAULT_QUANTILES["seuil_faible"])
    p_seuils.add_argument("--modere", type=float, default=DEFAULT_QUANTILES["seuil_modere"])
    p_seuils.add_argument("--severe", type=float, default=DEFAULT_QUANTILES["seuil_severe"])
    p_seuils.add_argument("--output", required=True)
    p_seuils.add_argument("--verbose", action="store_true")

    p_classer = sub.add_parser("classer", help="Classe la prévision par rapport aux seuils")
    p_classer.add_argument("--prevision", required=True, help="CSV produit par extract_glofas_forecast_at_points")
    p_classer.add_argument("--seuils", required=True, help="CSV produit par la sous-commande 'seuils'")
    p_classer.add_argument("--central-stat", default="median", choices=["median", "mean", "max"])
    p_classer.add_argument("--output", required=True)
    p_classer.add_argument("--verbose", action="store_true")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        if args.action == "seuils":
            compute_historical_thresholds(
                args.historique,
                quantiles={
                    "seuil_faible": args.faible,
                    "seuil_modere": args.modere,
                    "seuil_severe": args.severe,
                },
                basis=args.basis,
                start=args.debut,
                end=args.fin,
                output=args.output,
            )
        else:
            classify_forecast_risk(
                args.prevision,
                args.seuils,
                central_stat=args.central_stat,
                output=args.output,
            )
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
