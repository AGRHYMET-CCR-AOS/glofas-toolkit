"""Téléchargement et extraction des **prévisions** GloFAS depuis l'EWDS.

Complète ``glofas_download.py`` (historique) et ``glofas_extract.py``
(extraction ponctuelle historique) avec le pendant "prévision" du même
pipeline, nécessaire pour produire des cartes de risque d'inondation à
partir de la prévision du jour.

GloFAS propose deux jeux de données de prévision sur l'EWDS :

- ``cems-glofas-forecast`` (utilisé ici) : horizon de **30 jours**, mis à
  jour **chaque jour**, ensemble de 51 membres (1 prévision de contrôle +
  50 membres perturbés).
- ``cems-glofas-seasonal`` : horizon de ~4 mois, mis à jour une fois par
  mois — non couvert par ce module, mais la même logique s'y transposerait
  (mêmes principes de requête, périodes différentes).

Deux étapes, comme pour l'historique :

1. **Téléchargement** (``download_glofas_forecast``) : un fichier par
   type de produit (contrôle / membres perturbés) et par date d'émission,
   nommés ``glofas_forecast_{AAAAMMJJ}_{control|perturbed}.{ext}``.
2. **Extraction aux points** (``extract_glofas_forecast_at_points``) :
   contrairement à l'historique, on ne recale PAS à nouveau les points sur
   le réseau hydrographique ici — on réutilise directement la maille déjà
   déterminée lors de l'extraction historique (colonnes ``lon_pixel``/
   ``lat_pixel`` du fichier ``*_points.csv`` produit par
   ``glofas_extract.extract_glofas_at_points``). Cela garantit qu'on
   compare la prévision aux seuils historiques calculés **sur exactement
   la même maille** — un recalage indépendant sur la prévision (qui reflète
   la situation du jour, pas un débit moyen) pourrait sélectionner une
   maille différente et fausser la comparaison.

Prérequis (mêmes dépendances que ``glofas_download.py``/``glofas_extract.py``) :
    python -m pip install cdsapi xarray cfgrib eccodes netCDF4 pandas

Utilisation typique ::

    from glofas_forecast import download_glofas_forecast, extract_glofas_forecast_at_points

    download_glofas_forecast(
        issue_date="2026-09-04",
        area=(15, -20, -5, 20),
        output_dir="glofas_forecast_data",
    )

    prevision = extract_glofas_forecast_at_points(
        points_meta="resultats/extraction_points.csv",  # sortie de l'extraction historique
        forecast_dir="glofas_forecast_data",
        output="resultats/prevision_series.csv",
    )
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import socket
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

import pandas as pd

from glofas_extract import _nearest_index, open_month_file  # réutilisation interne

LOG = logging.getLogger("glofas_forecast")

DATASET = "cems-glofas-forecast"
EWDS_URL = "https://ewds.climate.copernicus.eu/api"

PRODUCT_TYPES_ALL = ("control_forecast", "ensemble_perturbed_forecasts")
_PRODUCT_TYPE_SHORT = {
    "control_forecast": "control",
    "ensemble_perturbed_forecasts": "perturbed",
}
_PRODUCT_TYPE_LONG = {short: long for long, short in _PRODUCT_TYPE_SHORT.items()}

# Motif des fichiers produits par download_glofas_forecast() ci-dessous :
# glofas_forecast_{AAAAMMJJ}_{control|perturbed}.{extension}
_FORECAST_FILENAME_RE = re.compile(r"glofas_forecast_(\d{8})_(control|perturbed)\.(\w+)$")


# --------------------------------------------------------------------------- #
# Résultats / utilitaires partagés
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ForecastDownloadResult:
    issue_date: date
    product_type: str
    path: Path
    status: str  # "downloaded" ou "skipped"


def leadtime_hours_for_days(max_days: int = 30, *, step_hours: int = 24) -> list[int]:
    """Liste d'échéances (heures) par pas de ``step_hours`` jusqu'à ``max_days``
    jours -- ``[24, 48, ..., 720]`` par défaut (échéance journalière, horizon
    GloFAS complet de 30 jours).
    """
    if max_days < 1:
        raise ValueError("max_days doit être supérieur ou égal à 1")
    return list(range(step_hours, step_hours * max_days + 1, step_hours))


def _coerce_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Date attendue au format AAAA-MM-JJ, reçu : {value!r}") from exc


def _validate_area(area: Sequence[float]) -> tuple[float, float, float, float]:
    if len(area) != 4:
        raise ValueError("area doit contenir quatre valeurs : nord ouest sud est")
    north, west, south, east = map(float, area)
    if not (-90 <= south < north <= 90):
        raise ValueError("la zone doit respecter -90 <= sud < nord <= 90")
    if not (-180 <= west < east <= 180):
        raise ValueError("la zone doit respecter -180 <= ouest < est <= 180")
    return north, west, south, east


def _is_complete(path: Path, download_format: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if download_format == "zip":
        try:
            with zipfile.ZipFile(path) as archive:
                return archive.testzip() is None and bool(archive.infolist())
        except (OSError, zipfile.BadZipFile):
            return False
    return True


def _configured_url(config_path: Path) -> str:
    """Lit le champ ``url:`` du fichier de config cdsapi (format simple
    ``clé: valeur``, pas besoin de PyYAML) ; retombe sur ``EWDS_URL`` si la
    lecture échoue pour une raison quelconque -- utilisé seulement pour la
    vérification réseau ci-dessous, jamais pour la requête elle-même
    (laissée à cdsapi.Client()).
    """
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("url:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return EWDS_URL


def _check_network(url: str, *, timeout: float = 8.0) -> None:
    """Vérification rapide (DNS + connexion TCP) avant d'utiliser cdsapi.

    En cas de coupure réseau, DNS bloqué par un pare-feu/VPN d'entreprise,
    etc., cdsapi retente en interne jusqu'à 500 fois (toutes les 120 s, soit
    potentiellement plusieurs *heures*) avant d'abandonner, avec un message
    peu clair ("Recovering from connection error... attempt 1 of 500"). On
    échoue ici volontairement vite, avec un message exploitable, plutôt que
    de laisser un notebook tourner en boucle sans qu'on sache pourquoi.
    """
    host = urlparse(url).hostname or url
    try:
        socket.create_connection((host, 443), timeout=timeout).close()
    except OSError as exc:
        raise RuntimeError(
            f"Impossible de joindre {host} ({exc}). Vérifiez votre connexion Internet "
            "(et un éventuel VPN/pare-feu/proxy d'entreprise qui bloquerait ce domaine) "
            "avant de relancer. Sans cette vérification, cdsapi retenterait en interne "
            "pendant potentiellement plusieurs heures (jusqu'à 500 tentatives espacées "
            "de 120 s) sans message d'erreur clair entre-temps."
        ) from exc


def _make_client() -> Any:
    try:
        import cdsapi
    except ImportError as exc:
        raise RuntimeError(
            'Le paquet cdsapi manque. Installez-le avec : python -m pip install "cdsapi>=0.7.7"'
        ) from exc

    # Respecte CDSAPI_RC si défini (cas du notebook de l'atelier, qui utilise
    # un fichier de config nommé différemment de ~/.cdsapirc), sinon le
    # chemin par défaut de cdsapi.
    config = Path(os.environ.get("CDSAPI_RC", str(Path.home() / ".cdsapirc")))
    if not config.is_file():
        raise RuntimeError(
            f"Configuration EWDS absente : créez {config} (ou définissez la variable "
            "d'environnement CDSAPI_RC) avec les champs url et key."
        )
    _check_network(_configured_url(config))
    return cdsapi.Client()


# --------------------------------------------------------------------------- #
# Téléchargement
# --------------------------------------------------------------------------- #


def _download_glofas_forecast_issue(
    issue_date: date,
    *,
    leadtime_hours: Iterable[int],
    area: Sequence[float],
    system_version: str,
    hydrological_model: str,
    product_types: Sequence[str],
    variable: str,
    data_format: str,
    download_format: str,
    output_dir: Path,
    retries: int,
    retry_delay: float,
    overwrite: bool,
    client: Any,
) -> list[ForecastDownloadResult]:
    leadtimes = sorted({int(h) for h in leadtime_hours})
    if not leadtimes:
        raise ValueError("leadtime_hours ne peut pas être vide")
    selected_area = _validate_area(area)
    extension = "zip" if download_format == "zip" else data_format
    results: list[ForecastDownloadResult] = []

    for product_type in product_types:
        short = _PRODUCT_TYPE_SHORT.get(product_type, product_type)
        target = output_dir / f"glofas_forecast_{issue_date:%Y%m%d}_{short}.{extension}"
        partial = target.with_name(target.name + ".part")

        if not overwrite and _is_complete(target, download_format):
            LOG.info("[%s/%s] déjà présent : %s", issue_date, short, target)
            results.append(ForecastDownloadResult(issue_date, product_type, target, "skipped"))
            continue

        request = {
            "system_version": [system_version],
            "hydrological_model": [hydrological_model],
            "product_type": [product_type],
            "variable": variable,
            "year": [f"{issue_date.year}"],
            "month": [f"{issue_date.month:02d}"],
            "day": [f"{issue_date.day:02d}"],
            "leadtime_hour": [str(h) for h in leadtimes],
            "data_format": data_format,
            "download_format": download_format,
            "area": list(selected_area),
        }

        for attempt in range(1, retries + 1):
            try:
                partial.unlink(missing_ok=True)
                LOG.info(
                    "[%s/%s] envoi (tentative %d/%d, %d échéance(s))",
                    issue_date, short, attempt, retries, len(leadtimes),
                )
                client.retrieve(DATASET, request, str(partial))
                if not _is_complete(partial, download_format):
                    raise RuntimeError("le fichier reçu est vide ou corrompu")
                os.replace(partial, target)
                LOG.info("[%s/%s] terminé : %s", issue_date, short, target)
                results.append(ForecastDownloadResult(issue_date, product_type, target, "downloaded"))
                break
            except Exception as exc:
                partial.unlink(missing_ok=True)
                if attempt == retries:
                    LOG.exception("[%s/%s] échec définitif", issue_date, short)
                    raise
                wait = retry_delay * (2 ** (attempt - 1)) + random.uniform(0, retry_delay * 0.1)
                LOG.warning(
                    "[%s/%s] échec temporaire (%s) ; nouvelle tentative dans %.1f s",
                    issue_date, short, exc, wait,
                )
                time.sleep(wait)

    return results


def download_glofas_forecast(
    issue_date: str | date | Iterable[str | date],
    *,
    leadtime_hours: Iterable[int] | None = None,
    max_days: int = 30,
    area: Sequence[float] = (90, -180, -60, 180),
    system_version: str = "operational",
    hydrological_model: str = "lisflood",
    product_types: Sequence[str] = PRODUCT_TYPES_ALL,
    variable: str = "river_discharge_in_the_last_24_hours",
    data_format: str = "grib2",
    download_format: str = "zip",
    output_dir: str | os.PathLike[str] = "glofas_forecast_data",
    retries: int = 4,
    retry_delay: float = 30.0,
    pause_between_issues: float = 5.0,
    overwrite: bool = False,
    client: Any | None = None,
) -> list[ForecastDownloadResult]:
    """Télécharge une ou plusieurs dates d'émission de la prévision GloFAS.

    ``issue_date`` accepte une date seule (``'2026-09-04'`` ou
    ``datetime.date``) ou un itérable de dates. ``leadtime_hours`` accepte
    une liste explicite d'échéances en heures ; par défaut, échéance
    journalière jusqu'à ``max_days`` jours (30 par défaut = horizon complet
    de ``cems-glofas-forecast``). ``product_types`` restreint aux membres
    de contrôle et/ou perturbés (les deux par défaut, soit 51 membres au
    total) -- réduire à ``("control_forecast",)`` accélère nettement le
    téléchargement pour une simple démonstration.

    Comme pour ``glofas_download.download_glofas_discharge``, pensez à
    restreindre ``area`` à votre zone d'intérêt : contrairement à
    l'historique (une requête par mois), une requête de prévision couvre
    déjà jusqu'à 30 échéances (et 50 membres pour les perturbés) -- une
    zone globale peut produire des fichiers volumineux.
    """
    if isinstance(issue_date, (str, date)):
        issues = [_coerce_date(issue_date)]
    else:
        try:
            issues = [_coerce_date(value) for value in issue_date]
        except TypeError as exc:
            raise ValueError("issue_date doit être une date ou un itérable de dates") from exc
    if not issues:
        raise ValueError("issue_date ne peut pas être vide")
    if pause_between_issues < 0:
        raise ValueError("pause_between_issues ne peut pas être négatif")

    unknown = set(product_types) - set(PRODUCT_TYPES_ALL)
    if unknown:
        raise ValueError(f"product_types invalide(s) : {sorted(unknown)} (attendu parmi {PRODUCT_TYPES_ALL})")

    leadtimes = list(leadtime_hours) if leadtime_hours is not None else leadtime_hours_for_days(max_days)

    destination = Path(output_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    api = client or _make_client()
    all_results: list[ForecastDownloadResult] = []

    for index, issue in enumerate(issues):
        all_results.extend(
            _download_glofas_forecast_issue(
                issue,
                leadtime_hours=leadtimes,
                area=area,
                system_version=system_version,
                hydrological_model=hydrological_model,
                product_types=product_types,
                variable=variable,
                data_format=data_format,
                download_format=download_format,
                output_dir=destination,
                retries=retries,
                retry_delay=retry_delay,
                overwrite=overwrite,
                client=api,
            )
        )
        if index < len(issues) - 1 and pause_between_issues:
            LOG.info("Pause de %.1f s avant la date d'émission suivante", pause_between_issues)
            time.sleep(pause_between_issues)

    return all_results


# --------------------------------------------------------------------------- #
# Localisation des fichiers déjà téléchargés
# --------------------------------------------------------------------------- #


def find_forecast_files(
    forecast_dir: str | Path,
    issue_date: str | date | None = None,
) -> dict[date, dict[str, Path]]:
    """Retourne ``{date_emission: {"control": chemin, "perturbed": chemin}}``
    pour les fichiers présents dans ``forecast_dir`` (le membre perturbé est
    optionnel : une date peut n'avoir que le contrôle).
    """
    forecast_dir = Path(forecast_dir)
    if not forecast_dir.is_dir():
        raise FileNotFoundError(f"Dossier de prévisions introuvable : {forecast_dir}")

    wanted = _coerce_date(issue_date) if issue_date is not None else None
    files: dict[date, dict[str, Path]] = {}
    for candidate in sorted(forecast_dir.iterdir()):
        m = _FORECAST_FILENAME_RE.search(candidate.name)
        if not m:
            continue
        issue = datetime.strptime(m.group(1), "%Y%m%d").date()
        if wanted is not None and issue != wanted:
            continue
        files.setdefault(issue, {})[m.group(2)] = candidate

    if not files:
        detail = f" pour la date d'émission {wanted}" if wanted is not None else ""
        raise FileNotFoundError(
            f"Aucun fichier 'glofas_forecast_AAAAMMJJ_{{control,perturbed}}.*' trouvé "
            f"dans {forecast_dir}{detail}."
        )
    return files


# --------------------------------------------------------------------------- #
# Extraction aux points (réutilise la maille de l'extraction historique)
# --------------------------------------------------------------------------- #


def extract_glofas_forecast_at_points(
    points_meta: str | Path | pd.DataFrame,
    forecast_dir: str | Path = "glofas_forecast_data",
    *,
    issue_date: str | date | None = None,
    variable: str | None = None,
    cache_dir: str | Path | None = None,
    output: str | Path | None = None,
) -> pd.DataFrame:
    """Extrait les séries de prévision (contrôle + membres perturbés) aux
    points déjà recalés lors de l'extraction historique.

    Paramètres
    ----------
    points_meta : chemin vers le ``*_points.csv`` produit par
        ``glofas_extract.extract_glofas_at_points`` (ou DataFrame équivalent)
        -- doit contenir au minimum les colonnes ``id``, ``lon_pixel``,
        ``lat_pixel``. La maille utilisée est celle-ci, PAS un nouveau
        recalage sur la prévision (voir note en tête de module).
    forecast_dir : dossier des fichiers téléchargés par
        ``download_glofas_forecast`` (un ou plusieurs dates d'émission).
    issue_date : restreint à une date d'émission (``'2026-09-04'``) ;
        toutes les dates trouvées dans ``forecast_dir`` par défaut.
    output : si fourni, écrit le résultat en CSV.

    Retour
    ------
    DataFrame au format long : ``id, issue_date, leadtime_hours, date,
    membre, variable, value`` -- une ligne par point, échéance et membre
    d'ensemble (``membre`` vaut ``"controle"`` ou ``"perturbe_NN"``).
    """
    meta = points_meta if isinstance(points_meta, pd.DataFrame) else pd.read_csv(points_meta)
    required = {"id", "lon_pixel", "lat_pixel"}
    missing = required - set(meta.columns)
    if missing:
        raise ValueError(
            f"Colonnes manquantes dans points_meta : {sorted(missing)}. "
            "Utilisez le fichier '*_points.csv' produit par glofas_extract.extract_glofas_at_points "
            "(extraction historique) pour garantir la même maille que les seuils de risque."
        )
    meta = meta.drop_duplicates("id").reset_index(drop=True)

    forecast_dir = Path(forecast_dir)
    cache_dir = Path(cache_dir) if cache_dir is not None else forecast_dir / "_extracted"
    files_by_issue = find_forecast_files(forecast_dir, issue_date=issue_date)
    LOG.info("%d date(s) d'émission trouvée(s) dans %s", len(files_by_issue), forecast_dir)

    frames: list[pd.DataFrame] = []
    variable_name: str | None = None

    for issue, files in sorted(files_by_issue.items()):
        issue_ts = pd.Timestamp(datetime(issue.year, issue.month, issue.day))
        for short, path in sorted(files.items()):
            LOG.info("Lecture %s (émission %s, %s)", path.name, issue, short)
            data_array, var_name = open_month_file(path, variable, cache_dir)
            variable_name = var_name

            lat_values = data_array["latitude"].values
            lon_values = data_array["longitude"].values

            for row in meta.itertuples():
                i = _nearest_index(lat_values, row.lat_pixel)
                j = _nearest_index(lon_values, row.lon_pixel)
                sub = data_array.isel(latitude=i, longitude=j)
                df = sub.to_dataframe(name="value").reset_index()

                if "number" in df.columns:
                    df["membre"] = df["number"].apply(lambda n: f"perturbe_{int(n):02d}")
                else:
                    df["membre"] = "controle"

                df["date"] = pd.to_datetime(df["datetime"])
                df["id"] = row.id
                df["issue_date"] = issue
                df["leadtime_hours"] = (
                    (df["date"] - issue_ts).dt.total_seconds() / 3600
                ).round().astype(int)

                frames.append(df[["id", "issue_date", "leadtime_hours", "date", "membre", "value"]])

    if not frames:
        raise ValueError("Aucune donnée de prévision extraite (vérifiez forecast_dir et issue_date).")

    result = pd.concat(frames, ignore_index=True)
    result = (
        result.sort_values(["id", "issue_date", "leadtime_hours", "membre"])
        .drop_duplicates(["id", "issue_date", "leadtime_hours", "membre"])
        .reset_index(drop=True)
    )
    result["variable"] = variable_name

    if output is not None:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_path, index=False)
        LOG.info("Écrit : %s (%d lignes)", output_path, len(result))

    return result


# --------------------------------------------------------------------------- #
# CLI -- deux sous-commandes (download / extract), routées par glofas_cli.py
# --------------------------------------------------------------------------- #


def build_download_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Télécharge la prévision GloFAS (EWDS) pour une ou plusieurs dates d'émission")
    parser.add_argument("--issue-dates", nargs="+", required=True, help="Date(s) d'émission AAAA-MM-JJ")
    parser.add_argument("--max-days", type=int, default=30, help="Horizon en jours (échéance journalière, 30 = horizon complet)")
    parser.add_argument("--area", nargs=4, type=float, metavar=("N", "W", "S", "E"), default=(90, -180, -60, 180))
    parser.add_argument(
        "--products",
        nargs="+",
        choices=["control", "perturbed"],
        default=["control", "perturbed"],
        help="'control' (1 membre, rapide) et/ou 'perturbed' (50 membres) -- les deux par défaut",
    )
    parser.add_argument("--output-dir", default="glofas_forecast_data")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=30.0)
    parser.add_argument("--pause", type=float, default=5.0, help="pause entre deux dates d'émission")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main_download(argv: Sequence[str] | None = None) -> int:
    args = build_download_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    product_types = tuple(_PRODUCT_TYPE_LONG[p] for p in args.products)
    try:
        client = _make_client()
    except RuntimeError as exc:
        LOG.error("%s", exc)
        return 2

    downloaded = skipped = 0
    failures: list[str] = []
    for index, issue_text in enumerate(args.issue_dates):
        try:
            results = download_glofas_forecast(
                issue_text,
                max_days=args.max_days,
                area=args.area,
                product_types=product_types,
                output_dir=args.output_dir,
                retries=args.retries,
                retry_delay=args.retry_delay,
                overwrite=args.overwrite,
                client=client,
            )
            downloaded += sum(r.status == "downloaded" for r in results)
            skipped += sum(r.status == "skipped" for r in results)
        except Exception as exc:
            LOG.error("[%s] échec : %s", issue_text, exc)
            failures.append(issue_text)
        if index < len(args.issue_dates) - 1 and args.pause:
            time.sleep(args.pause)

    LOG.info("Résumé : %d téléchargé(s), %d ignoré(s), %d date(s) en échec", downloaded, skipped, len(failures))
    if failures:
        LOG.error("Dates en échec : %s", ", ".join(failures))
        return 1
    return 0


def build_extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extrait la prévision GloFAS déjà téléchargée aux points d'une extraction historique."
    )
    parser.add_argument("--points-meta", required=True, help="'*_points.csv' de l'extraction historique (glofas_extract)")
    parser.add_argument("--forecast-dir", default="glofas_forecast_data")
    parser.add_argument("--issue-date", default=None, help="Restreint à une date d'émission AAAA-MM-JJ (toutes par défaut)")
    parser.add_argument("--variable", default=None)
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--output", required=True, help="Fichier CSV de sortie")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main_extract(argv: Sequence[str] | None = None) -> int:
    args = build_extract_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        extract_glofas_forecast_at_points(
            points_meta=args.points_meta,
            forecast_dir=args.forecast_dir,
            issue_date=args.issue_date,
            variable=args.variable,
            cache_dir=args.cache_dir,
            output=args.output,
        )
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    # Usage direct (hors glofas_cli.py) : premier argument = sous-commande.
    argv = sys.argv[1:]
    if not argv or argv[0] not in {"download", "extract"}:
        print("Usage : python glofas_forecast.py {download|extract} [options] (--help pour le détail)", file=sys.stderr)
        sys.exit(2)
    sub, rest = argv[0], argv[1:]
    sys.exit(main_download(rest) if sub == "download" else main_extract(rest))
